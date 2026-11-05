"""Serialize pinned R5 witnesses; routing remains native StreetRouter/FastRaptor.

The single frequency-free departure permits reading the final scheduled states.
No route is inferred from neighbouring stop times or a whole route-number shape.
"""

import csv
import hashlib
import io
import json
import sys
import time
from collections import Counter, OrderedDict
from zipfile import ZipFile

ROUTE_METHOD = "r5-native-scheduled-hops-v31"


def confirmed_wait(previous_clock, board_clock, walk_seconds):
    """Validate this native connection; never search for a replacement trip."""
    if walk_seconds < 0:
        raise ValueError("Negative walking duration")
    wait = board_clock - previous_clock - walk_seconds
    return wait if wait >= 60 else None


class ReachableRoutes:
    def __init__(self, network, root, walk_access=None, trip_lineage=None, witness=None):
        from pyproj import Transformer

        self.network = network._transport_network
        self.trip_lineage = trip_lineage
        self.transit = self.network.transitLayer
        self.project = Transformer.from_crs(4326, 2180, always_xy=True).transform
        self.trips, self.shapes, self.stops = {}, {}, {}
        if witness is not None:
            self.trips, self.shapes, self.stops = (
                witness[key] for key in ("trips", "shapes", "stops")
            )
        else:
            for feed in "amt":
                prefix = f"ztp_gtfs_{feed}"
                with ZipFile(root / f"data/raw/{prefix}.zip") as archive:

                    def rows(name):
                        return csv.DictReader(
                            io.TextIOWrapper(archive.open(name), encoding="utf-8-sig")
                        )

                    for row in rows("trips.txt"):
                        self.trips[f"{prefix}:{row['trip_id']}"] = row
                    for row in rows("stops.txt"):
                        self.stops[f"{prefix}:{row['stop_id']}"] = {
                            "name": row["stop_name"],
                            "coordinates": [float(row["stop_lon"]), float(row["stop_lat"])],
                        }
                    points = {}
                    for row in rows("shapes.txt"):
                        points.setdefault(row["shape_id"], []).append(
                            (
                                int(row["shape_pt_sequence"]),
                                float(row["shape_pt_lon"]),
                                float(row["shape_pt_lat"]),
                            )
                        )
                    for shape_id, values in points.items():
                        self.shapes[f"{prefix}:{shape_id}"] = [(x, y) for _, x, y in sorted(values)]
            if trip_lineage is not None:
                # Experimental normalized feeds retain original shapes/metadata;
                # never guess an original trip from an edited ID or nearby clock.
                self.trips = {
                    native_id: self.trips[source["source_trip_id"]]
                    for native_id, source in trip_lineage.items()
                }
        self.geometry_cache = {}
        self.transfer_cache = OrderedDict()
        # Immutable native pattern layout, bounded by the pinned graph. Active
        # services stay query-local; never reuse a weekday selection on Sunday.
        self.pattern_cache = {}
        self.walk_access = walk_access

    def pattern_layout(self, pattern_index):
        if pattern_index not in self.pattern_cache:
            pattern = self.transit.tripPatterns.get(pattern_index)
            positions = {}
            for index, stop in enumerate(pattern.stops):
                positions.setdefault(int(stop), []).append(index)
            self.pattern_cache[pattern_index] = (
                pattern,
                positions,
                tuple(str(value) for value in pattern.pickups),
                tuple(str(value) for value in pattern.dropoffs),
                tuple(pattern.tripSchedules),
            )
        return self.pattern_cache[pattern_index]

    def geometry(self, pattern_index, trip_id):
        """Reject missing/other-trip shapes, including native straight-hop fallbacks."""
        trip = self.trips.get(trip_id)
        if trip is None:
            return None
        prefix = trip_id.split(":", 1)[0]
        key = (pattern_index, prefix, trip["shape_id"])
        if key in self.geometry_cache:
            return self.geometry_cache[key]
        pattern = self.transit.tripPatterns.get(pattern_index)
        trip = self.trips.get(trip_id)
        if trip is None or pattern.shape is None:
            return None
        prefix = trip_id.split(":", 1)[0]
        source = self.shapes.get(f"{prefix}:{trip['shape_id']}")
        actual = [(float(c.x), float(c.y)) for c in pattern.shape.getCoordinates()]
        if (
            source is None
            or len(source) != len(actual)
            or any(
                abs(x - a) > 1e-7 or abs(y - b) > 1e-7
                for (x, y), (a, b) in zip(source, actual, strict=True)
            )
        ):
            self.geometry_cache[key] = None
            return None
        hops = [
            [[float(c.x), float(c.y)] for c in line.getCoordinates()]
            for line in pattern.getHopGeometries(self.transit)
        ]
        from shapely.geometry import Point

        for i, coordinates in enumerate(hops):
            source_points = [
                self.stops[str(self.transit.stopIdForIndex.get(int(pattern.stops[j])))][
                    "coordinates"
                ]
                for j in (i, i + 1)
            ]
            if (
                len(coordinates) < 2
                or max(
                    Point(self.project(*coordinate)).distance(Point(self.project(*source_point)))
                    for coordinate, source_point in zip(
                        (coordinates[0], coordinates[-1]), source_points, strict=True
                    )
                )
                > 30
            ):
                hops[i] = None
        self.geometry_cache[key] = hops
        return hops

    def compute(self, task):
        import com.conveyal.r5 as r5

        started = time.perf_counter()
        stages = {}
        native = self.transit
        if native.hasFrequencies or int(task.toTime - task.fromTime) != 60:
            raise ValueError("Witness extraction requires one exact scheduled minute")
        street = r5.streets.StreetRouter(self.network.streetLayer)
        street.profileRequest = task
        street.streetMode = r5.profile.StreetMode.WALK
        street.quantityToMinimize = r5.streets.StreetRouter.State.RoutingVariable.DURATION_SECONDS
        street.timeLimitSeconds = min(2700, int(task.getMaxTimeSeconds(r5.profile.StreetMode.WALK)))
        if not street.setOrigin(task.fromLat, task.fromLon):
            raise ValueError("No native WALK origin")
        street.route()
        access = street.getReachedStops()
        worker = r5.profile.FastRaptorWorker(native, task, access)
        worker.retainPaths = True
        durations = worker.route()
        stages["native_search_seconds"] = time.perf_counter() - started
        if len(durations) != 1 or worker.pathsPerIteration.size() != 1:
            raise ValueError("Unexpected native departure iterations")
        # Pinned R5 7.5.1 internal scheduled states. Only read native predecessor
        # witnesses after the native search; never alter states or choose trips.
        field = worker.getClass().getDeclaredField("scheduleState")
        field.setAccessible(True)
        final = field.get(worker)[int(task.maxRides)]
        active = native.getActiveServicesForDate(task.date)
        trips_by_leg, journeys, segments, omitted = {}, {}, {}, Counter()
        boarding_index = {}
        active_schedules = {}

        def leg(pattern_index, board_stop, alight_stop, board_clock, alight_clock):
            key = (pattern_index, board_stop, alight_stop, board_clock, alight_clock)
            if key in trips_by_leg:
                return trips_by_leg[key]
            pattern, positions, pickups, dropoffs, schedules = self.pattern_layout(pattern_index)
            if pattern_index not in active_schedules:
                active_schedules[pattern_index] = tuple(
                    schedule for schedule in schedules if active.get(schedule.serviceCode)
                )
            matches = []
            for b in positions.get(board_stop, ()):
                for a in positions.get(alight_stop, ()):
                    if a <= b:
                        continue
                    if pickups[b] != "SCHEDULED" or dropoffs[a] != "SCHEDULED":
                        omitted["conditional_boarding_or_alighting"] += 1
                        continue
                    if (pattern_index, b) not in boarding_index:
                        indexed = {}
                        for schedule in active_schedules[pattern_index]:
                            indexed.setdefault(int(schedule.departures[b]), []).append(schedule)
                        boarding_index[pattern_index, b] = indexed
                    for schedule in boarding_index[pattern_index, b].get(board_clock, []):
                        if int(schedule.arrivals[a]) == alight_clock:
                            trip_id = str(schedule.tripId)
                            geometry = self.geometry(pattern_index, trip_id)
                            if geometry is not None:
                                matches.append((trip_id, schedule, b, a, geometry))
            if not matches:
                omitted["unmatched_native_trip_or_shape"] += 1
                trips_by_leg[key] = None
                return None
            # Equivalent exact native pattern/clock witnesses; deterministic ID,
            # never search for an earlier/later replacement departure.
            trip_id, schedule, b, a, geometry = min(matches, key=lambda m: m[0])
            metadata = self.trips[trip_id]
            route = native.routes.get(pattern.routeIndex)
            result = {
                "trip_id": trip_id,
                "equivalent_trip_ids": sorted({m[0] for m in matches}),
                "route_id": str(pattern.routeId),
                "route_name": str(route.route_short_name),
                "mode": "TRAM" if int(route.route_type) in (0, 900) else "BUS",
                "direction": str(metadata.get("direction_id", "")),
                "headsign": metadata.get("trip_headsign", ""),
                "service_date": str(task.date),
                "board_stop": str(native.stopIdForIndex.get(board_stop)),
                "alight_stop": str(native.stopIdForIndex.get(alight_stop)),
                "board_clock": board_clock,
                "alight_clock": alight_clock,
                "hops": [
                    {
                        "from_stop": str(native.stopIdForIndex.get(int(pattern.stops[i]))),
                        "to_stop": str(native.stopIdForIndex.get(int(pattern.stops[i + 1]))),
                        "departure_clock": int(schedule.departures[i]),
                        "arrival_clock": int(schedule.arrivals[i + 1]),
                        "alighting": str(pattern.dropoffs[i + 1]) == "SCHEDULED",
                        "coordinates": geometry[i],
                    }
                    for i in range(b, a)
                ],
            }
            trips_by_leg[key] = result
            return result

        reconstruction_started = time.perf_counter()
        for target, duration in enumerate(durations[0]):
            if not 0 <= int(duration) <= 2700 or worker.pathsPerIteration.get(0)[target] is None:
                continue
            state, stop, legs = final, target, []
            while state.previous is not None:
                if int(state.previous.bestNonTransferTimes[stop]) == int(
                    state.bestNonTransferTimes[stop]
                ):
                    state = state.previous
                    continue
                pattern_index, board = (
                    int(state.previousPatterns[stop]),
                    int(state.previousStop[stop]),
                )
                arrival = int(state.bestNonTransferTimes[stop])
                boarding = arrival - int(state.previousInVehicleTravelTime[stop])
                value = leg(pattern_index, board, stop, boarding, arrival)
                if value is None:
                    legs = []
                    break
                legs.append(
                    value
                    | {
                        "wait_seconds": int(state.previousWaitTime[stop]),
                        "ready_clock": int(state.previous.bestTimes[board]),
                    }
                )
                state, stop = state.previous, board
                if int(state.transferStop[stop]) != -1:
                    stop = int(state.transferStop[stop])
            if not legs:
                continue
            legs.reverse()
            start = int(task.fromTime)
            if legs[0]["board_clock"] - start != int(access.get(stop)) + legs[0]["wait_seconds"]:
                raise ValueError("Native access/wait clock mismatch")
            witness_id = f"r5:{target}"
            journeys[witness_id] = {
                "access_seconds": int(access.get(stop)),
                "arrival_seconds": int(duration),
                "legs": legs,
            }
        stages["witness_reconstruction_seconds"] = time.perf_counter() - reconstruction_started
        walks, transfers, access_geometries = {}, {}, {}

        def path_geometry(state):
            if state is None:
                return None
            if int(state.getDurationSeconds()) == 0:
                return ([], 0)
            path = r5.profile.StreetPath(state, self.network, False)
            segment = r5.api.util.StreetSegment(
                path, r5.api.util.LegMode.WALK, self.network.streetLayer
            )
            if segment.geometry is None:
                return None
            coordinates = [[float(c.x), float(c.y)] for c in segment.geometry.getCoordinates()]
            if self.walk_access is not None and not self.walk_access.allows(coordinates, 0.02):
                return None
            return (coordinates, int(state.getDurationSeconds())) if len(coordinates) >= 2 else None

        walking_started = time.perf_counter()
        stop_indices = {str(x): i for i, x in enumerate(native.stopIdForIndex)}
        for witness_id in sorted(journeys):
            witness = journeys[witness_id]
            accepted = []
            for index, item in enumerate(witness["legs"]):
                board = stop_indices[item["board_stop"]]
                if index == 0:
                    if board not in access_geometries:
                        access_geometries[board] = path_geometry(
                            street.getStateAtVertex(int(native.streetVertexForStop.get(board)))
                        )
                    geometry = access_geometries[board]
                    end_seconds, allocated, role = (
                        witness["access_seconds"],
                        item["board_clock"] - int(task.fromTime) - 60,
                        "access",
                    )
                else:
                    previous = witness["legs"][index - 1]
                    allocated = item["board_clock"] - previous["alight_clock"] - 60
                    if allocated < 0:
                        raise ValueError("Non-monotonic native transfer")
                    end_seconds = item["ready_clock"] - int(task.fromTime)
                    role = "transfer"
                    key = (previous["alight_stop"], item["board_stop"])
                    if key[0] == key[1]:
                        geometry = ([], 0)
                    elif key not in self.transfer_cache:
                        walk = r5.streets.StreetRouter(self.network.streetLayer)
                        walk.profileRequest = task
                        walk.streetMode = r5.profile.StreetMode.WALK
                        walk.quantityToMinimize = (
                            r5.streets.StreetRouter.State.RoutingVariable.DURATION_SECONDS
                        )
                        walk.timeLimitSeconds = 2700
                        walk.toVertex = int(native.streetVertexForStop.get(board))
                        walk.setOrigin(
                            int(
                                native.streetVertexForStop.get(
                                    stop_indices[previous["alight_stop"]]
                                )
                            )
                        )
                        walk.route()
                        self.transfer_cache[key] = path_geometry(
                            walk.getStateAtVertex(walk.toVertex)
                        )
                        while len(self.transfer_cache) > 256:
                            self.transfer_cache.popitem(last=False)
                    if key[0] != key[1]:
                        self.transfer_cache.move_to_end(key)
                        geometry = self.transfer_cache[key]
                if geometry is None or geometry[1] > allocated:
                    omitted["native_path_prefix_cut_at_unconfirmed_walk"] += 1
                    break
                previous_clock = int(task.fromTime) if index == 0 else previous["alight_clock"]
                wait = confirmed_wait(previous_clock, item["board_clock"], geometry[1])
                if wait is None:
                    raise ValueError("Unverified connection reached serialization")
                end_seconds = previous_clock + geometry[1] - int(task.fromTime)
                item["walk_seconds"] = geometry[1]
                item["verified_wait_seconds"] = wait
                accepted.append(item)
                if index > 0:
                    transfer_id = f"{previous['alight_stop']}:{item['board_stop']}"
                    if (
                        transfer_id not in transfers
                        or transfers[transfer_id]["properties"]["cumulative_seconds"] > end_seconds
                    ):
                        transfers[transfer_id] = {
                            "type": "Feature",
                            "id": transfer_id,
                            "geometry": {
                                "type": "Point",
                                "coordinates": self.stops[item["board_stop"]]["coordinates"],
                            },
                            "properties": {
                                "id": transfer_id,
                                "witness_id": witness_id,
                                "cumulative_seconds": end_seconds,
                                "walk_seconds": geometry[1],
                                "name": self.stops[item["board_stop"]]["name"],
                            },
                        }
                if len(geometry[0]) < 2:
                    continue
                identity = hashlib.sha256(json.dumps([geometry[0], role]).encode()).hexdigest()[:20]
                if (
                    identity in walks
                    and walks[identity]["properties"]["cumulative_seconds"] <= end_seconds
                ):
                    continue
                walks[identity] = {
                    "type": "Feature",
                    "id": identity,
                    "geometry": {"type": "LineString", "coordinates": geometry[0]},
                    "properties": {
                        "id": identity,
                        "witness_id": witness_id,
                        "role": role,
                        "cumulative_seconds": end_seconds,
                        "walk_seconds": geometry[1],
                    },
                }
            witness["legs"] = accepted
            if accepted:
                witness["arrival_seconds"] = accepted[-1]["alight_clock"] - int(task.fromTime)
                witness["access_seconds"] = accepted[0]["walk_seconds"]
        stages["walk_geometry_seconds"] = time.perf_counter() - walking_started
        sections_started = time.perf_counter()
        hop_identities = {}
        for witness_id, witness in journeys.items():
            legs = witness["legs"]
            start = int(task.fromTime)
            for leg_index, item in enumerate(legs):
                for hop in item["hops"]:
                    seconds = hop["arrival_clock"] - start
                    if hop["coordinates"] is None:
                        omitted["hop_call_shape_alignment_over_30m"] += 1
                        continue
                    if not 0 <= seconds <= 2700 or len(hop["coordinates"]) < 2:
                        continue
                    identity_key = (
                        item["route_id"],
                        item["direction"],
                        hop["from_stop"],
                        hop["to_stop"],
                        id(hop["coordinates"]),
                    )
                    if identity_key not in hop_identities:
                        hop_identities[identity_key] = hashlib.sha256(
                            json.dumps(
                                [
                                    item["route_id"],
                                    item["direction"],
                                    hop["from_stop"],
                                    hop["to_stop"],
                                    hop["coordinates"],
                                ]
                            ).encode()
                        ).hexdigest()[:20]
                    identity = hop_identities[identity_key]
                    if (
                        identity in segments
                        and segments[identity]["properties"]["cumulative_seconds"] <= seconds
                    ):
                        continue
                    segments[identity] = {
                        "type": "Feature",
                        "id": identity,
                        "geometry": {"type": "LineString", "coordinates": hop["coordinates"]},
                        "properties": {
                            "id": identity,
                            "method": ROUTE_METHOD
                            if self.trip_lineage is None
                            else "r5-native-service-days-hops-v4",
                            "mode": item["mode"],
                            "route_id": item["route_id"],
                            "route_name": item["route_name"],
                            "direction": item["direction"],
                            "headsign": item["headsign"],
                            "trip_id": item["trip_id"],
                            "witness_id": witness_id,
                            "leg_index": leg_index,
                            "transfers": leg_index,
                            "from_stop": hop["from_stop"],
                            "to_stop": hop["to_stop"],
                            "to_name": self.stops[hop["to_stop"]]["name"],
                            "alighting": hop["alighting"],
                            "to_longitude": self.stops[hop["to_stop"]]["coordinates"][0],
                            "to_latitude": self.stops[hop["to_stop"]]["coordinates"][1],
                            "service_date": str(task.date),
                            "start_cumulative_seconds": hop["departure_clock"] - start,
                            "cumulative_seconds": seconds,
                            "band": next(n for n in (15, 30, 45) if seconds <= n * 60),
                        },
                    }
        used = {
            f["properties"]["witness_id"]
            for f in [*segments.values(), *walks.values(), *transfers.values()]
        }
        for witness in journeys.values():
            witness["legs"] = [
                {k: v for k, v in item.items() if k != "hops"} for item in witness["legs"]
            ]
        stages["section_serialization_seconds"] = time.perf_counter() - sections_started
        stages["total_seconds"] = time.perf_counter() - started
        print(
            json.dumps({"event": "route_stages", **stages}),
            file=sys.stderr,
            flush=True,
        )
        result = {
            "method": ROUTE_METHOD
            if self.trip_lineage is None
            else "r5-native-service-days-hops-v4",
            "sections": {
                "type": "FeatureCollection",
                "features": sorted(
                    segments.values(),
                    key=lambda f: (-f["properties"]["cumulative_seconds"], f["id"]),
                ),
            },
            "witnesses": {k: v for k, v in journeys.items() if k in used},
            "walks": {"type": "FeatureCollection", "features": list(walks.values())},
            "transfers": {"type": "FeatureCollection", "features": list(transfers.values())},
            "seconds": round(time.perf_counter() - started, 4),
            "regional_platforms": len(native.stopIdForIndex),
            "access_platforms": int(access.size()),
            "omitted": dict(omitted),
        }
        if self.trip_lineage is not None:
            from datetime import date

            from tools.router.service_days import source_packet

            return source_packet(result, self.trip_lineage, date.fromisoformat(str(task.date)))
        return result
