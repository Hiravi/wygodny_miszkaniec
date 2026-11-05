"""Versioned evidence grades, independent of map filters, zoom and R5."""

import json
import math
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from pyproj import Transformer
from shapely import from_wkb, transform, unary_union
from shapely.geometry import Point

VERSION = "place-evidence-v2"
PROJECT = Transformer.from_crs(4326, 2180, always_xy=True).transform
INVERSE = Transformer.from_crs(2180, 4326, always_xy=True).transform
CATEGORIES = ("groceries", "pharmacy", "healthcare", "atm", "market")
POLICY = {
    "version": VERSION,
    "green_radius_m": 500,
    "green_search_m": 2000,
    "green_share_percent": [2, 5, 10, 20, 30],
    "green_distance_m": [150, 300, 600, 1000],
    "service_categories": CATEGORIES,
    "service_near_m": 500,
    "service_far_m": 1000,
    "stop_radius_m": 600,
    "stop_distance_m": [150, 300, 500, 800],
    "departures_per_hour": [0, 1, 3, 6, 12],
    "scheduled_hours": 12,
}


def stamp(path):
    try:
        s = path.stat()
        return str(path), s.st_mtime_ns, s.st_size
    except FileNotFoundError:
        return str(path), None, None


def grade(level):
    return (max(1, min(5, level)) - 1) * 20 + 10


def level_above(value, thresholds):
    return sum(value >= limit for limit in thresholds)


def level_distance(distance, limits):
    return 5 - next((i for i, limit in enumerate(limits) if distance <= limit), 4)


@lru_cache(maxsize=2)
def catalogue(key):
    path = Path(key[0])
    doc = json.loads(path.read_text("utf-8"))
    result = []
    seen = set()
    for f in doc["features"]:
        props = f["properties"]
        kind = props["kind"]
        identifier = props.get("source_id", f.get("id"))
        if kind not in CATEGORIES or f["geometry"]["type"] != "Point" or (identifier, kind) in seen:
            continue
        seen.add((identifier, kind))
        x, y = PROJECT(*f["geometry"]["coordinates"])
        result.append((kind, x, y))
    return result, doc.get("source_date", "")[:10]


def green(directory, point):
    path = directory / "greenery-v36.sqlite"
    if not path.is_file():
        return None, "—", "2026-10-11", None
    search = transform(point.buffer(2000), INVERSE, interleaved=False)
    w, s, e, n = search.bounds
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        parts = [
            transform(from_wkb(wkb), PROJECT, interleaved=False)
            for raw, wkb in db.execute(
                "SELECT a.properties,a.geometry FROM area_bounds b "
                "CROSS JOIN areas a ON a.rowid=b.id "
                "WHERE b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?",
                (e, w, n, s),
            )
            if json.loads(raw).get("kind") in ("park", "wood")
        ]
    if not parts:
        return None, "—", "2026-10-11", None
    geometry = unary_union(parts)
    distance = point.distance(geometry)
    if distance > 2000:
        return None, "—", "2026-10-11", None
    buffer = point.buffer(500)
    share = 100 * geometry.intersection(buffer).area / buffer.area
    # Positive observations are a lower bound. An empty partial catalogue is unknown.
    share_level = level_above(share, POLICY["green_share_percent"])
    near_level = level_distance(distance, POLICY["green_distance_m"])
    level = max(1, math.floor((share_level + near_level) / 2 + 0.5))
    return (
        grade(level),
        f"≥{share:.1f}% · {distance:.0f} m w linii prostej",
        "2026-10-11",
        "lower_bound",
    )


def services(directory, point):
    path = directory / "osm-services.geojson"
    if not path.is_file():
        return None, "—", "", None
    records, period = catalogue(stamp(path))
    nearest = {kind: math.inf for kind in CATEGORIES}
    for kind, x, y in records:
        nearest[kind] = min(nearest[kind], math.hypot(x - point.x, y - point.y))
    weights = [
        1 if d <= POLICY["service_near_m"] else 0.5 if d <= POLICY["service_far_m"] else 0
        for d in nearest.values()
    ]
    if not any(weights):
        return None, "—", period, None
    level = max(1, math.floor(sum(weights) + 0.5))
    return (
        grade(level),
        f"{sum(d <= 1000 for d in nearest.values())}/5 kategorii w promieniu 1 km",
        period,
        "lower_bound",
    )


def transport(directory, point):
    path = directory / "point-transit.sqlite"
    if not path.is_file():
        return None, "—", "", None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        meta = json.loads(db.execute("SELECT value FROM metadata").fetchone()[0])
        if set(meta.get("complete_feeds", [])) != set("amt"):
            return None, "—", meta.get("date", ""), None
        search = transform(point.buffer(2000), INVERSE, interleaved=False)
        w, s, e, n = search.bounds
        rows = db.execute(
            "SELECT a.id,a.longitude,a.latitude FROM bounds b CROSS JOIN stops a ON a.rowid=b.id "
            "WHERE b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?",
            (e, w, n, s),
        )
        distances = [
            (identifier, math.hypot(x - point.x, y - point.y))
            for identifier, lon, lat in rows
            for x, y in [PROJECT(lon, lat)]
        ]
        nearest = min((d for _, d in distances), default=math.inf)
        close = [identifier for identifier, d in distances if d <= POLICY["stop_radius_m"]]
        count = (
            db.execute(
                "SELECT count(DISTINCT trip) FROM departures WHERE stop_id IN ("
                + ",".join("?" * len(close))
                + ")",
                close,
            ).fetchone()[0]
            if close
            else 0
        )
    hourly = count / POLICY["scheduled_hours"]
    proximity = level_distance(nearest, POLICY["stop_distance_m"])
    frequency = level_above(hourly, POLICY["departures_per_hour"])
    label = (
        f"{hourly:.1f} kursów/h w promieniu 600 m"
        if math.isfinite(nearest)
        else "Przystanek poza promieniem 2 km"
    )
    return grade(min(proximity, frequency)), label, meta["date"], None


def at(directory, longitude, latitude):
    keys = tuple(
        stamp(directory / name)
        for name in ["greenery-v36.sqlite", "osm-services.geojson", "point-transit.sqlite"]
    )
    return cached(str(directory), longitude, latitude, keys)


@lru_cache(maxsize=128)
def cached(directory, longitude, latitude, keys):
    directory = Path(directory)
    point = Point(*PROJECT(longitude, latitude))
    items = []
    for identifier, name, fn in [
        ("green", "Parki i lasy", green),
        ("services", "Codzienne usługi", services),
        ("transport", "Komunikacja", transport),
    ]:
        value, fact, period, quality = fn(directory, point)
        items.append(
            {
                "id": identifier,
                "name": name,
                "score": value,
                "fact": fact,
                "period": period,
                "coverage": None,
                "quality": quality,
            }
        )
    return {"version": VERSION, "reference_size": 0, "items": items}
