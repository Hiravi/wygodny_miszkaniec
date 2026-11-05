"""Serialize native scheduled McRaptor point states and verified forward WALK.

Routing stays in pinned R5. Never infer a trip from a nearby timetable, reverse
an estimated walking duration, or manufacture a connector across a missing link.
"""

import math
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from shapely.geometry import LineString, Point
from shapely.ops import substring, transform

from tools.router.instant_point_runtime import POLICY
from tools.router.service_days import query_clock

WARSAW = ZoneInfo("Europe/Warsaw")
HORIZON = 2700


def coordinate(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in value
        )
        or not -180 <= value[0] <= 180
        or not -90 <= value[1] <= 90
    ):
        raise ValueError("Invalid point coordinate")
    return Point(*value)


class InvalidPath(ValueError):
    """The returned native candidate lacks a complete, legal path proof."""


class NativePoint:
    def __init__(self, network, routes, access, graph_version, *, sampler_verified=False):
        if not sampler_verified:
            raise ValueError("Exact scheduled point sampler must be verified before native routing")
        import com.conveyal.r5 as r5
        import jpype
        from pyproj import Transformer

        self.r5, self.jpype = r5, jpype
        self.network, self.native = network, network._transport_network
        self.transit = self.native.transitLayer
        if self.transit.hasFrequencies:
            raise ValueError("Exact point policy rejects stochastic frequency services")
        self.routes, self.access, self.graph_version = routes, access, graph_version
        self.project = routes.project
        self.unproject = Transformer.from_crs(2180, 4326, always_xy=True).transform

    def vertex_point(self, vertex):
        cursor = self.native.streetLayer.vertexStore.getCursor(vertex)
        return Point(float(cursor.getLon()), float(cursor.getLat()))

    def stop_vertex(self, stop):
        vertex = int(self.transit.streetVertexForStop.get(int(stop)))
        if vertex < 0:
            raise InvalidPath("Unlinked native platform")
        return vertex

    def walk(self, start, end, task, cache):
        """Route forward and clip only along the actual first/last native edges."""
        a = self.vertex_point(start) if isinstance(start, int) else start
        b = self.vertex_point(end) if isinstance(end, int) else end
        key = (a.x, a.y, b.x, b.y)
        if key in cache:
            return cache[key]
        walk = self.r5.streets.StreetRouter(self.native.streetLayer)
        request = task.clone()
        request.reverseSearch = False
        walk.profileRequest = request
        walk.streetMode = self.r5.profile.StreetMode.WALK
        walk.quantityToMinimize = (
            self.r5.streets.StreetRouter.State.RoutingVariable.DURATION_SECONDS
        )
        walk.timeLimitSeconds = HORIZON
        linked = walk.setOrigin(start) if isinstance(start, int) else walk.setOrigin(a.y, a.x)
        if linked is False:
            raise InvalidPath("Unlinked walking origin")
        if isinstance(end, int):
            walk.toVertex = end
        elif not walk.setDestination(b.y, b.x):
            raise InvalidPath("Unlinked walking destination")
        walk.route()
        state = (
            walk.getStateAtVertex(end)
            if isinstance(end, int)
            else walk.getState(walk.getDestinationSplit())
        )
        if state is None:
            raise InvalidPath("Missing native WALK")
        seconds = int(state.getDurationSeconds())
        path = self.r5.profile.StreetPath(state, self.native, False)
        edges = [int(edge) for edge in path.getEdges()]
        pa, pb = Point(*self.project(a.x, a.y)), Point(*self.project(b.x, b.y))
        if not edges:
            if pa.distance(pb) > 0.02:
                raise InvalidPath("Walking gap without native edges")
            coordinates, distance = [[a.x, a.y], [a.x, a.y]], 0.0
        else:
            segment = self.r5.api.util.StreetSegment(
                path, self.r5.api.util.LegMode.WALK, self.native.streetLayer
            )
            line = transform(
                self.project,
                LineString([(float(c.x), float(c.y)) for c in segment.geometry.getCoordinates()]),
            )
            for point, edge in ((pa, edges[0]), (pb, edges[-1])):
                geometry = self.native.streetLayer.edgeStore.getCursor(edge).getGeometry()
                edge_line = transform(
                    self.project,
                    LineString([(float(c.x), float(c.y)) for c in geometry.getCoordinates()]),
                )
                if point.distance(edge_line) > 0.02:
                    raise InvalidPath("WALK endpoint is not on its native boundary edge")
            clipped = substring(line, line.project(pa), line.project(pb))
            if clipped.geom_type != "LineString":
                if pa.distance(pb) > 0.02:
                    raise InvalidPath("Degenerate native WALK")
                coordinates, distance = [[a.x, a.y], [a.x, a.y]], 0.0
            else:
                distance = clipped.length
                coordinates = [list(c) for c in transform(self.unproject, clipped).coords]
            if not self.access.allows(coordinates, 0.02):
                raise InvalidPath("Unverified mapped WALK access")
        result = {"seconds": seconds, "coordinates": coordinates, "distance_m": distance}
        cache[key] = result
        return result

    @staticmethod
    def walk_leg(walk, start, from_name, to_name):
        return {
            "mode": "WALK",
            "start_seconds": start,
            "end_seconds": start + walk["seconds"],
            "from_name": from_name,
            "to_name": to_name,
            "route_id": None,
            "route_name": None,
            "trip_id": None,
            "service_date": None,
            "distance_m": walk["distance_m"],
            "coordinates": walk["coordinates"],
        }

    def ride_leg(self, state, task, day):
        index = int(state.pattern)
        pattern = self.transit.tripPatterns.get(index)
        schedule = pattern.tripSchedules.get(int(state.trip))
        board, alight = int(state.boardStopPosition), int(state.alightStopPosition)
        if (
            not 0 <= board < alight < len(pattern.stops)
            or str(pattern.pickups[board]) != "SCHEDULED"
            or str(pattern.dropoffs[alight]) != "SCHEDULED"
            or int(schedule.departures[board]) != int(state.boardTime)
            or int(schedule.arrivals[alight]) != int(state.time)
        ):
            raise InvalidPath("Unverified native scheduled calls")
        trip_id = str(schedule.tripId)
        source = self.routes.trip_lineage.get(trip_id)
        geometry = self.routes.geometry(index, trip_id)
        if source is None or geometry is None or any(h is None for h in geometry[board:alight]):
            raise InvalidPath("Missing original trip/shape provenance")
        coordinates = []
        for hop in geometry[board:alight]:
            if coordinates and coordinates[-1] != hop[0]:
                raise InvalidPath("Disconnected source transit shape")
            coordinates.extend(hop if not coordinates else hop[1:])
        route = self.transit.routes.get(pattern.routeIndex)
        from_stop = str(self.transit.stopIdForIndex.get(int(state.back.stop)))
        to_stop = str(self.transit.stopIdForIndex.get(int(state.stop)))
        return {
            "mode": "TRAM" if int(route.route_type) in (0, 900) else "BUS",
            "start_seconds": int(state.boardTime) - int(task.fromTime),
            "end_seconds": int(state.time) - int(task.fromTime),
            "from_name": self.routes.stops[from_stop]["name"],
            "to_name": self.routes.stops[to_stop]["name"],
            "route_id": str(pattern.routeId),
            "route_name": str(route.route_short_name),
            "trip_id": source["source_trip_id"],
            "service_date": (day + timedelta(days=source["service_day_shift"])).isoformat(),
            "coordinates": coordinates,
            "distance_m": transform(self.project, LineString(coordinates)).length,
        }

    @staticmethod
    def itinerary(legs):
        if not legs:
            raise InvalidPath("Missing native itinerary legs")
        previous_end = 0
        for leg in legs:
            if leg["start_seconds"] < previous_end or leg["end_seconds"] < leg["start_seconds"]:
                raise InvalidPath("Overlapping or reversed native legs")
            previous_end = leg["end_seconds"]
        elapsed = legs[-1]["end_seconds"]
        travel = sum(leg["end_seconds"] - leg["start_seconds"] for leg in legs)
        if elapsed > HORIZON or elapsed < travel or travel < 0:
            raise InvalidPath("Point itinerary exceeds its verified horizon")
        rides = sum(leg["mode"] != "WALK" for leg in legs)
        if rides > 8:
            raise InvalidPath("Native ride limit exceeded")
        return {
            "elapsed_seconds": elapsed,
            "travel_seconds": travel,
            "waiting_seconds": elapsed - travel,
            "transfers": max(0, rides - 1),
            "walking_only": rides == 0,
            "legs": legs,
        }

    def calculate(self, origin, destination, departure):
        import java.time
        from r5py import TransportMode
        from r5py.r5.regional_task import RegionalTask
        from r5py.r5.trip_planner import TripPlanner

        aware = datetime.fromisoformat(departure)
        local = aware.astimezone(WARSAW)
        if (
            aware.tzinfo is None
            or aware.second
            or aware.microsecond
            or aware.replace(tzinfo=None) != local.replace(tzinfo=None)
        ):
            raise ValueError("Use a Warsaw departure minute with its actual offset")
        result = {"method": POLICY, "graph_version": self.graph_version, "itineraries": []}
        if self.transit.getActiveServicesForDate(
            java.time.LocalDate.parse(local.date().isoformat())
        ).isEmpty():
            return result | {"status": "unsupported_date"}
        if query_clock(local) + HORIZON > 86400 * 2 + HORIZON:
            return result | {"status": "unsupported_date"}
        a, b = coordinate(origin), coordinate(destination)
        effective = [self.network.street_layer.find_split(p, radius=30) for p in (a, b)]
        if any(p.is_empty for p in effective):
            return result | {"status": "insufficient_coverage"}
        result.update(
            {
                "effective_origin": [effective[0].x, effective[0].y],
                "effective_destination": [effective[1].x, effective[1].y],
                "origin_link_m": Point(*self.project(a.x, a.y)).distance(
                    Point(*self.project(effective[0].x, effective[0].y))
                ),
                "destination_link_m": Point(*self.project(b.x, b.y)).distance(
                    Point(*self.project(effective[1].x, effective[1].y))
                ),
            }
        )
        # A network itinerary does not prove an entrance or an off-network link.
        result["origin_access_confirmed"] = result["origin_link_m"] <= 0.02
        result["destination_access_confirmed"] = result["destination_link_m"] <= 0.02
        request = RegionalTask(
            self.network,
            departure=local.replace(tzinfo=None),
            departure_time_window=timedelta(minutes=1),
            transport_modes=[TransportMode.BUS, TransportMode.TRAM, TransportMode.WALK],
            speed_walking=4.8,
            max_time=timedelta(seconds=HORIZON),
            max_public_transport_rides=8,
        )
        request.origin = a
        task = request._regional_task
        task.fromTime = query_clock(local)
        task.toTime = task.fromTime + 60
        task.toLon, task.toLat, task.monteCarloDraws = b.x, b.y, 1
        planner = TripPlanner(self.network, request)
        supplier = self.jpype.JProxy(
            "java.util.function.IntFunction",
            dict(apply=lambda ignored: self.r5.profile.SuboptimalDominatingList(5)),
        )
        router = self.r5.profile.McRaptorSuboptimalPathProfileRouter(
            self.native,
            request,
            planner._transit_access_times,
            planner._transit_egress_times,
            supplier,
            None,
            True,
        )
        router.route()
        if [int(k) for k in router.finalStatesByDepartureTime.keys()] != [int(task.fromTime)]:
            raise ValueError("Unexpected native point departure sampling")
        cache, omissions, choices = {}, Counter(), []
        try:
            walk = self.walk(effective[0], effective[1], task, cache)
            choices.append(
                self.itinerary([self.walk_leg(walk, 0, "Wybrany początek", "Wybrany cel")])
            )
        except InvalidPath:
            omissions["direct_walk"] += 1
        states = list(router.finalStatesByDepartureTime.get(int(task.fromTime)) or [])
        for final in states:
            chain, state = [], final.back
            while state is not None:
                chain.append(state)
                state = state.back
            chain.reverse()
            legs, ready, previous_stop = [], 0, None
            try:
                for state in chain:
                    stop = int(state.stop)
                    vertex = self.stop_vertex(stop)
                    name = self.routes.stops[str(self.transit.stopIdForIndex.get(stop))]["name"]
                    if state.back is None or int(state.pattern) == -1:
                        start = (
                            effective[0]
                            if previous_stop is None
                            else self.stop_vertex(previous_stop)
                        )
                        walk = self.walk(start, vertex, task, cache)
                        from_name = "Wybrany początek" if not legs else legs[-1]["to_name"]
                        legs.append(self.walk_leg(walk, ready, from_name, name))
                        ready = max(legs[-1]["end_seconds"], int(state.time) - int(task.fromTime))
                    else:
                        leg = self.ride_leg(state, task, local.date())
                        if leg["start_seconds"] < ready + 60:
                            raise InvalidPath("Unverified boarding/transfer time")
                        legs.append(leg)
                        ready = leg["end_seconds"]
                    previous_stop = stop
                if previous_stop is None:
                    raise InvalidPath("Missing native access chain")
                walk = self.walk(self.stop_vertex(previous_stop), effective[1], task, cache)
                legs.append(self.walk_leg(walk, ready, legs[-1]["to_name"], "Wybrany cel"))
                choices.append(self.itinerary(legs))
            except InvalidPath as error:
                omissions[str(error)] += 1
        unique = {}
        for choice in choices:
            key = tuple(
                (leg["mode"], leg["trip_id"], leg["start_seconds"], leg["to_name"])
                for leg in choice["legs"]
            )
            unique.setdefault(key, choice)
        result["itineraries"] = sorted(unique.values(), key=lambda p: p["elapsed_seconds"])[:5]
        result["omitted"] = dict(omissions)
        result["status"] = (
            "ok"
            if result["itineraries"]
            else ("insufficient_coverage" if states else "outside_horizon")
        )
        return result
