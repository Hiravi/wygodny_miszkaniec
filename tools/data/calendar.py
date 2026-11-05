"""GTFS calendar inspection helpers, not a transit routing implementation."""

from datetime import date, datetime

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_date(value: str) -> date:
    if len(value) != 8 or not value.isdigit():
        raise ValueError(f"Invalid GTFS date: {value!r}")
    return datetime.strptime(value, "%Y%m%d").date()


def parse_time(value: str) -> int:
    parts = value.split(":")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise ValueError(f"Invalid GTFS time: {value!r}")
    hours, minutes, seconds = map(int, parts)
    if not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError(f"Invalid GTFS time: {value!r}")
    return hours * 3600 + minutes * 60 + seconds


def active_service_ids(calendars: list[dict], exceptions: list[dict], day: date) -> set[str]:
    active = {
        row["service_id"]
        for row in calendars
        if parse_date(row["start_date"]) <= day <= parse_date(row["end_date"])
        and row[WEEKDAYS[day.weekday()]] == "1"
    }
    for row in exceptions:
        if row["exception_type"] not in {"1", "2"}:
            raise ValueError("Unknown calendar exception type")
        if parse_date(row["date"]) == day:
            if row["exception_type"] == "1":
                active.add(row["service_id"])
            else:
                active.discard(row["service_id"])
    return active
