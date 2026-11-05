"""Prepared official layers served through the existing bounded tile encoder."""

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from functools import lru_cache
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from shapely import from_wkb
from shapely.geometry import Point
from starlette.concurrency import run_in_threadpool

from services.api.locations import DATA
from services.api.map_tiles import CACHE, cached_tile, source_bounds, source_version, tile_limiter

router = APIRouter(prefix="/api")
Layer = Literal[
    "residents",
    "residents-overview",
    "prices",
    "prices-primary",
    "prices-secondary",
    "age-young",
    "age-working",
    "age-senior",
    "air",
    "air-display",
    "mobility",
    "greenery",
    "noise",
]


@router.get("/ux-areas")
def areas():
    path = DATA / "ux-areas.geojson"
    if not path.is_file():
        raise HTTPException(503, "Granice SIM nie zostały przygotowane.")
    return FileResponse(
        path, media_type="application/geo+json", headers={"Cache-Control": "no-cache"}
    )


@router.get("/ux-areas/{identifier}")
def area(identifier: str):
    path = DATA / "ux-areas-summary.json"
    if not path.is_file():
        raise HTTPException(503, "Dane obszarów niedostępne.")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if identifier not in doc:
        raise HTTPException(404, "Nieznany obszar SIM")
    return doc[identifier]


def source(layer):
    path = DATA / f"ux-{layer}.sqlite"
    if layer == "noise" and not path.exists():
        path = DATA / "noise-night.sqlite"
    if not path.is_file():
        raise HTTPException(503, "Źródło nie zostało przygotowane.")
    stat = path.stat()
    version = f"ux-{layer}-" + source_version(path, stat.st_mtime_ns, stat.st_size)
    if layer in ("air-display", "residents-overview"):
        meta = metadata(path, version)
        parents = {meta["analytical_source"]: meta["analytical_sha256"]}
        if layer == "residents-overview":
            parents["ux-areas.geojson"] = meta["area_source_sha256"]
        for name, expected in parents.items():
            parent = DATA / name
            if not parent.is_file():
                raise HTTPException(503, "Źródło analityczne niedostępne")
            ps = parent.stat()
            actual = source_version(parent, ps.st_mtime_ns, ps.st_size).rsplit("-", 1)[-1]
            if actual != expected[:24]:
                raise HTTPException(503, "Warstwa wymaga przygotowania")
    if layer in ("greenery", "noise"):
        meta = metadata(path, version)
        if meta.get("analytical_source"):
            parent = DATA / meta["analytical_source"]
            if not parent.is_file():
                raise HTTPException(503, "Źródło analityczne niedostępne")
            ps = parent.stat()
            if (ps.st_size, ps.st_mtime_ns) == (
                meta.get("analytical_size"),
                meta.get("analytical_mtime_ns"),
            ):
                return path, version
            if layer == "greenery" and meta.get("analytical_geometry_sha256"):
                from services.api.display_lineage import green_fingerprint

                actual = green_fingerprint(parent, ps.st_mtime_ns, ps.st_size)
                expected = meta["analytical_geometry_sha256"]
            else:
                actual = source_version(parent, ps.st_mtime_ns, ps.st_size).rsplit("-", 1)[-1]
                expected = meta["analytical_sha256"][:24]
            if actual != expected:
                raise HTTPException(503, "Warstwa wymaga przygotowania")
    return path, version


@lru_cache(maxsize=6)
def metadata(path, version):
    if path.name == "noise-night.sqlite":
        return {
            "layer": "noise",
            "model_year": 2022,
            "indicator": "LN",
            "method": "road-ln-intervals-v1",
        }
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return json.loads(db.execute("SELECT value FROM metadata").fetchone()[0])


@router.get("/ux-context")
def context():
    layers = {}
    for layer in ("residents", "prices", "air", "age-young", "age-working", "age-senior"):
        try:
            path, version = source(layer)
            layers[layer] = metadata(path, version) | {"version": version, "available": True}
        except HTTPException:
            layers[layer] = {"available": False, "reason": "Źródło nie zostało przygotowane"}
    mobility = DATA / "mobility-ux.geojson"
    if not mobility.is_file():
        mobility = DATA / "osm-mobility.geojson"
    excluded = ["parking_zone"]
    if mobility.is_file():
        # Existing manifest describes the installed profile. Never reintroduce
        # the owner's excluded ZDMK geometry from a full-source checkout.
        head = mobility.read_text(encoding="utf-8")
        doc = json.loads(head)
        excluded = doc.get("excluded_categories", [])
    return {"layers": layers, "excluded_categories": excluded}


@router.get("/ux/{layer}.json")
def tile_info(layer: Layer, response: Response):
    path, version = source(layer)
    response.headers["Cache-Control"] = "no-cache"
    meta = metadata(path, version)
    if meta.get("format") == "png":
        return {
            **meta,
            "tilejson": "3.0.0",
            "tiles": [f"/api/ux/{layer}/{{z}}/{{x}}/{{y}}.png?version={version}"],
            "tileSize": 256,
            "minzoom": 9,
            "maxzoom": meta["maxzoom"],
            "version": version,
        }
    return {
        **meta,
        "tilejson": "3.0.0",
        "tiles": [f"/api/ux/{layer}/{{z}}/{{x}}/{{y}}.pbf?version={version}"],
        "minzoom": 9,
        "maxzoom": meta.get("maxzoom", 16),
        "bounds": source_bounds(path, version),
        "vector_layers": [{"id": "context", "fields": {"value": "Number", "state": "String"}}],
        "version": version,
    }


@router.get("/ux/{layer}/{z}/{x}/{y}.png")
async def raster_tile(layer: Literal["air-display"], z: int, x: int, y: int, version: str):
    if not 9 <= z <= 14 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise HTTPException(404, "Tile outside prepared pyramid")
    path, actual = source(layer)
    if actual != version:
        raise HTTPException(409, "Source version changed; reload the map")

    def read():
        key = (actual, z, x, y)
        value = CACHE.get(key)
        if value is None:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                value = db.execute(
                    "SELECT body,features FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)
                ).fetchone()
                if value is None:
                    value = db.execute("SELECT body,features FROM tiles WHERE z=0").fetchone()
            CACHE.put(key, value)
        return value[0]

    body = await run_in_threadpool(read)
    return Response(
        body, media_type="image/png", headers={"Cache-Control": "public,max-age=31536000,immutable"}
    )


@router.get("/ux/{layer}/{z}/{x}/{y}.pbf")
async def tile(layer: Layer, z: int, x: int, y: int, request: Request, version: str = Query()):
    if not 9 <= z <= 16 or not 0 <= x < 2**z or not 0 <= y < 2**z:
        raise HTTPException(404, "Tile outside supported pyramid")
    path, actual = source(layer)
    if version != actual:
        raise HTTPException(409, "Source version changed; reload the map")
    if metadata(path, actual).get("format") == "png":
        raise HTTPException(404, "Raster layer requires PNG tiles")
    started = time.perf_counter()
    if metadata(path, actual).get("tiles_complete"):
        if await request.is_disconnected():
            return Response(status_code=499)
        from services.api.map_tiles import prepared_tile

        value, hit = await run_in_threadpool(prepared_tile, path, actual, z, x, y)
    else:
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
        },
    )


def point_values(layer, longitude, latitude):
    path, version = source(layer)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            (
                "SELECT f.properties,f.geometry FROM bounds b CROSS JOIN features f "
                "ON b.id=f.id WHERE b.minx<=? AND b.maxx>=? AND b.miny<=? AND "
                "b.maxy>=? ORDER BY f.id"
            ),
            (longitude, longitude, latitude, latitude),
        )
        matches = [
            json.loads(raw) for raw, wkb in rows if from_wkb(wkb).covers(Point(longitude, latitude))
        ]
        if layer.startswith("prices"):
            detail = [p for p in matches if p.get("detail_level") == "detail"]
            matches = detail or matches
    return {"metadata": metadata(path, version), "matches": matches}


@router.get("/ux-at")
def at(
    longitude: float = Query(ge=19, le=21),
    latitude: float = Query(ge=49, le=51),
    market: Literal["all", "primary", "secondary"] = "all",
    age: Literal["total", "young", "working", "senior"] = "total",
):
    result = {}
    for layer in ("residents", "prices", "air"):
        try:
            dataset = "prices-" + market if layer == "prices" and market != "all" else layer
            if layer == "residents" and age != "total":
                dataset = "age-" + age
            result[layer] = point_values(dataset, longitude, latitude)
        except HTTPException:
            result[layer] = {"metadata": None, "matches": []}
    return result
