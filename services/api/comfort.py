"""Exact point facts against an immutable precomputed place reference."""

import json
import sqlite3
from contextlib import closing
from functools import lru_cache
from math import hypot
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pyproj import Transformer
from shapely import from_wkb
from shapely.geometry import Point
from shapely.ops import transform, unary_union

from services.api.locations import DATA
from services.api.noise_point import lookup
from services.api.ux_layers import point_values
from tools.data.acquire import sha256

router = APIRouter(prefix="/api")


def file_stamp(path):
    try:
        stat = path.stat()
        return str(path.absolute()), stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return str(path.absolute()), None, None


@lru_cache(maxsize=1)
def reference_document(stamp):
    from pathlib import Path

    path = Path(stamp[0])
    if not path.is_file():
        raise HTTPException(503, "Porównanie miejskie niedostępne")
    return json.loads(path.read_text(encoding="utf-8"))


def input_stamp():
    stamp = file_stamp(DATA / "comfort-v03.json")
    value = reference_document(stamp)
    names = sorted({*value["sources"], "greenery-v36.sqlite", "osm-services.geojson"})
    return stamp, tuple(file_stamp(DATA / name) for name in names)


@lru_cache(maxsize=1)
def reference(stamps):
    value = reference_document(stamps[0])
    if any(
        not (DATA / name).is_file() or sha256(DATA / name) != expected
        for name, expected in value["sources"].items()
    ):
        raise HTTPException(503, "Zmieniono dane; porównanie miejskie wymaga aktualizacji")
    return value


def score(value, ref):
    if value is None or not ref["eligible"]:
        return None
    n = ref["known"]
    percentile = (
        100
        * sum(count * (1 if x < value else 0.5 if x == value else 0) for x, count in ref["counts"])
        / n
    )
    return 100 - percentile if ref["direction"] == "lower" else percentile


def green_share(longitude, latitude):
    path = DATA / "greenery-v36.sqlite"
    if not path.is_file():
        return "Brak danych"
    project = Transformer.from_crs(4326, 2180, always_xy=True).transform
    unproject = Transformer.from_crs(2180, 4326, always_xy=True).transform
    buffer = transform(project, Point(longitude, latitude)).buffer(500, quad_segs=32)
    w, s, e, n = transform(unproject, buffer).bounds
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(
            "SELECT a.properties,a.geometry FROM areas a JOIN area_bounds b ON a.rowid=b.id "
            "WHERE b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?",
            (e, w, n, s),
        )
        geometries = [
            transform(project, from_wkb(g))
            for p, g in rows
            if json.loads(p).get("kind") in ("park", "wood")
        ]
    percent = (
        100 * unary_union(geometries).intersection(buffer).area / buffer.area if geometries else 0
    )
    return f"≥{percent:.1f}% · częściowy katalog"


@lru_cache(maxsize=1)
def service_catalogue(path, modified, size):
    """Reuse the deduplicated snapshot; modification/size invalidate one cached index."""
    project = Transformer.from_crs(4326, 2180, always_xy=True)
    seen, result = set(), []
    for feature in json.loads(path.read_text(encoding="utf-8"))["features"]:
        props, geometry = feature["properties"], feature["geometry"]
        key = props["source_id"]
        if key in seen or props["kind"] not in ("groceries", "pharmacy"):
            continue
        seen.add(key)
        if geometry["type"] == "Point":
            x, y = project.transform(*geometry["coordinates"])
            result.append((props["kind"], x, y))
    return result


def service_fact(longitude, latitude):
    path = DATA / "osm-services.geojson"
    if not path.is_file():
        return "Katalog usług niedostępny"
    stat = path.stat()
    x, y = Transformer.from_crs(4326, 2180, always_xy=True).transform(longitude, latitude)
    counts = {"groceries": 0, "pharmacy": 0}
    for kind, px, py in service_catalogue(path, stat.st_mtime_ns, stat.st_size):
        if hypot(px - x, py - y) <= 500:
            counts[kind] += 1
    return f"Katalog 500 m: sklepy {counts['groceries']}, apteki {counts['pharmacy']}"


@router.get("/comfort")
def at(
    longitude: float = Query(ge=19, le=21),
    latitude: float = Query(ge=49, le=51),
    scope: Literal["all", "core", "facts"] = "all",
):
    if scope == 'facts':
        from services.api.place_signals import at as place_signals
        return place_signals(DATA,longitude,latitude)
    return point_facts(longitude, latitude, input_stamp(), scope)


@lru_cache(maxsize=128)
def point_facts(longitude, latitude, stamps, scope="all"):
    # Exact coordinates and every analytical input belong to the cache key.
    # At most128 fixed four-indicator responses; no decoded geometry is retained.
    ref = reference(stamps)
    with closing(sqlite3.connect(DATA / "noise-night.sqlite")) as db:
        db.row_factory = sqlite3.Row
        pair, _ = lookup(db, Point(longitude, latitude))
    ordinal = None if pair is None else 0 if pair[0] is None else 1 + int((pair[0] - 50) / 5)
    air = point_values("air", longitude, latitude)["matches"]
    air_values = {p["value"] for p in air if p["state"] == "known"}
    air_value = next(iter(air_values)) if len(air_values) == 1 else None
    night_fact = (
        "Brak danych"
        if pair is None
        else f"<{pair[1]:g} dB"
        if pair[0] is None
        else f"≥{pair[0]:g} dB"
        if pair[1] is None
        else f"{pair[0]:g}–{pair[1]:g} dB"
    )
    values = [
        ("night", "Cisza nocą", ordinal, night_fact),
        (
            "air",
            "Powietrze",
            air_value,
            f"{air_value:g} µg/m³" if air_value is not None else "Brak danych",
        ),
    ]
    return {
        "version": ref["version"],
        "reference_size": ref["reference_size"],
        "items": [
            {
                "id": key,
                "name": name,
                "fact": fact,
                "score": score(value, ref["references"][key]),
                "period": ref["references"][key]["period"],
                "coverage": ref["references"][key]["coverage"],
            }
            for key, name, value, fact in values
        ]
        + (
            []
            if scope == "core"
            else [
                {
                    "id": "green",
                    "name": "Parki i lasy",
                    "fact": green_share(longitude, latitude),
                    "score": None,
                    "period": "2026-10-11",
                    "coverage": None,
                },
                {
                    "id": "services",
                    "name": "Codzienne usługi",
                    "fact": service_fact(longitude, latitude),
                    "score": None,
                    "period": "2026-10-11",
                    "coverage": None,
                },
            ]
        ),
    }
