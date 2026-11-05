"""GTFS service-day normalization for a native single-date R5 task.

No routing is performed here. Source times are translated onto one absolute
timeline; previous/current/next service dates remain separately identifiable.
Calendar/time transformations are distinct from the native routing algorithm.
"""

import csv
import io
import json
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
from zoneinfo import ZoneInfo

from tools.data.acquire import ROOT, sha256
from tools.data.calendar import WEEKDAYS, active_service_ids, parse_time

WARSAW = ZoneInfo("Europe/Warsaw")
BASE_SECONDS = 86400
POLICY = "native-three-service-days-noon-v2"
METHOD = "r5-7.5.1-service-days-grid150-v4"
ROUTE_METHOD = "r5-native-service-days-hops-v4"


def anchor(day):
    # Subtract on the absolute timeline, not wall time across a DST transition.
    return datetime.combine(day, time(12), WARSAW).astimezone(UTC) - timedelta(hours=12)


def query_clock(departure):
    if departure.tzinfo is None:
        raise ValueError("An explicit departure offset is required")
    local = departure.astimezone(WARSAW)
    return BASE_SECONDS + int((departure.astimezone(UTC) - anchor(local.date())).total_seconds())


def service_variants(calendars, exceptions, days):
    variants = defaultdict(set)
    for target_day in days:
        for shift in (-1, 0, 1):
            source_day = target_day + timedelta(days=shift)
            offset = BASE_SECONDS + int((anchor(source_day) - anchor(target_day)).total_seconds())
            for service_id in active_service_ids(calendars, exceptions, source_day):
                variants[service_id, shift, offset].add(target_day)
    return variants


def table(archive, name):
    return list(csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8-sig")))


def csv_bytes(fields, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def identity(original, shift, offset):
    return f"{original}__wm_{shift + 1}_{offset}"


def expanded_feed(source: Path, target: Path, days):
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(source) as archive:
        if "frequencies.txt" in archive.namelist() and table(archive, "frequencies.txt"):
            raise ValueError("Frequency feeds need separate normalization proof")
        if "transfers.txt" in archive.namelist() and any(
            row.get("from_trip_id") or row.get("to_trip_id")
            for row in table(archive, "transfers.txt")
        ):
            raise ValueError("Trip-specific transfer rules need separate normalization proof")
        calendars = table(archive, "calendar.txt") if "calendar.txt" in archive.namelist() else []
        variants = service_variants(calendars, table(archive, "calendar_dates.txt"), days)
        by_service = defaultdict(list)
        for service_id, shift, offset in sorted(variants):
            by_service[service_id].append((shift, offset))
        trips = table(archive, "trips.txt")
        bounds = {}
        for row in csv.DictReader(
            io.TextIOWrapper(archive.open("stop_times.txt"), encoding="utf-8-sig")
        ):
            clocks = [
                parse_time(row[key]) for key in ("arrival_time", "departure_time") if row[key]
            ]
            if clocks:
                low, high = bounds.get(row["trip_id"], (min(clocks), max(clocks)))
                bounds[row["trip_id"]] = min(low, *clocks), max(high, *clocks)
        source_clock_max = max(high for _, high in bounds.values())
        if source_clock_max >= 2 * 86400:
            raise ValueError("Source trips require service dates older than the previous day")
        expanded_trips, trip_variants, lineage = [], {}, {}
        for row in trips:
            low, high = bounds[row["trip_id"]]
            trip_variants[row["trip_id"]] = [
                (shift, offset)
                for shift, offset in by_service[row["service_id"]]
                if any(
                    high + offset >= query_clock(datetime.combine(day, time(), WARSAW))
                    and low + offset <= BASE_SECONDS + 86400 + 2700
                    for day in variants[row["service_id"], shift, offset]
                )
            ]
            for shift, offset in trip_variants[row["trip_id"]]:
                if low + offset < 0:
                    raise ValueError("A relevant trip would need a negative GTFS clock")
                record = row | {
                    "trip_id": identity(row["trip_id"], shift, offset),
                    "service_id": identity(row["service_id"], shift, offset),
                }
                if record.get("block_id"):
                    record["block_id"] = identity(record["block_id"], shift, offset)
                expanded_trips.append(record)
                lineage[record["trip_id"]] = {
                    "trip_id": row["trip_id"],
                    "service_day_shift": shift,
                    "clock_offset": offset,
                }
        replacements = {
            "trips.txt": csv_bytes(list(trips[0]), expanded_trips),
            "calendar.txt": csv_bytes(
                ["service_id", *WEEKDAYS, "start_date", "end_date"],
                [
                    {
                        "service_id": identity(s, shift, offset),
                        **dict.fromkeys(WEEKDAYS, "0"),
                        "start_date": min(days).strftime("%Y%m%d"),
                        "end_date": max(days).strftime("%Y%m%d"),
                    }
                    for s, shift, offset in sorted(variants)
                ],
            ),
            "calendar_dates.txt": csv_bytes(
                ["service_id", "date", "exception_type"],
                [
                    {
                        "service_id": identity(s, shift, offset),
                        "date": day.strftime("%Y%m%d"),
                        "exception_type": "1",
                    }
                    for (s, shift, offset), active in sorted(variants.items())
                    for day in sorted(active)
                ],
            ),
        }
        with ZipFile(target, "w", compression=ZIP_DEFLATED, compresslevel=6) as output:
            for name in archive.namelist():
                if name not in {*replacements, "stop_times.txt"}:
                    info = ZipInfo(name, (2026, 10, 22, 0, 0, 0))
                    info.compress_type = ZIP_DEFLATED
                    output.writestr(info, archive.read(name))
            for name, content in replacements.items():
                info = ZipInfo(name, (2026, 10, 22, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                output.writestr(info, content)
            with output.open("stop_times.txt", "w") as stream:
                text = io.TextIOWrapper(stream, encoding="utf-8", newline="")
                reader = csv.DictReader(
                    io.TextIOWrapper(archive.open("stop_times.txt"), encoding="utf-8-sig")
                )
                writer = csv.DictWriter(text, fieldnames=reader.fieldnames, lineterminator="\n")
                writer.writeheader()
                for row in reader:
                    for shift, offset in trip_variants[row["trip_id"]]:
                        record = row | {"trip_id": identity(row["trip_id"], shift, offset)}
                        for key in ("arrival_time", "departure_time"):
                            if record[key]:
                                seconds = parse_time(record[key]) + offset
                                if not 0 <= seconds <= 72 * 3600:
                                    raise ValueError(
                                        "A relevant trip exceeds the native GTFS clock range"
                                    )
                                hours, rest = divmod(seconds, 3600)
                                minutes, seconds = divmod(rest, 60)
                                record[key] = f"{hours:02}:{minutes:02}:{seconds:02}"
                        writer.writerow(record)
                text.flush()
                text.detach()
    proof = {
        "policy": POLICY,
        "source_sha256": sha256(source),
        "source_clock_max_seconds": source_clock_max,
        "output_sha256": sha256(target),
        "trip_lineage": lineage,
        "dates": [day.isoformat() for day in days],
    }
    target.with_suffix(".json").write_text(
        json.dumps(proof, separators=(",", ":")), encoding="utf-8"
    )
    return proof


def prepare(root=ROOT):
    coverage = json.loads(
        (root / "data/manifests/gtfs-coverage-v1.json").read_text(encoding="utf-8")
    )
    days = [date.fromisoformat(day) for day in coverage["common_active_service_dates"]]
    if not days:
        raise ValueError("No common source service dates to normalize")
    inputs, lineage = [], {}
    for feed in "amt":
        target = root / f".cache/router/service-days-v2/ztp_gtfs_{feed}.zip"
        source = root / f"data/raw/ztp_gtfs_{feed}.zip"
        metadata = target.with_suffix(".json")
        proof = json.loads(metadata.read_text(encoding="utf-8")) if metadata.exists() else None
        if (
            proof is None
            or "source_clock_max_seconds" not in proof
            or proof["policy"] != POLICY
            or proof["source_sha256"] != sha256(source)
            or proof["dates"] != [day.isoformat() for day in days]
        ):
            proof = expanded_feed(source, target, days)
        if sha256(target) != proof["output_sha256"]:
            raise ValueError("Prepared multi-day feed differs from its verified provenance")
        inputs.append(target)
        for identity, record in proof["trip_lineage"].items():
            lineage[f"ztp_gtfs_{feed}:{identity}"] = record | {
                "source_trip_id": f"ztp_gtfs_{feed}:{record['trip_id']}"
            }
    return inputs, lineage, days


def normalize_task(task, departure):
    task.fromTime = query_clock(departure)
    task.toTime = task.fromTime + 60


def source_packet(packet, lineage, target_day):
    """Keep native clocks monotonic; add raw GTFS clocks and actual service dates."""
    packet["native_timeline_start"] = (
        anchor(target_day) - timedelta(seconds=BASE_SECONDS)
    ).isoformat()
    for witness in packet["witnesses"].values():
        for leg in witness["legs"]:
            source = lineage[leg["trip_id"]]
            leg["source_board_clock"] = leg["board_clock"] - source["clock_offset"]
            leg["source_alight_clock"] = leg["alight_clock"] - source["clock_offset"]
            leg["trip_id"] = source["source_trip_id"]
            leg["service_date"] = (
                target_day + timedelta(days=source["service_day_shift"])
            ).isoformat()
            alternatives = {
                lineage[identity]["source_trip_id"]: (
                    target_day + timedelta(days=lineage[identity]["service_day_shift"])
                ).isoformat()
                for identity in leg["equivalent_trip_ids"]
            }
            leg["equivalent_trip_ids"] = sorted(alternatives)
            if len(set(alternatives.values())) > 1:
                leg["equivalent_service_dates"] = alternatives
    for feature in packet["sections"]["features"]:
        properties = feature["properties"]
        source = lineage[properties["trip_id"]]
        properties["trip_id"] = source["source_trip_id"]
        properties["service_date"] = (
            target_day + timedelta(days=source["service_day_shift"])
        ).isoformat()
    return packet


def main():
    inputs, lineage, days = prepare()
    print(
        json.dumps(
            {
                "policy": POLICY,
                "days": len(days),
                "trips": len(lineage),
                "outputs": {path.name: sha256(path) for path in inputs},
            }
        )
    )


if __name__ == "__main__":
    main()
