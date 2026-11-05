"""Fixed250m raw indicators: classified land-cover area and source-model count."""

import json
import math
import sqlite3
from contextlib import closing
from functools import lru_cache
from typing import Literal

import numpy as np
import rasterio
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from rasterio.windows import Window, from_bounds
from shapely import box, covers, from_wkb, intersection, points, transform
from shapely.geometry import Point
from shapely.ops import unary_union

from services.api.locations import DATA
from tools.data.acquire import ROOT, sha256
from tools.data.cells import TO_METRIC, TO_WGS
from tools.release.response_cache import response_cache

router = APIRouter(prefix="/api")
RADIUS = 250
METHOD = "fixed250m-classified-cover-and-source-models-v37"
VALID = [10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100]


class LowVegetation(BaseModel):
    status: Literal["known", "partial", "unknown"]
    lower_percent: float = Field(ge=0, le=100)
    upper_percent: float = Field(ge=0, le=100)
    known_percent: float = Field(ge=0, le=100)
    classified_area_m2: float = Field(ge=0)
    missing_area_m2: float = Field(ge=0)
    source_year: Literal[2021] = 2021
    source_id: Literal["esa_worldcover_2021_v200"] = "esa_worldcover_2021_v200"
    classes: list[int] = [20, 30]
    pixel_size_m: Literal[10] = 10


class TreeModels(BaseModel):
    count: int = Field(ge=0)
    imported_area_percent: float = Field(ge=0, le=100)
    full_import_area: bool
    biological_inventory_complete: Literal[False] = False
    source_dates: dict[str, int]
    source_id: Literal["gugik_trees_1261_lod1_2023"] = "gugik_trees_1261_lod1_2023"


class Neighbourhood(BaseModel):
    schema_version: Literal["1"] = "1"
    method: Literal["fixed250m-classified-cover-and-source-models-v37"] = METHOD
    longitude: float
    latitude: float
    radius_m: Literal[250] = RADIUS
    area_m2: float
    footprint: dict
    dataset_version: str
    low: LowVegetation | None
    trees: TreeModels | None
    issues: list[str]


@lru_cache(maxsize=4)
def checked_asset(name, signature):
    manifest = json.loads((ROOT / f"data/manifests/{name}.json").read_text("utf-8"))
    if name == "low-vegetation-v37":
        if (
            manifest.get("source_year"),
            manifest.get("pixel_size_m"),
            manifest.get("radius_m"),
        ) != (2021, 10, 250):
            raise ValueError("Unexpected low-vegetation schema")
        location_path = DATA / "locations.sqlite"
        if city_version(location_path, location_path.stat().st_mtime_ns) != manifest.get(
            "city_sha256"
        ):
            raise ValueError("Neighbourhood source territory changed")
    elif manifest.get("part_identity_policy") != "paired-source-gml-object-ids-v36":
        raise ValueError("Tree model identities have not been verified")
    keys = ("raster", "display") if name == "low-vegetation-v37" else ("output",)
    for key in keys:
        path = DATA / manifest[key].split("/")[-1]
        if sha256(path) != manifest[key + "_sha256"]:
            raise ValueError("Neighbourhood asset does not match its manifest")
    return manifest


def asset(name):
    manifest = ROOT / f"data/manifests/{name}.json"
    files = [DATA / "low-vegetation-v37.tif", DATA / "low-vegetation-v37.sqlite"]
    if name == "greenery-v36":
        files = [DATA / "greenery-v36.sqlite"]
    signatures = tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in [manifest, *files])
    if name == "low-vegetation-v37":
        signatures += ((DATA / "locations.sqlite").stat().st_mtime_ns,)
    return checked_asset(name, signatures)


@lru_cache(maxsize=2)
def city_version(path, signature):
    return sha256(path)


@lru_cache(maxsize=2)
def city(path, signature):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return transform(
            unary_union([from_wkb(r[0]) for r in db.execute("SELECT geometry FROM districts")]),
            TO_METRIC.transform,
            interleaved=False,
        )


def raster_measure(path, circle):
    """Exact pixel/circle intersection on the declared10m metric categorical grid.

    Missing and out-of-window pixels stay unknown; never renormalise the
    denominator to the part that happens to have data.
    """
    with rasterio.open(path) as src:
        if src.crs.to_epsg() != 2180 or src.res != (10, 10) or src.nodata != 0:
            raise ValueError("Unexpected analytical grid")
        raw = from_bounds(*circle.bounds, src.transform)
        col, row = math.floor(raw.col_off), math.floor(raw.row_off)
        width = math.ceil(raw.col_off + raw.width) - col
        height = math.ceil(raw.row_off + raw.height) - row
        window = Window(col, row, width, height)
        values = src.read(1, window=window, boundless=True, fill_value=0)
        affine = src.window_transform(window)
    rows, cols = np.indices(values.shape)
    x = affine.c + cols * 10
    y = affine.f - rows * 10
    weights = intersection(box(x, y - 10, x + 10, y), circle)
    weights = np.array([g.area for g in weights.flat]).reshape(values.shape)
    total = circle.area
    known = float(weights[np.isin(values, VALID)].sum())
    low = float(weights[np.isin(values, [20, 30])].sum())
    missing = max(0, total - known)
    if missing < 1e-5:
        missing = 0
    return LowVegetation(
        status="unknown" if known < 1e-5 else "partial" if missing else "known",
        lower_percent=min(100, low / total * 100),
        upper_percent=min(100, (low + missing) / total * 100),
        known_percent=min(100, known / total * 100),
        classified_area_m2=low,
        missing_area_m2=missing,
    )


def tree_measure(path, circle, mask):
    extent = transform(circle, TO_WGS.transform, interleaved=False).bounds
    west, south, east, north = extent
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            """SELECT longitude,latitude,source_date FROM tree_bounds b
            JOIN trees t ON b.id=t.id WHERE b.minx<=? AND b.maxx>=? AND b.miny<=?
            AND b.maxy>=?""",
            (east, west, north, south),
        ).fetchall()
    dates = {}
    if rows:
        positions = transform(
            points(np.array([r[:2] for r in rows])), TO_METRIC.transform, interleaved=False
        )
        for row, inside in zip(rows, covers(circle, positions), strict=True):
            if inside:
                dates[row[2]] = dates.get(row[2], 0) + 1
    imported = min(100, circle.intersection(mask).area / circle.area * 100)
    return TreeModels(
        count=sum(dates.values()),
        imported_area_percent=imported,
        full_import_area=imported > 100 - 1e-7,
        source_dates=dates,
    )


@lru_cache(maxsize=64)
def calculate(longitude, latitude, low_version, tree_version, city_signature):
    path = DATA / "locations.sqlite"
    mask = city(path, city_signature)
    point = Point(*TO_METRIC.transform(longitude, latitude))
    if not mask.covers(point):
        raise HTTPException(422, "Wybierz punkt w Krakowie.")
    circle = point.buffer(RADIUS, quad_segs=64)
    issues, low, trees = [], None, None
    if low_version:
        low = raster_measure(DATA / "low-vegetation-v37.tif", circle)
    else:
        issues.append("low_vegetation_unavailable")
    if tree_version:
        trees = tree_measure(DATA / "greenery-v36.sqlite", circle, mask)
    else:
        issues.append("tree_models_unavailable")
    return Neighbourhood(
        longitude=longitude,
        latitude=latitude,
        area_m2=circle.area,
        footprint={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"meaning": "250m analytical circle, not a walking isochrone"},
                    "geometry": transform(
                        circle, TO_WGS.transform, interleaved=False
                    ).__geo_interface__,
                }
            ],
        },
        dataset_version=(
            f"{METHOD}:{low_version or 'missing'}:{tree_version or 'missing'}:"
            f"{city_version(path, city_signature)}"
        ),
        low=low,
        trees=trees,
        issues=issues,
    )


@router.get("/neighbourhood", response_model=Neighbourhood)
def neighbourhood(
    longitude: float = Query(ge=-180, le=180), latitude: float = Query(ge=-90, le=90)
):
    versions = []
    for name, key in (("low-vegetation-v37", "raster_sha256"), ("greenery-v36", "output_sha256")):
        try:
            versions.append(asset(name)[key])
        except FileNotFoundError:
            versions.append(None)
        except (ValueError, OSError, KeyError):
            raise HTTPException(503, "Wersja danych otoczenia jest niedostępna.") from None
    path = DATA / "locations.sqlite"
    try:
        return calculate(longitude, latitude, *versions, path.stat().st_mtime_ns)
    except (OSError, ValueError, sqlite3.Error):
        raise HTTPException(503, "Obliczenie otoczenia jest niedostępne.") from None


@response_cache()
def layer(version, west, south, east, north, zoom):
    features = []
    with closing(
        sqlite3.connect((DATA / "low-vegetation-v37.sqlite").as_uri() + "?mode=ro", uri=True)
    ) as db:
        rows = (
            db.execute(
                """SELECT f.id,f.class,f.geometry FROM features f JOIN bounds b
            ON f.id=b.id WHERE b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?
            ORDER BY f.id LIMIT 2001""",
                (east, west, north, south),
            ).fetchall()
            if zoom >= 13
            else []
        )
        for identifier, kind, wkb in rows[:2000]:
            features.append(
                {
                    "type": "Feature",
                    "id": identifier,
                    "properties": {"class": kind, "source_year": 2021},
                    "geometry": from_wkb(wkb).__geo_interface__,
                }
            )
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": features,
            "dataset_version": version,
            "source_year": 2021,
            "truncated": len(rows) > 2000,
            "minimum_zoom": 13,
            "display_only": True,
        },
        separators=(",", ":"),
    ).encode()


@router.get("/low-vegetation-layer")
def low_layer(
    west: float = Query(ge=-180, le=180),
    south: float = Query(ge=-90, le=90),
    east: float = Query(ge=-180, le=180),
    north: float = Query(ge=-90, le=90),
    zoom: int = Query(ge=9, le=18),
):
    if east <= west or north <= south or east - west > 2 or north - south > 1:
        raise HTTPException(422, "Wybierz poprawny widok.")
    try:
        manifest = asset("low-vegetation-v37")
    except (FileNotFoundError, OSError, ValueError):
        raise HTTPException(503, "Dane klas traw i krzewów są niedostępne.") from None
    return Response(
        layer(manifest["raster_sha256"], west, south, east, north, zoom),
        media_type="application/geo+json",
    )
