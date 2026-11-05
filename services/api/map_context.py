"""Viewport/zoom display context; analytical source geometries stay independent."""

import hashlib
import json
import sqlite3
from contextlib import closing

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from shapely import from_wkb, set_precision
from shapely.geometry import box, mapping

from services.api.buildings import building_class
from services.api.locations import DATA
from tools.release.response_cache import response_cache

router = APIRouter(prefix="/api")

SPATIAL_QUERY = """SELECT f.osm_id,f.properties,f.geometry FROM features f
    JOIN bounds b ON f.id=b.id WHERE min_zoom<=? AND b.minx<=? AND b.maxx>=?
    AND b.miny<=? AND b.maxy>=? ORDER BY CASE f.kind WHEN 'address' THEN 2
    WHEN 'building' THEN 1 ELSE 0 END,f.id LIMIT 12001"""

OVERVIEW_QUERY = """WITH visible AS MATERIALIZED (
    SELECT z.id FROM features z
    CROSS JOIN bounds b ON z.id=b.id WHERE z.min_zoom<=?
    AND b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?)
    SELECT f.osm_id,f.properties,f.geometry FROM visible v
    JOIN features f ON f.id=v.id ORDER BY CASE f.kind WHEN 'address' THEN 2
    WHEN 'building' THEN 1 ELSE 0 END,f.id LIMIT 12001"""


def context_query(db, west, south, east, north, zoom):
    # Broad low-zoom views have few visible kinds but many spatial candidates.
    # Filter using the existing covering index before loading geometry blobs.
    # Keep spatial-first lookup for close views; zoom-first is much slower there.
    if zoom <= 11 and east - west >= 0.05 and north - south >= 0.05:
        # Do not scan the city-wide zoom index for an empty spatial window.
        if db.execute(
            "SELECT 1 FROM bounds WHERE minx<=? AND maxx>=? AND miny<=? AND maxy>=? LIMIT 1",
            (east, west, north, south),
        ).fetchone():
            return OVERVIEW_QUERY
    return SPATIAL_QUERY


def overview_roads(features):
    """Group display lines with identical attributes; keep every source part/ID.

    This changes neither analytical geometry nor any routing/access input. The
    close-view response retains individual features and their original IDs.
    """
    groups, result = {}, []
    for feature in features:
        geometry = feature["geometry"]
        if feature["properties"].get("kind") != "road" or geometry["type"] not in {
            "LineString",
            "MultiLineString",
        }:
            result.append(feature)
            continue
        key = json.dumps(feature["properties"], sort_keys=True, separators=(",", ":"))
        groups.setdefault(key, []).append(feature)
    for key, members in groups.items():
        if len(members) == 1:
            result.append(members[0])
            continue
        parts = []
        for member in members:
            geometry = member["geometry"]
            parts.extend(
                [geometry["coordinates"]]
                if geometry["type"] == "LineString"
                else geometry["coordinates"]
            )
        result.append(
            {
                "type": "Feature",
                "id": "road-group/" + hashlib.sha256(key.encode()).hexdigest(),
                "properties": members[0]["properties"] | {"member_ids": [f["id"] for f in members]},
                "geometry": {"type": "MultiLineString", "coordinates": parts},
            }
        )
    return result


@response_cache()
def context_features(path, signature, west, south, east, north, zoom):
    viewport = box(west, south, east, north)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            context_query(db, west, south, east, north, zoom),
            (zoom, east, west, north, south),
        ).fetchall()
        version = dict(db.execute("SELECT key,value FROM metadata"))
    result = []
    for identifier, properties, wkb in rows[:12000]:
        geometry = from_wkb(wkb)
        if geometry.intersects(viewport):
            geometry = set_precision(geometry.intersection(viewport), 0.000001)
            if geometry.is_empty or geometry.geom_type not in {
                "Point",
                "LineString",
                "MultiLineString",
                "Polygon",
                "MultiPolygon",
            }:
                continue
            values = json.loads(properties)
            if values.get("kind") == "building":
                values["building_class"] = building_class(values.get("building"))
            result.append(
                {
                    "type": "Feature",
                    "id": identifier,
                    "properties": values,
                    "geometry": mapping(geometry),
                }
            )
    result = {
        "type": "FeatureCollection",
        "display_version": version["version"] + ("-overview-roads-v1" if zoom <= 11 else ""),
        "data_version": version["locations_version"],
        "features": overview_roads(result) if zoom <= 11 else result,
        "truncated": len(rows) > 12000,
    }
    # Serialize once in the worker thread; returning a nested dict makes FastAPI
    # repeatedly walk every vertex on the event loop, delaying unrelated selection.
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


@router.get("/map-context")
def map_context(
    west: float = Query(ge=-180, le=180),
    south: float = Query(ge=-90, le=90),
    east: float = Query(ge=-180, le=180),
    north: float = Query(ge=-90, le=90),
    zoom: int = Query(ge=9, le=18),
):
    if east <= west or north <= south or east - west > 2 or north - south > 1:
        raise HTTPException(422, "Wybierz poprawny widok mapy Krakowa.")
    path = DATA / "map-context.sqlite"
    if not path.is_file():
        raise HTTPException(503, "Szczegóły ulic i budynków są niedostępne.")
    signature = path.stat().st_mtime_ns
    return Response(
        context_features(path, signature, west, south, east, north, zoom),
        media_type="application/geo+json",
        headers={"Cache-Control": "no-cache"},
    )
