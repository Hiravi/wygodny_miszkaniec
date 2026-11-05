"""Bounded read-only diagnostics of pinned native WALK paths and stop transfers."""


def inspect_walks(network, task, requests):
    import com.conveyal.r5 as r5

    if len(requests) > 12:
        raise ValueError("At most twelve WALK controls")
    native = network.transitLayer
    stops = {str(value): i for i, value in enumerate(native.stopIdForIndex)}
    results = []
    for item in requests:
        walk = r5.streets.StreetRouter(network.streetLayer)
        walk.profileRequest = task
        walk.streetMode = r5.profile.StreetMode.WALK
        walk.quantityToMinimize = r5.streets.StreetRouter.State.RoutingVariable.DURATION_SECONDS
        walk.timeLimitSeconds = 3600
        origin, target = item["from"], item["to"]
        if isinstance(origin, str):
            vertex = int(native.streetVertexForStop.get(stops[origin]))
            if vertex < 0:
                results.append({"request": item, "status": "unlinked_origin"})
                continue
            walk.setOrigin(vertex)
        elif not walk.setOrigin(origin[1], origin[0]):
            results.append({"request": item, "status": "unlinked_origin"})
            continue
        if isinstance(target, str):
            walk.toVertex = int(native.streetVertexForStop.get(stops[target]))
        elif not walk.setDestination(target[1], target[0]):
            results.append({"request": item, "status": "unlinked_destination"})
            continue
        walk.route()
        state = (
            walk.getStateAtVertex(walk.toVertex)
            if isinstance(target, str)
            else walk.getState(walk.getDestinationSplit())
        )
        result = {"request": item, "status": "ok" if state is not None else "no_path"}
        if isinstance(origin, str) and isinstance(target, str):
            transfers = native.streetTransfers.get(stops[origin])
            values = [] if transfers is None else [int(x) for x in transfers.toArray()]
            result["precomputed_transfer_mm"] = next(
                (values[i + 1] for i in range(0, len(values), 2) if values[i] == stops[target]),
                None,
            )
        if state is not None:
            path = r5.profile.StreetPath(state, network, False)
            segment = r5.api.util.StreetSegment(path, r5.api.util.LegMode.WALK, network.streetLayer)
            result["seconds"] = int(state.getDurationSeconds())
            result["distance_native"] = int(path.getDistance())
            result["coordinates"] = [
                [float(c.x), float(c.y)] for c in segment.geometry.getCoordinates()
            ]
            result["edges"] = []
            for edge_id in path.getEdges():
                edge = network.streetLayer.edgeStore.getCursor(int(edge_id))
                result["edges"].append(
                    {
                        "edge": int(edge_id),
                        "osm_way": int(edge.getOSMID()),
                        "length_m": float(edge.getLengthM()),
                        "permissions": str(edge.getPermissionsAsString()),
                        "flags": str(edge.getFlags()),
                        "walk_allowed": bool(edge.allowsStreetMode(r5.profile.StreetMode.WALK)),
                    }
                )
        results.append(result)
    return results
