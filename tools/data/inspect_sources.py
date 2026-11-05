"""Validate acquired bytes and inspect actual data without asserting reuse permission."""

import argparse
import csv
import io
import json
import math
from collections import Counter
from typing import Literal
from zipfile import ZipFile

import ijson
from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt
from pyproj import Transformer
from shapely.geometry import shape

from tools.data.acquire import ROOT, SOURCES, sha256
from tools.data.calendar import active_service_ids, parse_date, parse_time


class Inspection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    source_id: str
    input_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_crs: str
    counts: dict[str, NonNegativeInt]
    details: dict
    issues: list[str]
    release_validated: bool = False


def require_crs(value: str) -> None:
    if value != "EPSG:2178":
        raise ValueError(f"Unsupported source CRS {value!r}; update transformation explicitly")


def inspect_geo(source_id: str, archive: ZipFile) -> dict:
    entries = [name for name in archive.namelist() if name.endswith(".geojson")]
    if "noise" in source_id:
        entries = [name for name in entries if name.endswith("halas_2022_imisja_dr_LDWN.geojson")]
    if len(entries) != 1:
        raise ValueError("Expected one explicitly selected GeoJSON layer")
    entry = entries[0]
    with archive.open(entry) as stream:
        crs = next(ijson.items(stream, "crs.properties.name"))
    require_crs(crs)
    transform = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    bounds = [math.inf, math.inf, -math.inf, -math.inf]
    identifiers = set()
    properties = set()
    geometries = Counter()
    values = {}
    invalid = empty = duplicate = count = 0
    with archive.open(entry) as stream:
        for feature in ijson.items(stream, "features.item", use_float=True):
            geometry = shape(feature["geometry"])
            count += 1
            empty += int(geometry.is_empty)
            invalid += int(not geometry.is_valid)
            geometries[geometry.geom_type] += 1
            identifier = feature.get("id", feature["properties"].get("id"))
            if identifier is None:
                raise ValueError("Feature without source identifier")
            duplicate += int(identifier in identifiers)
            identifiers.add(identifier)
            properties.update(feature["properties"])
            for key, value in feature["properties"].items():
                if len(values.setdefault(key, set())) < 25:
                    values[key].add(str(value))
            if not geometry.is_empty:
                x0, y0, x1, y1 = geometry.bounds
                if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
                    raise ValueError("Non-finite source geometry")
                bounds = [
                    min(bounds[0], x0),
                    min(bounds[1], y0),
                    max(bounds[2], x1),
                    max(bounds[3], y1),
                ]
    if count == 0:
        raise ValueError("Empty feature collection")
    dates = {
        name: archive.read(name).decode("utf-8-sig")
        for name in archive.namelist()
        if name.endswith("__Aktualnosc__.txt")
    }
    issues = []
    if invalid or empty or duplicate:
        issues.append(
            "Invalid, empty or duplicate geometries need explicit handling before derivation"
        )
    return {
        "source_crs": crs,
        "counts": {
            "features": count,
            "invalid_geometry": invalid,
            "empty_geometry": empty,
            "duplicate_ids": duplicate,
        },
        "details": {
            "entry": entry,
            "geometry_types": dict(geometries),
            "property_names": sorted(properties),
            "property_samples": {k: sorted(v) for k, v in values.items()},
            "source_bbox": bounds,
            "wgs84_bbox": transform.transform_bounds(*bounds),
            "export_generation_text": dates,
        },
        "issues": issues,
    }


def read_table(archive: ZipFile, name: str, required: set[str]) -> list[dict]:
    with archive.open(name) as stream:
        reader = csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig"))
        if not required <= set(reader.fieldnames or []):
            raise ValueError(f"{name}: missing required columns")
        return list(reader)


def inspect_gtfs(source_id: str, archive: ZipFile, reference_date: str) -> dict:
    stops = read_table(archive, "stops.txt", {"stop_id", "stop_lat", "stop_lon"})
    routes = read_table(archive, "routes.txt", {"route_id", "route_type"})
    trips = read_table(archive, "trips.txt", {"trip_id", "route_id", "service_id"})
    agencies = read_table(archive, "agency.txt", {"agency_timezone"})
    calendars = read_table(archive, "calendar.txt", {"service_id", "start_date", "end_date"})
    exceptions = read_table(archive, "calendar_dates.txt", {"service_id", "date", "exception_type"})
    feed_info = read_table(archive, "feed_info.txt", {"feed_publisher_name"})
    identifiers = {}
    for name, rows, key in [
        ("stops", stops, "stop_id"),
        ("routes", routes, "route_id"),
        ("trips", trips, "trip_id"),
        ("calendar", calendars, "service_id"),
    ]:
        identifiers[name] = {row[key] for row in rows}
        if len(identifiers[name]) != len(rows) or "" in identifiers[name]:
            raise ValueError(f"{name}: duplicate or empty identifiers")
    if any(row["agency_timezone"] != "Europe/Warsaw" for row in agencies):
        raise ValueError("Unexpected GTFS timezone")
    for row in stops:
        lat, lon = float(row["stop_lat"]), float(row["stop_lon"])
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("Invalid stop coordinates")
    services = identifiers["calendar"] | {row["service_id"] for row in exceptions}
    if any(
        row["route_id"] not in identifiers["routes"] or row["service_id"] not in services
        for row in trips
    ):
        raise ValueError("Invalid GTFS trip foreign key")
    count = after_midnight = 0
    with archive.open("stop_times.txt") as stream:
        reader = csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8-sig"))
        required = {"trip_id", "stop_id", "stop_sequence", "arrival_time", "departure_time"}
        if not required <= set(reader.fieldnames or []):
            raise ValueError("Missing stop_times columns")
        for row in reader:
            count += 1
            if (
                row["trip_id"] not in identifiers["trips"]
                or row["stop_id"] not in identifiers["stops"]
            ):
                raise ValueError("Invalid stop_times foreign key")
            for key in ("arrival_time", "departure_time"):
                if row[key]:
                    seconds = parse_time(row[key])
                    after_midnight += int(seconds >= 86400)
    dates = [parse_date(row[key]) for row in calendars for key in ("start_date", "end_date")]
    dates += [parse_date(row["date"]) for row in exceptions]
    day = parse_date(reference_date)
    active = active_service_ids(calendars, exceptions, day)
    active_trips = sum(row["service_id"] in active for row in trips)
    issues = [] if active_trips else ["No active trips on the reference date"]
    return {
        "source_crs": "EPSG:4326",
        "counts": {
            "stops": len(stops),
            "routes": len(routes),
            "trips": len(trips),
            "stop_times": count,
            "after_midnight_time_values": after_midnight,
            "calendar_exceptions": len(exceptions),
            "active_trips": active_trips,
        },
        "details": {
            "feed_info": feed_info,
            "agency_names": [r.get("agency_name") for r in agencies],
            "timezone": "Europe/Warsaw",
            "reference_date": day.isoformat(),
            "active_service_ids": sorted(active),
            "calendar_span": [min(dates).isoformat(), max(dates).isoformat()],
            "route_types": dict(Counter(row["route_type"] for row in routes)),
            "identifier_namespace": source_id,
        },
        "issues": issues,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-date", default="20261014")
    args = parser.parse_args()
    reports = []
    for source_id, *_ in SOURCES:
        raw = ROOT / "data/raw" / f"{source_id}.zip"
        manifest = json.loads(
            (ROOT / "data/manifests" / f"{source_id}.json").read_text(encoding="utf-8")
        )
        digest = sha256(raw)
        if digest != manifest["sha256"]:
            raise ValueError(f"{source_id}: input checksum mismatch")
        with ZipFile(raw) as archive:
            result = (
                inspect_geo(source_id, archive)
                if source_id.startswith("msip")
                else inspect_gtfs(source_id, archive, args.reference_date)
            )
        report = Inspection(source_id=source_id, input_sha256=digest, **result)
        reports.append(report.model_dump())
        print(
            json.dumps({"source_id": source_id, "counts": report.counts, "issues": report.issues}),
            flush=True,
        )
    report_path = ROOT / "data/manifests/inspection-v1.json"
    report_path.write_text(
        json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
