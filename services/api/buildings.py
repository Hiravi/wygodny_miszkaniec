"""Observed OSM building functions; no inferred age, condition or planning use."""

import json
import sqlite3
from contextlib import closing

from fastapi import APIRouter, HTTPException, Query
from shapely import from_wkb
from shapely.geometry import Point

from services.api.locations import DATA

router = APIRouter(prefix="/api")
VERSION = "osm-building-function-v3-13"
CLASSES = {
    "apartments": "apartments",
    "house": "single",
    "detached": "single",
    "semidetached_house": "single",
    "bungalow": "single",
    "terrace": "terrace",
    "retail": "commercial",
    "commercial": "commercial",
    "office": "commercial",
    "industrial": "industrial",
    "warehouse": "industrial",
    "school": "institution",
    "university": "institution",
    "college": "institution",
    "kindergarten": "institution",
    "hospital": "institution",
    "civic": "institution",
    "public": "institution",
    "church": "institution",
    "residential": "residential_unknown",
    "mixed_use": "mixed",
}


def building_class(value):
    return CLASSES.get(value, "unknown")


@router.get("/building-at")
def building_at(longitude: float = Query(ge=19, le=21), latitude: float = Query(ge=49, le=51)):
    path = DATA / "map-context.sqlite"
    if not path.is_file():
        raise HTTPException(503, "Dane zabudowy są niedostępne.")
    point = Point(longitude, latitude)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT f.osm_id,f.properties,f.geometry FROM features f JOIN bounds b ON f.id=b.id "
            "WHERE f.kind='building' AND b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?",
            (longitude, longitude, latitude, latitude),
        ).fetchall()
    matches = []
    for identity, props, wkb in rows:
        if from_wkb(wkb).covers(point):
            values = json.loads(props)
            matches.append(
                {
                    "id": identity,
                    "class": building_class(values.get("building")),
                    "building": values.get("building"),
                    "name": values.get("name"),
                }
            )
    return {
        "version": VERSION,
        "matches": matches,
        "coverage_complete": False,
        "source_date": "2026-10-11",
        "geometry": "Display footprints, simplified0.3m; unknown outside an explicit footprint",
    }
