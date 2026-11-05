"""Account for a returned itinerary's elapsed time without planning routes."""

from datetime import datetime


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Routing timestamps require an explicit UTC offset")
    return parsed


def summarize(node: dict, departure: str) -> dict:
    requested = instant(departure)
    start, end = instant(node["start"]), instant(node["end"])
    if start < requested or end < start:
        raise ValueError("Itinerary outside requested departure semantics")
    previous = start
    wait = (start - requested).total_seconds()
    travel = 0.0
    transit_legs = 0
    for leg in node["legs"]:
        begin = instant(leg["from"]["departure"]["scheduledTime"])
        finish = instant(leg["to"]["arrival"]["scheduledTime"])
        if begin < previous or finish < begin:
            raise ValueError("Non-monotonic itinerary leg timestamps")
        wait += (begin - previous).total_seconds()
        travel += (finish - begin).total_seconds()
        transit_legs += int(leg["mode"] not in {"WALK", "BICYCLE", "CAR"})
        previous = finish
    if not node["legs"] or previous > end:
        raise ValueError("Incomplete itinerary")
    wait += (end - previous).total_seconds()
    return {
        "elapsed_seconds": (end - requested).total_seconds(),
        "travel_seconds": travel,
        "waiting_seconds": wait,
        "transfers": max(0, transit_legs - 1),
    }
