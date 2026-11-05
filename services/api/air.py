"""GIOS station observations; bounded server caches, no address interpolation."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import lru_cache
from urllib.request import Request, urlopen

from fastapi import APIRouter, HTTPException

from services.api.locations import DATA

BASE = "https://api.gios.gov.pl/pjp-api/v1/rest/"
router = APIRouter(prefix="/api")


@router.get("/air-months")
def monthly_archive():
    path = DATA / "air-months.json"
    if not path.is_file():
        raise HTTPException(503, "Archiwum miesięczne niedostępne")
    return json.loads(path.read_text(encoding="utf-8"))


def fetch(path):
    with urlopen(
        Request(BASE + path, headers={"User-Agent": "WygodnyMieszkaniecPilot/0.3"}), timeout=10
    ) as response:
        return json.load(response)


def items(data, prefix="Lista"):
    return next((v for k, v in data.items() if k.startswith(prefix) and isinstance(v, list)), [])


@lru_cache(maxsize=2)
def stations(hour):
    data = fetch("station/findAll?size=500")
    if data.get("totalPages", 1) != 1:
        raise ValueError("Station catalogue pagination changed")
    result = [s for s in items(data) if s["Nazwa miasta"] == "Kraków"]
    if not result:
        raise ValueError("Krakow station catalogue unavailable")
    return result


def station_feature(station):
    identifier = station["Identyfikator stacji"]
    props = {
        "kind": "air_station",
        "station_code": station["Kod stacji"],
        "name": station["Nazwa stacji"],
        "source_id": f"gios/{identifier}",
        "source_ids": [f"gios/{identifier}"],
        "geometry_role": "source_point",
        "source_label": "GIOŚ · pomiar stacji",
        "index_name": "Brak danych",
        "index_level": None,
        "index_time": None,
    }
    try:
        index = fetch(f"aqindex/getIndex/{identifier}")["AqIndex"]
        props.update(
            index_name=index.get("Nazwa kategorii indeksu") or "Brak danych",
            index_level=index.get("Wartość indeksu"),
            index_time=index.get(
                "Data danych źródłowych, z których policzono wartość indeksu dla wskaźnika st"
            ),
        )
    except (OSError, ValueError, KeyError):
        props["index_name"] = "Dane niedostępne"
    return {
        "type": "Feature",
        "id": f"gios/{identifier}",
        "properties": props,
        "geometry": {
            "type": "Point",
            "coordinates": [float(station["WGS84 λ E"]), float(station["WGS84 φ N"])],
        },
    }


@lru_cache(maxsize=2)
def layer(bucket):
    with ThreadPoolExecutor(max_workers=4) as pool:
        features = list(pool.map(station_feature, stations(int(time.time() // 3600))))
    return {
        "type": "FeatureCollection",
        "data_version": "gios-stations-v3-11",
        "features": features,
        "source_date": datetime.now(UTC).isoformat(),
        "coverage_complete": False,
        "timezone": "Europe/Warsaw",
        "attribution": "GIOŚ, Państwowy Monitoring Środowiska",
    }


def latest(rows):
    # Null latest hour never becomes zero or a silently older current reading.
    return max(rows, key=lambda row: row["Data"]) if rows else None


@lru_cache(maxsize=64)
def station_data(identifier, bucket):
    known = stations(int(time.time() // 3600))
    if not any(s["Identyfikator stacji"] == identifier for s in known):
        raise HTTPException(404, "Stacja poza zakresem Krakowa.")
    sensors = items(fetch(f"station/sensors/{identifier}?size=500"))

    def reading(sensor):
        result = {
            "pollutant": sensor["Wskaźnik - kod"],
            "unit": "µg/m³",
            "value": None,
            "time_local": None,
            "status": "unavailable",
        }
        try:
            row = latest(
                items(fetch(f"data/getData/{sensor['Identyfikator stanowiska']}?size=500"))
            )
            if row:
                result.update(
                    value=row["Wartość"],
                    time_local=row["Data"],
                    status="observed" if row["Wartość"] is not None else "missing",
                )
        except (OSError, ValueError, KeyError):
            pass
        return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        readings = list(pool.map(reading, sensors))
    seasons = None
    path = DATA / "air-seasons.json"
    if path.is_file():
        archive = json.loads(path.read_text(encoding="utf-8"))
        code = next(s["Kod stacji"] for s in known if s["Identyfikator stacji"] == identifier)
        seasons = archive["stations"].get(code)
    return {
        "station_id": identifier,
        "seasons": seasons,
        "readings": readings,
        "timezone": "Europe/Warsaw",
        "retrieved_at": datetime.now(UTC).isoformat(),
        "source": "GIOŚ / PMŚ",
        "note": "Niezweryfikowane wyniki stacji; nie pomiar przy wybranym domu.",
    }


@router.get("/air-layer")
def air_layer():
    try:
        return layer(int(time.time() // 1800))
    except (OSError, ValueError, KeyError) as error:
        raise HTTPException(503, "Stacje GIOŚ chwilowo niedostępne.") from error


@router.get("/air-stations/{identifier}")
def air_station(identifier: int):
    try:
        return station_data(identifier, int(time.time() // 1800))
    except (OSError, ValueError, KeyError) as error:
        raise HTTPException(503, "Pomiary GIOŚ chwilowo niedostępne.") from error
