"""Local official-address index and exact point geometry lookup."""

import json
import os
import re
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict
from shapely import from_wkb
from shapely.geometry import Point

from services.api.cell_data import CellDetail, lookup
from services.api.noise import METHOD_VERSION, source_band
from tools.data.import_locations import normalize

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / os.environ.get("WM_DATA_DIR", "data/derived/generated")
router = APIRouter(prefix="/api")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Address(Model):
    id: str
    label: str
    longitude: float
    latitude: float


class AddressResults(Model):
    data_version: str
    results: list[Address]


class Metric(Model):
    criterion: Literal["transport", "quiet", "green", "services"]
    status: Literal["raw", "unavailable"]
    raw_value: str | None = None
    unit: str | None = None
    resolution: str
    explanation: str
    source_id: str | None = None
    score: None = None
    method_version: str | None = None
    lower_db: float | None = None
    upper_db: float | None = None


class Location(Model):
    data_version: str
    kind: Literal["address", "point", "district"]
    label: str
    longitude: float | None = None
    latitude: float | None = None
    district_ids: list[int]
    coverage: Literal["inside", "outside"]
    metrics: list[Metric]
    cell_detail: CellDetail | None = None


def database():
    path = DATA / "locations.sqlite"
    if not path.is_file():
        raise HTTPException(503, "Brak importu danych. Uruchom import opisany w RUNBOOK.md.")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def version(connection) -> str:
    return connection.execute("SELECT value FROM metadata WHERE key='version'").fetchone()[0]


def unavailable() -> list[Metric]:
    return [
        Metric(
            criterion="transport",
            status="unavailable",
            resolution="punkt / sieć piesza",
            explanation=(
                "Rozkłady pobrano. Dostęp do przystanków i liczenie unikalnyc"
                "h kursów czekają na weryfikację."
            ),
            source_id="ztp_gtfs_a",
        ),
        Metric(
            criterion="quiet",
            status="unavailable",
            resolution="poligon modelu, rok 2022",
            explanation=(
                "Brak jednoznacznego pasma modelu hałasu drogowego LDWN dla tej lokalizacji."
            ),
            source_id="msip_noise_1361_2022",
        ),
        Metric(
            criterion="green",
            status="unavailable",
            resolution="wejście do parku / sieć piesza",
            explanation=(
                "Wejścia i ich publiczny dostęp wymagają sprawdzenia. Brak da"
                "nych nie oznacza braku parku."
            ),
            source_id="osm",
        ),
        Metric(
            criterion="services",
            status="unavailable",
            resolution="wejście do obiektu / sieć piesza",
            explanation=(
                "Sklepy, apteki i przychodnie są w OSM. Nie zweryfikowano wsz"
                "ystkich dojść ani kompletności kategorii."
            ),
            source_id="osm",
        ),
    ]


@router.get("/addresses", response_model=AddressResults)
def addresses(
    q: str = Query(min_length=2, max_length=120), limit: int = Query(default=8, ge=1, le=20)
):
    tokens = re.findall(r"\w+", normalize(q), flags=re.UNICODE)
    with closing(database()) as connection:
        if not tokens:
            return AddressResults(data_version=version(connection), results=[])
        expression = " AND ".join(f'"{token}"*' for token in tokens)
        rows = connection.execute(
            """SELECT a.id,a.label,a.longitude,a.latitude FROM address_search s
                                     JOIN addresses a ON a.id=s.id WHERE address_search MATCH ?
                                     ORDER BY CASE WHEN a.normalized LIKE ? THEN 0 ELSE 1 END,
                                     length(a.label),a.label,a.id LIMIT ?""",
            (expression, normalize(q) + "%", limit),
        ).fetchall()
        return AddressResults(
            data_version=version(connection), results=[Address(**dict(row)) for row in rows]
        )


def geojson(name):
    path = DATA / name
    if not path.is_file():
        raise HTTPException(503, "Warstwa danych jest niedostępna.")
    return FileResponse(
        path, media_type="application/geo+json", headers={"Cache-Control": "no-cache"}
    )


@router.get("/districts")
def districts():
    return geojson("districts.geojson")


@router.get("/basemap")
def basemap():
    return geojson("basemap.geojson")


@router.get("/noise-layer")
def noise_layer():
    return geojson("noise-display.geojson")


@router.get("/noise-night-layer")
def noise_night_layer():
    return geojson("noise-night-display.geojson")


@router.get("/parks-layer")
def parks_layer():
    return geojson("osm-parks.geojson")


@router.get("/services-layer")
def services_layer():
    paths = [DATA / "osm-services.geojson", DATA / "osm-churches.geojson"]
    if not paths[1].is_file():
        return geojson("osm-services.geojson")
    stamps = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    return Response(merged_services(stamps), media_type="application/geo+json")


@lru_cache(maxsize=1)
def merged_services(stamps):
    primary, churches = [json.loads(Path(row[0]).read_text("utf-8")) for row in stamps]
    if primary.get("source_sha256") != churches["source_sha256"]:
        raise HTTPException(503, "Niezgodna wersja usług")
    primary["features"] += churches["features"]
    primary["churches_version"] = churches["data_version"]
    return json.dumps(primary, ensure_ascii=False, separators=(",", ":")).encode()


@router.get("/family-layer")
def family_layer():
    return geojson(
        "family-ux.geojson" if (DATA / "family-ux.geojson").is_file() else "osm-family.geojson"
    )


@router.get("/mobility-layer")
def mobility_layer():
    return geojson(
        "mobility-ux.geojson"
        if (DATA / "mobility-ux.geojson").is_file()
        else "osm-mobility.geojson"
    )


@router.get("/poi-layer")
def poi_layer():
    return geojson("osm-pois.geojson")


@router.get("/sources")
def sources():
    path = DATA / "sources.json"
    if not path.is_file():
        raise HTTPException(503, "Metadane źródeł są niedostępne.")
    result = json.loads(path.read_text(encoding="utf-8"))
    runtime = json.loads((ROOT / "data/manifests/router-runtime.json").read_text(encoding="utf-8"))
    result["sources"].append(
        {
            "source_id": "osm",
            "title": "OpenStreetMap — Małopolskie",
            "landing_url": runtime["osm_landing_url"],
            "produced_at": "2026-10-11",
            "produced_period": None,
            "acquired_at": runtime["osm_acquired_at"],
            "terms_url": runtime["osm_terms_url"],
            "terms_status": "ODbL-1.0",
            "attribution": runtime["osm_attribution"],
            "source_crs": "EPSG:4326",
        }
    )
    for feed in ("a", "m", "t"):
        path = ROOT / f"data/manifests/ztp_gtfs_{feed}.json"
        result["sources"].append(json.loads(path.read_text(encoding="utf-8")))
    tree_source = ROOT / "data/manifests/gugik-trees-source-v35.json"
    if (ROOT / "data/manifests/gugik-trees-source-v36.json").is_file():
        tree_source = ROOT / "data/manifests/gugik-trees-source-v36.json"
    if tree_source.is_file():
        result["sources"].append(json.loads(tree_source.read_text(encoding="utf-8")))
    low_source = ROOT / "data/manifests/worldcover-source-v37.json"
    if low_source.is_file():
        result["sources"].append(json.loads(low_source.read_text(encoding="utf-8")))
    return result


@router.get("/districts/{district_id}", response_model=Location)
def district_location(district_id: int):
    with closing(database()) as connection:
        row = connection.execute("SELECT name FROM districts WHERE id=?", (district_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Nie znaleziono dzielnicy.")
        metrics = unavailable()
        for metric in metrics:
            metric.resolution = "dzielnica — agregacja nieobliczona"
            metric.explanation = (
                "Nie obliczono agregacji dla dzielnicy. Wybierz konkretny adres lub punkt."
            )
        return Location(
            data_version=version(connection),
            kind="district",
            label=row["name"],
            district_ids=[district_id],
            coverage="inside",
            metrics=metrics,
        )


@router.get("/locations", response_model=Location)
def location(
    address_id: str | None = Query(default=None, max_length=40),
    longitude: float | None = Query(default=None, ge=-180, le=180),
    latitude: float | None = Query(default=None, ge=-90, le=90),
):
    with closing(database()) as connection:
        if address_id is not None:
            if longitude is not None or latitude is not None:
                raise HTTPException(422, "Wybierz adres albo współrzędne.")
            row = connection.execute("SELECT * FROM addresses WHERE id=?", (address_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Nie znaleziono adresu.")
            longitude, latitude, label, kind = (
                row["longitude"],
                row["latitude"],
                row["label"],
                "address",
            )
        elif longitude is not None and latitude is not None:
            label, kind = "Wybrany punkt", "point"
        else:
            raise HTTPException(422, "Podaj adres albo obie współrzędne.")
        point = Point(longitude, latitude)
        district_ids = [
            r["id"]
            for r in connection.execute("SELECT * FROM districts ORDER BY id")
            if from_wkb(r["geometry"]).covers(point)
        ]
        metrics = unavailable()
        if district_ids:
            rows = connection.execute(
                """SELECT n.* FROM noise n JOIN noise_bounds b ON n.id=b.id
                                         WHERE b.minx<=? AND b.maxx>=?
                                         AND b.miny<=? AND b.maxy>=?""",
                (longitude, longitude, latitude, latitude),
            ).fetchall()
            bands = {
                (r["lower_db"], r["upper_db"])
                for r in rows
                if from_wkb(r["geometry"]).covers(point)
            }
            if len(bands) == 1:
                low, high = next(iter(bands))
                band = source_band(low, high)
                metrics[1] = Metric(
                    criterion="quiet",
                    status="raw" if band else "unavailable",
                    raw_value=band.label if band else None,
                    unit="dB LDWN",
                    resolution="dokładny poligon modelu drogowego, 2022",
                    explanation=(
                        "Pasmo ISOV1/ISOV2 potwierdzone oficjalną legendą, nie pomiar "
                        "przy mieszkaniu. Dostępna ocena komórki 250 m jest pokazana "
                        "osobno; uwzględniamy wyłącznie hałas drogowy."
                    )
                    if band
                    else "Pasmo nie odpowiada potwierdzonym klasom. Brak oceny.",
                    source_id="msip_noise_1361_2022",
                    method_version=METHOD_VERSION if band else None,
                    lower_db=band.lower if band else None,
                    upper_db=band.upper if band else None,
                )
            elif len(bands) > 1:
                metrics[
                    1
                ].explanation = (
                    "Punkt na granicy kilku pasm. Nie przypisano jednej wartości ani oceny."
                )
        return Location(
            data_version=version(connection),
            kind=kind,
            label=label,
            longitude=longitude,
            latitude=latitude,
            district_ids=district_ids,
            coverage="inside" if district_ids else "outside",
            metrics=metrics,
            cell_detail=lookup(longitude, latitude, version(connection)) if district_ids else None,
        )
