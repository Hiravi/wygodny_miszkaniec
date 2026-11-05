"""Validate sampled destinations with native directed WALK, not distance propagation.

Reuse the matrix's native egress choice and validated transit prefixes. One native
StreetRouter per distinct egress stop serves all its destinations; no OTP fanout.
Failure is absence of proof, never proof that no journey exists.
"""

from collections import defaultdict

POLICY = "native-grouped-final-walk-v34"


def seconds_band(seconds):
    if seconds is None or seconds < 0 or seconds > 2700:
        return None
    return next(n for n in (15, 30, 45) if seconds <= n * 60)


def remaining_budget(targets, proofs, uncertain, prefix_seconds):
    """Only an improvement on the incumbent can change accept(); WALK costs are nonnegative.

    Use the largest remaining time across ALL group targets, including unknowns.
    Do not substitute a distance radius or discard targets without an incumbent.
    """

    def incumbent(index):
        if index in proofs:
            return proofs[index]["confirmed_seconds"]
        if index in uncertain:
            return uncertain[index]["candidate_seconds"]
        return 2700

    return max((incumbent(i) - prefix_seconds for i in targets), default=0)


def matrix_class(r5py, native, candidates, native_clock=None, prepared_destinations=None):
    import copy

    import com.conveyal.r5 as r5

    class Matrix(r5py.TravelTimeMatrix):
        def _travel_times_per_origin(self, from_id):
            request = copy.copy(self.request)
            if prepared_destinations is not None:
                prepared_destinations.apply(request, self.destinations)
            request.origin = self.origins.geometry.item()
            if native_clock is not None:
                request._regional_task.fromTime = native_clock
                request._regional_task.toTime = native_clock + 60
            request._regional_task.includePathResults = True
            result = r5.analyst.TravelTimeComputer(
                request, self.transport_network
            ).computeTravelTimes()
            for index in range(len(self.destinations)):
                choices = []
                templates = result.paths.iterationsForPathTemplates[index] if result.paths else None
                if templates is not None:
                    for entry in templates.entries():
                        sequence = entry.getKey().stopSequence
                        stops = sequence.alightStops if sequence is not None else None
                        if stops is not None and stops.size():
                            choices.append(
                                (int(entry.getValue().totalTime), int(stops.get(stops.size() - 1)))
                            )
                candidates.append(
                    str(native.stopIdForIndex.get(min(choices)[1])) if choices else None
                )
            return self._parse_results(from_id, result)

    return Matrix


def confirm(network, task, records, candidates, routes, access, splits=None, eligible=None):
    import com.conveyal.r5 as r5
    from org.locationtech.jts.io import WKBWriter
    from shapely import from_wkb

    native = network._transport_network
    geometry_writer = WKBWriter(2)
    transit = native.transitLayer
    indices = {str(s): i for i, s in enumerate(transit.stopIdForIndex)}
    prefixes = {}
    for feature in routes["sections"]["features"] if routes else []:
        p = feature["properties"]
        stop = p["to_stop"]
        if p["alighting"] and (
            stop not in prefixes or p["cumulative_seconds"] < prefixes[stop]["cumulative_seconds"]
        ):
            prefixes[stop] = p
    groups = defaultdict(list)
    for index, candidate in enumerate(candidates):
        if eligible is not None and index not in eligible:
            continue
        if candidate in prefixes:
            groups[candidate].append(index)

    def router():
        walk = r5.streets.StreetRouter(native.streetLayer)
        walk.profileRequest = task
        walk.streetMode = r5.profile.StreetMode.WALK
        walk.quantityToMinimize = r5.streets.StreetRouter.State.RoutingVariable.DURATION_SECONDS
        return walk

    proofs, uncertain = {}, {}

    def accept(walk, index, prefix=None):
        record = records[index]
        split = (
            splits[index]
            if splits is not None
            else native.streetLayer.findSplit(
                record["latitude"], record["longitude"], 30.0, r5.profile.StreetMode.WALK
            )
        )
        if split is None:
            return
        state = walk.getState(split)
        if state is None:
            return
        seconds = int(state.getDurationSeconds())
        total = seconds + (prefix["cumulative_seconds"] if prefix else 0)
        if seconds_band(total) is None or (
            index in proofs and proofs[index]["confirmed_seconds"] <= total
        ):
            return
        # Off-network connector is a model linkage, not source-confirmed access.
        # Retain its diagnostic cost as uncertain; never colour it as a proof.
        if int(split.distanceToEdge_mm) > 20:
            if index not in uncertain or total < uncertain[index]["candidate_seconds"]:
                uncertain[index] = {
                    "confirmation": "approximate",
                    "band": None,
                    "candidate_seconds": total,
                    "endpoint_link_m": int(split.distanceToEdge_mm) / 1000,
                }
            return
        if seconds:
            path = r5.profile.StreetPath(state, native, False)
            segment = r5.api.util.StreetSegment(path, r5.api.util.LegMode.WALK, native.streetLayer)
            if segment.geometry is None:
                return
            # WKB retains the same x/y doubles without hundreds of thousands of
            # per-coordinate JVM/Python field reads. Search and guard are unchanged.
            line = from_wkb(bytes(geometry_writer.write(segment.geometry)))
            if not access.allows_geometry(line, 0.02):
                return
        proofs[index] = {
            "confirmed_seconds": total,
            "final_walk_seconds": seconds,
            "egress_stop": prefix["to_stop"] if prefix else None,
            "witness_id": prefix["witness_id"] if prefix else None,
            "section_id": prefix["id"] if prefix else None,
            "band": seconds_band(total),
            "confirmation": "confirmed",
        }

    direct = router()
    direct.timeLimitSeconds = 2700
    if direct.setOrigin(task.fromLat, task.fromLon):
        direct.route()
        for index in eligible if eligible is not None else range(len(records)):
            accept(direct, index)
    for stop, targets in groups.items():
        vertex = int(transit.streetVertexForStop.get(indices[stop]))
        if vertex < 0:
            continue
        walk = router()
        walk.timeLimitSeconds = remaining_budget(
            targets, proofs, uncertain, prefixes[stop]["cumulative_seconds"]
        )
        if walk.timeLimitSeconds <= 0:
            continue
        walk.setOrigin(vertex)
        walk.route()
        for index in targets:
            accept(walk, index, prefixes[stop])
    return uncertain | proofs, {
        "egress_searches": len(groups),
        "direct_searches": 1,
        "confirmed_points": len(proofs),
    }
