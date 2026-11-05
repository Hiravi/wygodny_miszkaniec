"""Expose service-day overlap that pinned FastRaptor's single-date task cannot combine."""

import csv
import hashlib
import io
import json
from datetime import date, timedelta
from zipfile import ZipFile

from tools.data.calendar import active_service_ids, parse_time


def overlap_windows(root, sources, days):
    key = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    path = root / f".cache/router/service-overlap-{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    cutoffs = {day: 0 for day in days}
    for feed in "amt":
        with ZipFile(root / f"data/raw/ztp_gtfs_{feed}.zip") as archive:

            def rows(name):
                return csv.DictReader(io.TextIOWrapper(archive.open(name), encoding="utf-8-sig"))

            trips = {r["trip_id"]: r["service_id"] for r in rows("trips.txt")}
            service_end = {}
            for row in rows("stop_times.txt"):
                clock = parse_time(row["departure_time"])
                if clock >= 86400:
                    service = trips[row["trip_id"]]
                    service_end[service] = max(service_end.get(service, 0), clock - 86400)
            calendars, exceptions = list(rows("calendar.txt")), list(rows("calendar_dates.txt"))
            for day in days:
                previous = date.fromisoformat(day) - timedelta(days=1)
                cutoff = max(
                    (
                        service_end.get(s, 0)
                        for s in active_service_ids(calendars, exceptions, previous)
                    ),
                    default=0,
                )
                cutoffs[day] = max(cutoffs[day], cutoff)
    path.write_text(json.dumps(cutoffs, indent=2), encoding="utf-8")
    return cutoffs


def crosses_service_day(local, previous_end, horizon_seconds=2700):
    clock = local.hour * 3600 + local.minute * 60 + local.second
    return (previous_end > 0 and clock <= previous_end) or clock + horizon_seconds >= 86400
