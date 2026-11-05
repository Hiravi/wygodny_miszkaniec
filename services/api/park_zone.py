"""Bounded multi-park native WALK; route evidence is separate from area completeness."""

import asyncio
import math
import time

from shapely.geometry import LineString, Point, shape

from services.api.parks import metric, unknown_result
from services.api.routing import Coordinate, RouteRequest
from tools.data.cells import decode_polyline


def origin_access(adapter, origin, origin_kind="point"):
    point = metric(Point(origin.longitude, origin.latitude))
    if any(adapter.buildings[i].contains(point) for i in adapter.building_tree.query(point)):
        return False, "building_access_unverified"
    if any(
        adapter.barriers[i].distance(point) <= 3
        for i in adapter.barrier_tree.query(point.buffer(3))
    ):
        return False, "mapped_barrier_near_origin"
    if not any(
        adapter.public[i].distance(point) <= 2 for i in adapter.public_tree.query(point.buffer(2))
    ):
        return False, "origin_to_network_unverified"
    if origin_kind == "address":
        return False, "address_to_network_unverified"
    return True, "mapped_walking_network"


def follows_public_network(adapter, line):
    """Require a returned walk to remain on retained public source geometry."""
    steps = max(1, int(line.length / 3) + 1)
    for step in range(steps + 1):
        point = line.interpolate(line.length * step / steps)
        # The retained OSM geometry and OTP polyline use different rounding;
        # two metres is a geometry-match tolerance, never a connector.
        candidates = adapter.public_tree.query(point.buffer(2))
        if not any(adapter.public[i].distance(point) <= 2 for i in candidates):
            return False
    return not any(
        adapter.buildings[i].intersects(line) for i in adapter.building_tree.query(line)
    )


def selected_targets(targets, origin):
    """Three closest geometric candidates per park, at most24; never a travel-time claim."""
    point = metric(Point(origin.longitude, origin.latitude))
    ranked = sorted(targets, key=lambda t: (point.distance(metric(Point(t["entrance"]))), t["id"]))
    counts, result = {}, []
    # First give up to8 parks one candidate, then up to three entrances per park.
    for cap in (1, 2, 3):
        for target in ranked:
            park = target["park_id"]
            if counts.get(park, 0) != cap - 1 or target in result:
                continue
            if cap == 1 and len(counts) == 8:
                continue
            result.append(target)
            counts[park] = cap
    return result


async def plan_zone(adapter, body):
    coverage = adapter.coverage
    result = {
        **unknown_result(
            body,
            adapter.base.graph_version,
            adapter.manifest["output_sha256"],
            len(adapter.targets),
        ),
        "schema_version": "3",
        "method": getattr(adapter, "method", "park-zone-walk-v3"),
        "coverage": coverage,
        "routes": [],
        "origin_access_confirmed": False,
        "exclusion": {"complete": False, "max_minutes": 30, "unresolved": True},
    }
    if not shape(getattr(adapter, "origin_zone", None) or coverage["zone"]).covers(
        Point(body.origin.longitude, body.origin.latitude)
    ):
        return {**result, "reason": "outside_pilot_zone"}
    if not adapter.base.snapshot_matches():
        return {**result, "reason": "snapshot_mismatch"}
    access, reason = origin_access(adapter, body.origin, body.origin_kind)
    result.update(origin_access_confirmed=access, reason=reason)
    key = (
        getattr(adapter, "method", "park-zone-walk-v3"),
        adapter.manifest["output_sha256"],
        adapter.base.graph_version,
        body.model_dump_json(),
    )
    cached = adapter.cache.get(key)
    if cached and time.monotonic() - cached[0] < 300:
        adapter.cache.move_to_end(key)
        return {**cached[1], "cache_hit": True}
    targets = (
        adapter.select_targets(body.origin)
        if hasattr(adapter, "select_targets")
        else selected_targets(adapter.targets, body.origin)
    )

    async def query(target):
        native = await adapter.base.plan(
            RouteRequest(
                origin=body.origin,
                destination=dict(zip(("longitude", "latitude"), target["target"], strict=True)),
                departure=result["reference_departure"],
                mode="walk",
                data_version=body.data_version,
            )
        )
        proof = None
        try:
            if native.itineraries:
                first = decode_polyline(native.itineraries[0].legs[0].geometry_polyline)[0]
                gap = metric(Point(first)).distance(
                    metric(Point(body.origin.longitude, body.origin.latitude))
                )
                # Keep an honest route-to-a-specific-park even when the origin link is unknown.
                if gap <= 25:
                    proof = adapter.proof(
                        Coordinate(longitude=first[0], latitude=first[1]), target, native
                    )
                    if proof:
                        attachment = metric(
                            LineString(
                                [
                                    (body.origin.longitude, body.origin.latitude),
                                    first,
                                ]
                            )
                        )
                        attachment_open = not any(
                            adapter.barriers[i].intersects(attachment)
                            for i in adapter.barrier_tree.query(attachment)
                        )
                        proof.update(
                            origin_access_confirmed=(
                                access
                                and gap <= 2
                                and attachment_open
                                and follows_public_network(adapter, attachment)
                            ),
                            origin_gap_m=math.ceil(gap * 10) / 10,
                            network_origin=list(first),
                            source_way_id=target["way_id"],
                        )
        except (ValueError, KeyError, TypeError, IndexError):
            proof = None
        return proof, native.status

    async with asyncio.timeout(10), adapter.batches:
        answers = await asyncio.gather(*(query(t) for t in targets))
    proofs = [proof for proof, _ in answers if proof is not None]
    confirmed = [p for p in proofs if p["origin_access_confirmed"]]
    by_park = {}
    for proof in sorted(proofs, key=lambda p: (not p["origin_access_confirmed"], p["seconds"])):
        by_park.setdefault(proof["park_id"], proof)
    result.update(
        targets_checked=len(answers),
        routes=list(by_park.values()),
        failures=sum(status not in {"ok", "no_route"} for _, status in answers),
        reason="confirmed_sample_route"
        if confirmed
        else reason
        if not access
        else "no_verified_route_in_partial_catalogue",
    )
    if confirmed:
        result.update(status="ok", best=min(confirmed, key=lambda p: p["seconds"]))
    if not result["failures"]:
        adapter.cache[key] = (time.monotonic(), result)
        while len(adapter.cache) > 16:
            adapter.cache.popitem(last=False)
    return result
