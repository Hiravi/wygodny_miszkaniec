"""Cut an existing native journey path at ordered native calls; never plan or time routes."""

from shapely.geometry import LineString, Point
from shapely.ops import substring

from tools.data.cells import TO_METRIC, TO_WGS, decode_polyline
from tools.router.normalize import instant

ALIGNMENT_METRES = 30
PATH_METHOD = "native-calls-end-clock-v2"


def time_band(seconds):
    if seconds < 0:
        raise ValueError("Cumulative travel time cannot precede the requested origin minute")
    return next((cutoff for cutoff in (900, 1800, 2700) if seconds <= cutoff), None)


def journey_features(node, departure, journey_id):
    """Colour by the actual arrival at each segment end, conservatively within the segment."""
    origin_time = instant(departure)
    features, omitted = [], []
    for leg_index, leg in enumerate(node["legs"]):
        coordinates = decode_polyline(leg["legGeometry"]["points"])
        if len(coordinates) < 2:
            omitted.append({"leg_index": leg_index, "reason": "missing_native_line"})
            continue
        line = LineString([TO_METRIC.transform(*point) for point in coordinates])
        if line.length <= 0:
            omitted.append({"leg_index": leg_index, "reason": "zero_length_native_line"})
            continue
        endpoint_offset = max(
            Point(line.coords[0]).distance(
                Point(TO_METRIC.transform(leg["from"]["lon"], leg["from"]["lat"]))
            ),
            Point(line.coords[-1]).distance(
                Point(TO_METRIC.transform(leg["to"]["lon"], leg["to"]["lat"]))
            ),
        )
        if endpoint_offset > ALIGNMENT_METRES:
            omitted.append(
                {
                    "leg_index": leg_index,
                    "reason": "native_endpoint_off_path",
                    "maximum_offset_m": endpoint_offset,
                }
            )
            continue
        if leg["mode"] == "WALK":
            parts = [
                (
                    line,
                    leg["from"]["departure"]["scheduledTime"],
                    leg["to"]["arrival"]["scheduledTime"],
                    None,
                    None,
                )
            ]
            maximum_offset = endpoint_offset
        else:
            calls = leg["stopCalls"]
            points = [
                Point(TO_METRIC.transform(call["stopLocation"]["lon"], call["stopLocation"]["lat"]))
                for call in calls
            ]
            distances = [line.project(point) for point in points]
            maximum_offset = max(endpoint_offset, max(line.distance(point) for point in points))
            if maximum_offset > ALIGNMENT_METRES:
                omitted.append(
                    {
                        "leg_index": leg_index,
                        "reason": "native_call_off_path",
                        "maximum_offset_m": maximum_offset,
                    }
                )
                continue
            # Preserve the original native endpoints, not a new straight-line stop link.
            distances[0], distances[-1] = 0, line.length
            if any(end <= start for start, end in zip(distances, distances[1:], strict=False)):
                omitted.append({"leg_index": leg_index, "reason": "ambiguous_call_path_order"})
                continue
            parts = [
                (
                    substring(line, distances[i], distances[i + 1]),
                    calls[i]["schedule"]["time"]["departure"],
                    calls[i + 1]["schedule"]["time"]["arrival"],
                    calls[i],
                    calls[i + 1],
                )
                for i in range(len(calls) - 1)
            ]
        for index, (geometry, start, end, from_call, to_call) in enumerate(parts):
            seconds = (instant(end) - origin_time).total_seconds()
            start_seconds = (instant(start) - origin_time).total_seconds()
            if start_seconds < 0 or seconds < start_seconds:
                raise ValueError("Non-monotonic cumulative segment clock")
            features.append(
                {
                    "type": "Feature",
                    "id": f"{journey_id}:{leg_index}:{index}",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [TO_WGS.transform(*point) for point in geometry.coords],
                    },
                    "properties": {
                        "journey_id": journey_id,
                        "leg_index": leg_index,
                        "segment_index": index,
                        "mode": leg["mode"],
                        "method": PATH_METHOD,
                        "cumulative_seconds": seconds,
                        "start_cumulative_seconds": start_seconds,
                        "band_upper_seconds": time_band(seconds),
                        "arrival": end,
                        "departure": start,
                        "route_id": (leg["route"] or {}).get("gtfsId"),
                        "route_name": (leg["route"] or {}).get("shortName"),
                        "trip_id": (leg["trip"] or {}).get("gtfsId"),
                        "headsign": leg["headsign"],
                        "direction_id": (leg["trip"] or {}).get("directionId"),
                        "service_date": leg["serviceDate"],
                        "from_stop_id": from_call["stopLocation"]["gtfsId"]
                        if from_call
                        else (leg["from"].get("stop") or {}).get("gtfsId"),
                        "to_stop_id": to_call["stopLocation"]["gtfsId"]
                        if to_call
                        else (leg["to"].get("stop") or {}).get("gtfsId"),
                        "from_position": leg["from"]["stopPosition"]["position"] + index
                        if from_call
                        else None,
                        "to_position": leg["from"]["stopPosition"]["position"] + index + 1
                        if to_call
                        else None,
                        "maximum_call_offset_m": maximum_offset,
                    },
                }
            )
    return features, omitted
