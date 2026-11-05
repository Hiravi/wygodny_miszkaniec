"""Exact-point road LN lookup; missing/overlapping polygons never mean quiet."""

import sqlite3
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from shapely import from_wkb
from shapely.geometry import Point

from services.api.locations import DATA, database
from services.api.noise import NIGHT_METHOD_VERSION

router = APIRouter(prefix="/api")


class NightNoise(BaseModel):
    model_config = ConfigDict(extra="forbid")
    longitude: float
    latitude: float
    data_version: str
    model_year: Literal[2022] = 2022
    source_id: Literal["msip_noise_1361_2022"] = "msip_noise_1361_2022"
    method_version: Literal["road-ln-intervals-v1"] = NIGHT_METHOD_VERSION
    indicator: Literal["LN"] = "LN"
    unit: Literal["dB"] = "dB"
    status: Literal["raw", "unavailable"] = "unavailable"
    lower_db: float | None = None
    upper_db: float | None = None
    reason: Literal["known", "outside", "unmapped", "ambiguous"]


def lookup(connection, point):
    pairs = {
        (row["lower_db"], row["upper_db"])
        for row in connection.execute(
            """SELECT n.* FROM noise n JOIN noise_bounds b ON n.id=b.id
               WHERE b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?""",
            (point.x, point.x, point.y, point.y),
        )
        if from_wkb(row["geometry"]).covers(point)
    }
    return next(iter(pairs)) if len(pairs) == 1 else None, "ambiguous" if len(
        pairs
    ) > 1 else "unmapped"


@router.get("/noise-point", response_model=NightNoise)
def noise_point(longitude: float = Query(ge=-180, le=180), latitude: float = Query(ge=-90, le=90)):
    path = DATA / "noise-night.sqlite"
    if not path.is_file():
        raise HTTPException(503, "Dane hałasu nocnego są niedostępne.")
    point = Point(longitude, latitude)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        version = connection.execute("SELECT value FROM metadata WHERE key='version'").fetchone()[0]
        result = NightNoise(
            longitude=longitude, latitude=latitude, data_version=version, reason="outside"
        )
        with closing(database()) as city:
            if not any(
                from_wkb(row["geometry"]).covers(point)
                for row in city.execute("SELECT geometry FROM districts")
            ):
                return result
        pair, reason = lookup(connection, point)
        if pair is None:
            return result.model_copy(update={"reason": reason})
        return result.model_copy(
            update={"status": "raw", "reason": "known", "lower_db": pair[0], "upper_db": pair[1]}
        )
