"""Versioned display tiles; source/analytical databases are never modified."""

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import closing
from functools import lru_cache

import mapbox_vector_tile
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pyproj import Transformer
from shapely import from_wkb, transform, unary_union
from shapely.geometry import box
from starlette.concurrency import run_in_threadpool

from services.api.buildings import building_class
from services.api.locations import DATA

router = APIRouter(prefix="/api")
POLICY = "context-mvt-v2"
NOISE_POLICY = "night-topology-prequant-halfgrid-v1"
WORLD = 20037508.342789244
TO_MERCATOR = Transformer.from_crs(4326, 3857, always_xy=True).transform
TO_WGS = Transformer.from_crs(3857, 4326, always_xy=True).transform
SQL = """SELECT f.osm_id,f.properties,f.geometry FROM bounds b
    CROSS JOIN features f ON f.id=b.id WHERE b.minx<=? AND b.maxx>=?
    AND b.miny<=? AND b.maxy>=? AND f.min_zoom<=? ORDER BY f.id"""
NOISE_SQL = """SELECT f.id,json_object('kind','noise','known',
    f.lower_db IS NOT NULL OR f.upper_db IS NOT NULL,
    'lower_db',f.lower_db,'upper_db',f.upper_db),f.geometry FROM noise_bounds b
    CROSS JOIN noise f ON f.id=b.id WHERE b.minx<=? AND b.maxx>=?
    AND b.miny<=? AND b.maxy>=? AND ? >=9 ORDER BY f.id"""


class TileCache:
    """Bound both encoded bytes and entries; never retain decoded geometry."""

    def __init__(self, max_bytes=24 * 1024 * 1024, max_entries=128):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.entries = OrderedDict()
        self.bytes = 0
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            value = self.entries.get(key)
            if value is not None:
                self.entries.move_to_end(key)
            return value

    def put(self, key, value):
        with self.lock:
            previous = self.entries.pop(key, None)
            if previous is not None:
                self.bytes -= len(previous[0])
            if len(value[0]) > self.max_bytes:
                return
            self.entries[key] = value
            self.bytes += len(value[0])
            while self.bytes > self.max_bytes or len(self.entries) > self.max_entries:
                _, removed = self.entries.popitem(last=False)
                self.bytes -= len(removed[0])


CACHE = TileCache()
# Serialize identical first misses without keeping unbounded per-request locks.
LOCKS = [threading.Lock() for _ in range(16)]


@lru_cache(maxsize=4)
def tile_limiter(loop):
    # One live ASGI loop in production; bounded loop keys also support TestClient.
    return asyncio.Semaphore(2)


@lru_cache(maxsize=16)
def source_version(path, modified, size):
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    policy = NOISE_POLICY if path.name == "noise-night.sqlite" else POLICY
    return f"{policy}-{digest[:24]}"


def source():
    path = DATA / "map-context.sqlite"
    if not path.is_file():
        raise HTTPException(503, "Szczegóły ulic i budynków są niedostępne.")
    stat = path.stat()
    return path, source_version(path, stat.st_mtime_ns, stat.st_size)


def tile_bounds(z, x, y):
    width = 2 * WORLD / (2**z)
    west, north = -WORLD + x * width, WORLD - y * width
    return west, north - width, west + width, north


@lru_cache(maxsize=4)
def source_bounds(path, version):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        meta = display_metadata(db)
        if meta.get("tiles_complete"):
            return meta["wgs_bounds"]
        result = list(
            db.execute(
                "SELECT min(minx),min(miny),max(maxx),max(maxy) FROM "
                + ("noise_bounds" if path.name == "noise-night.sqlite" else "bounds")
            ).fetchone()
        )
        stat = path.stat()
        if projected_source(path, stat.st_mtime_ns, stat.st_size):
            return list(transform(box(*result), TO_WGS, interleaved=False).bounds)
        return result


def polygon_signature(geometry):
    if geometry.geom_type == "Polygon":
        return 1, len(geometry.interiors)
    if geometry.geom_type == "MultiPolygon":
        return len(geometry.geoms), sum(len(g.interiors) for g in geometry.geoms)
    return None


def noise_display_geometry(geometry, tolerance):
    """At most half an MVT grid cell, or 1/16 of a512px tile pixel.

    Removes redundant subpixel vertices BEFORE the Python encoder. Original
    analytical WKB never changes; reject any change of parts/holes or validity.
    """
    simplified = geometry.simplify(tolerance, preserve_topology=True)
    if (
        simplified.is_empty
        or not simplified.is_valid
        or simplified.geom_type != geometry.geom_type
        or polygon_signature(simplified) != polygon_signature(geometry)
    ):
        return geometry
    return simplified


@lru_cache(maxsize=8)
def projected_source(path, modified, size):
    if path.name == "noise-night.sqlite":
        return False
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return display_metadata(db).get("crs") == "EPSG:3857"


def display_metadata(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
        return {}
    row = db.execute("SELECT value FROM metadata LIMIT 1").fetchone()
    return json.loads(row[0]) if row and str(row[0]).startswith("{") else {}


def encode_tile(path, z, x, y):
    bounds = tile_bounds(z, x, y)
    buffer = (bounds[2] - bounds[0]) / 32  # 16 screen pixels beyond a512px tile.
    stat = path.stat()
    projected_input = projected_source(path, stat.st_mtime_ns, stat.st_size)
    clip = box(bounds[0] - buffer, bounds[1] - buffer, bounds[2] + buffer, bounds[3] + buffer)
    if not projected_input:
        clip = transform(clip, TO_WGS, interleaved=False)
    west, south, east, north = clip.bounds
    features = []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            NOISE_SQL if path.name == "noise-night.sqlite" else SQL, (east, west, north, south, z)
        )
        # Prepared spatial fragments avoid reading city-spanning WKB per tile.
        # Rejoin fragments of the SAME original object, never different bands.
        if projected_input:
            groups = {}
            for identifier, raw, wkb in rows:
                groups.setdefault((identifier, raw), []).append(from_wkb(wkb))
            rows = ((key[0], key[1], unary_union(parts).wkb) for key, parts in groups.items())
        for identifier, raw, wkb in rows:
            geometry = from_wkb(wkb)
            props = json.loads(raw)
            if props.get("kind") == "noise":
                props["known"] = bool(props["known"])
            if props.get("kind") in {"parking", "park_ride"} and z < 14:
                geometry = geometry.representative_point()
            if not geometry.intersects(clip):
                continue
            geometry = geometry.intersection(clip)
            if geometry.is_empty:
                continue
            props = {
                k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in props.items()
            }
            props["source_id"] = identifier
            if props.get("kind") == "building":
                props["building_class"] = building_class(props.get("building"))
            parts = geometry.geoms if geometry.geom_type == "GeometryCollection" else [geometry]
            for part in parts:
                if part.geom_type not in {
                    "Point",
                    "MultiPoint",
                    "LineString",
                    "MultiLineString",
                    "Polygon",
                    "MultiPolygon",
                }:
                    continue
                projected = (
                    part if projected_input else transform(part, TO_MERCATOR, interleaved=False)
                )
                if props.get("kind") == "noise":
                    projected = noise_display_geometry(projected, (bounds[2] - bounds[0]) / 8192)
                features.append(
                    {
                        "geometry": projected,
                        "properties": props,
                    }
                )
    return (
        mapbox_vector_tile.encode(
            {"name": "context", "features": features},
            default_options={"quantize_bounds": bounds, "extents": 4096},
        ),
        len(features),
    )


@router.get("/map-tiles.json")
def tile_info(response: Response):
    path, version = source()
    response.headers["Cache-Control"] = "no-cache"
    return {
        "tilejson": "3.0.0",
        "name": "Kraków display context",
        "version": version,
        "tiles": [f"/api/map-tiles/{{z}}/{{x}}/{{y}}.pbf?version={version}"],
        "minzoom": 9,
        "maxzoom": 16,
        "bounds": source_bounds(path, version),
        "vector_layers": [{"id": "context", "fields": {"source_id": "String", "kind": "String"}}],
        "coverage_complete": False,
        "coverage": "Imported source records; empty tiles do not prove absence of real objects",
    }


def prepared_tile(path, actual, z, x, y):
    """Static reads never wait behind geometry workers or striped build locks."""
    key = (actual, z, x, y)
    value = CACHE.get(key)
    if value is not None:
        return value, True
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT body,features FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)
        ).fetchone()
    value = (
        (row[0], row[1])
        if row
        else (mapbox_vector_tile.encode({"name": "context", "features": []}), 0)
    )
    CACHE.put(key, value)
    return value, False


def cached_tile(path, actual, z, x, y):
    key = (actual, z, x, y)
    value = CACHE.get(key)
    hit = value is not None
    if value is None:
        with LOCKS[hash(key) % len(LOCKS)]:
            value = CACHE.get(key)
            if value is None:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                    prepared = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='tiles'"
                    ).fetchone()
                    row = (
                        db.execute(
                            "SELECT body,features FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)
                        ).fetchone()
                        if prepared
                        else None
                    )
                    complete = bool(
                        prepared
                        and json.loads(
                            db.execute("SELECT value FROM metadata LIMIT 1").fetchone()[0]
                        ).get("tiles_complete")
                    )
                value = (
                    (row[0], row[1])
                    if row
                    else (mapbox_vector_tile.encode({"name": "context", "features": []}), 0)
                    if complete
                    else encode_tile(path, z, x, y)
                )
                CACHE.put(key, value)
    return value, hit


@router.get("/map-tiles/{z}/{x}/{y}.pbf")
async def map_tile(request: Request, z: int, x: int, y: int, version: str = Query()):
    if not 9 <= z <= 16 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise HTTPException(404, "Tile outside supported pyramid")
    path, actual = source()
    if version != actual:
        raise HTTPException(409, "Map source version changed; reload the map")
    started = time.perf_counter()
    async with tile_limiter(asyncio.get_running_loop()):
        if await request.is_disconnected():
            return Response(status_code=499)
        value, hit = await run_in_threadpool(cached_tile, path, actual, z, x, y)
    elapsed = (time.perf_counter() - started) * 1000
    return Response(
        value[0],
        media_type="application/vnd.mapbox-vector-tile",
        headers={
            "Cache-Control": "public,max-age=31536000,immutable",
            "ETag": f'"{actual}-{z}-{x}-{y}"',
            "Server-Timing": f'tile;dur={elapsed:.2f};desc="{"hit" if hit else "miss"}"',
            "X-Map-Features": str(value[1]),
            "X-Map-Truncated": "false",
        },
    )
