"""Read-only, independently sourced green land and bounded high-tree model views."""

import json
import math
import sqlite3
from contextlib import closing
from functools import lru_cache

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from shapely import from_wkb, transform
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

from services.api.locations import DATA, ROOT
from tools.data.acquire import sha256
from tools.data.cells import TO_WGS
from tools.release.response_cache import response_cache

router = APIRouter(prefix="/api")
MAX_TREES = 5000
MAX_AREAS = 4000
MAX_GROUPS = 8000
MANIFEST = ROOT / "data/manifests/greenery-v35.json"


@lru_cache(maxsize=2)
def verified(path, signature, manifest, manifest_signature):
    expected = json.loads(manifest.read_text(encoding="utf-8"))["output_sha256"]
    if sha256(path) != expected:
        raise HTTPException(503, "Wersja warstwy zieleni nie odpowiada manifestowi.")


def index():
    path = DATA / "greenery-v36.sqlite"
    if not path.is_file():
        path = DATA / "greenery-v35.sqlite"
    if not path.is_file():
        raise HTTPException(503, "Dane zieleni i modeli drzew są niedostępne.")
    manifest = (
        ROOT / "data/manifests/greenery-v36.json"
        if path.name == "greenery-v36.sqlite"
        else MANIFEST
    )
    if not manifest.is_file():
        raise HTTPException(503, "Manifest zieleni jest niedostępny.")
    verified(path, path.stat().st_mtime_ns, manifest, manifest.stat().st_mtime_ns)
    return path


@lru_cache(maxsize=2)
def dataset_version(path, signature):
    manifest = (
        ROOT / "data/manifests/greenery-v36.json"
        if path.name == "greenery-v36.sqlite"
        else MANIFEST
    )
    return json.loads(manifest.read_text("utf-8"))["output_sha256"]


@lru_cache(maxsize=2)
def metadata(path, signature):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return json.loads(
            db.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()[0]
        )


@lru_cache(maxsize=2)
def import_mask(path, signature):
    coverage = metadata(path, signature).get("coverage", {}).get("features", [])
    return shape(coverage[0]["geometry"]) if coverage else None


def view_covered(path, signature, west, south, east, north):
    mask = import_mask(path, signature)
    return mask is not None and mask.covers(box(west, south, east, north))


@router.get("/greenery-metadata")
def greenery_metadata():
    path = index()
    value = metadata(path, path.stat().st_mtime_ns)
    if path.name == "greenery-v36.sqlite":
        value = value | {"coverage": file_coverage(path, path.stat().st_mtime_ns)}
    return value | {"dataset_version": dataset_version(path, path.stat().st_mtime_ns)}


@lru_cache(maxsize=2)
def file_coverage(path, signature):
    """Approximate model-file extents, not a surveyed complete inventory footprint."""
    manifest = json.loads((ROOT / "data/manifests/greenery-v36.json").read_text("utf-8"))
    boxes = []
    for member in manifest["members"]:
        if not member["counts"].get("city_models"):
            continue
        extent = member["model_extent_epsg2180"]
        low, high = extent["lowerCorner"], extent["upperCorner"]
        if len(low) != 3 or len(high) != 3 or not all(math.isfinite(v) for v in low + high):
            raise HTTPException(503, "Niezweryfikowany zakres plików modeli.")
        boxes.append(box(low[0], low[1], high[0], high[1]))
    mask = metadata(path, signature)["coverage"]["features"][0]
    extent = transform(unary_union(boxes).simplify(5), TO_WGS.transform, interleaved=False)
    extent = extent.intersection(shape(mask["geometry"])).buffer(0)
    return {
        "type": "FeatureCollection",
        "features": [
            mask | {"properties": {"kind": "import_mask"}},
            {
                "type": "Feature",
                "properties": {"kind": "file_model_extent"},
                "geometry": mapping(extent),
            },
        ],
    }


@response_cache()
def land(path, signature, west, south, east, north, zoom, parks_only=False):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            """SELECT a.id,a.properties,a.geometry FROM area_bounds b
            JOIN areas a ON b.id=a.rowid WHERE b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?
            AND (? >=14 OR json_extract(a.properties,'$.kind') IN ('wood','park'))
            AND (?=0 OR json_extract(a.properties,'$.kind') IN ('wood','park'))
            ORDER BY a.id LIMIT ?""",
            (east, west, north, south, zoom, int(parks_only), MAX_AREAS + 1),
        ).fetchall()
        features = [
            {
                "type": "Feature",
                "id": row[0],
                "properties": json.loads(row[1]),
                "geometry": mapping(from_wkb(row[2])),
            }
            for row in rows[:MAX_AREAS]
        ]
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": features,
            "source_id": "osm",
            "source_sha256": metadata(path, signature)["osm_sha256"],
            "walking_access_confirmed": False,
            "display_only": True,
            "truncated": len(rows) > MAX_AREAS,
            "dataset_version": dataset_version(path, signature),
        },
        separators=(",", ":"),
    ).encode()


@router.get("/greenery-layer")
def greenery_layer(
    west: float = Query(ge=-180, le=180),
    south: float = Query(ge=-90, le=90),
    east: float = Query(ge=-180, le=180),
    north: float = Query(ge=-90, le=90),
    zoom: int = Query(ge=9, le=18),
    parks_only: bool = False,
):
    if east <= west or north <= south or east - west > 2 or north - south > 1:
        raise HTTPException(422, "Wybierz poprawny widok zieleni.")
    path = index()
    return Response(
        land(path, path.stat().st_mtime_ns, west, south, east, north, zoom, parks_only),
        media_type="application/geo+json",
    )


@response_cache()
def trees(path, signature, west, south, east, north, zoom=18):
    if zoom < 16:
        return tree_groups(path, signature, west, south, east, north, zoom)
    part_column = "t.part_count" if metadata(path, signature).get("part_identity_policy") else "1"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            f"""SELECT t.id,t.source_id,t.longitude,t.latitude,t.height_m,
            t.source_date,{part_column}
            FROM tree_bounds b JOIN trees t ON b.id=t.id WHERE b.minx<=? AND b.maxx>=?
            AND b.miny<=? AND b.maxy>=? AND t.longitude BETWEEN ? AND ?
            AND t.latitude BETWEEN ? AND ? ORDER BY t.id LIMIT ?""",
            (east, west, north, south, west, east, south, north, MAX_TREES + 1),
        ).fetchall()
    features = [
        {
            "type": "Feature",
            "id": row[0],
            "properties": {
                "source_model_id": row[1],
                "height_m": row[4],
                "source_date": row[5],
                "source_id": "gugik_trees_1261_lod1_2023",
                "walking_access_confirmed": False,
                "representation": "model",
                "count": 1,
                "source_part_count": row[6],
            },
            "geometry": {"type": "Point", "coordinates": [row[2], row[3]]},
        }
        for row in rows[:MAX_TREES]
    ]
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": features,
            "truncated": len(rows) > MAX_TREES,
            "county_complete": False,
            "view_covered": view_covered(path, signature, west, south, east, north),
            "source_sha256": metadata(path, signature)["tree_source_sha256"],
            "source_id": "gugik_trees_1261_lod1_2023",
            "licence": "CC BY 4.0",
            "dataset_version": dataset_version(path, signature),
        },
        separators=(",", ":"),
    ).encode()


@router.get("/tree-layer")
def tree_layer(
    west: float = Query(ge=-180, le=180),
    south: float = Query(ge=-90, le=90),
    east: float = Query(ge=-180, le=180),
    north: float = Query(ge=-90, le=90),
    zoom: int = Query(default=18, ge=9, le=18),
):
    if east <= west or north <= south or east - west > 2 or north - south > 1:
        raise HTTPException(422, "Wybierz poprawny widok modeli drzew.")
    path = index()
    return Response(
        trees(path, path.stat().st_mtime_ns, west, south, east, north, zoom),
        media_type="application/geo+json",
    )


def tree_groups(path, signature, west, south, east, north, zoom):
    """Display groups, not a canopy/vegetation percentage or exact single-tree position."""
    step = 360 / 2**zoom / 16
    latitude_step = step * 0.64  # Display cells near Krakow, not analytical metric bins.
    part_column = "t.part_count" if metadata(path, signature).get("part_identity_policy") else "1"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            f"""SELECT min(t.id),avg(longitude),avg(latitude),max(height_m),
            group_concat(DISTINCT source_date),count(*),sum({part_column})
            FROM tree_bounds b JOIN trees t ON b.id=t.id
            WHERE b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?
            AND longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ?
            GROUP BY cast((longitude+180)/? AS INTEGER),cast((latitude+90)/? AS INTEGER)
            ORDER BY min(t.id) LIMIT ?""",
            (
                east,
                west,
                north,
                south,
                west,
                east,
                south,
                north,
                step,
                latitude_step,
                MAX_GROUPS + 1,
            ),
        ).fetchall()
    features = [
        {
            "type": "Feature",
            "id": row[0],
            "geometry": {"type": "Point", "coordinates": [row[1], row[2]]},
            "properties": {
                "source_id": "gugik_trees_1261_lod1_2023",
                "count": row[5],
                "source_part_count": row[6],
                "height_m": row[3],
                "height_meaning": "maximum model height in display group",
                "source_date": row[4],
                "representation": "group",
                "walking_access_confirmed": False,
            },
        }
        for row in rows[:MAX_GROUPS]
    ]
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": features,
            "truncated": len(rows) > MAX_GROUPS,
            "county_complete": False,
            "source_sha256": metadata(path, signature)["tree_source_sha256"],
            "source_id": "gugik_trees_1261_lod1_2023",
            "licence": "CC BY 4.0",
            "representation": "groups",
            "view_covered": view_covered(path, signature, west, south, east, north),
            "dataset_version": dataset_version(path, signature),
            "model_count": sum(f["properties"]["count"] for f in features),
        },
        separators=(",", ":"),
    ).encode()
