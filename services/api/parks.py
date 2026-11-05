"""Positive park-access proofs from a deliberately incomplete entrance catalogue."""

import asyncio
import json
import time
from collections import OrderedDict
from contextlib import closing
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field
from shapely import from_wkb, transform
from shapely.geometry import LineString, Point, shape
from shapely.strtree import STRtree

from services.api.locations import DATA, database, version
from services.api.park_runtime import packed
from services.api.routing import Coordinate, Model, RouteAdapter, RouteRequest
from tools.data.acquire import ROOT, sha256
from tools.data.cells import TO_METRIC, decode_polyline

router = APIRouter(prefix="/api")
METHOD = "public-park-walk-v2"


def geometry_array(geometries):
    """Complete source fallback; the prepared binary runtime avoids this cold cost."""
    return [shape(geometry) for geometry in geometries]


async def load_adapter(state, key, factory):
    """One shared static loader; an obsolete caller cannot cancel another caller's load."""
    if hasattr(state, key):
        return getattr(state, key)
    pending_key = key + "_loading"
    pending = getattr(state, pending_key, None)
    if pending is None:

        async def initialize():
            try:
                value = await asyncio.to_thread(factory)
                setattr(state, key, value)
                return value
            finally:
                if getattr(state, pending_key, None) is asyncio.current_task():
                    delattr(state, pending_key)

        pending = asyncio.create_task(initialize())
        # Retrieve errors even if every requesting browser has already disconnected.
        pending.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        setattr(state, pending_key, pending)
    return await asyncio.shield(pending)


@router.get("/park-coverage")
def park_coverage():
    if not (DATA / "park-zone-v3.json").is_file():
        raise HTTPException(404, "Park pilot coverage unavailable")
    path = ROOT / "data/manifests/park-zone-v3.json"
    return json.loads(path.read_text(encoding="utf8"))["coverage"]


class ParkRequest(Model):
    origin: Coordinate
    data_version: str = Field(min_length=1, max_length=80)
    # The OSM snapshot has no address-to-footway connectors. An address is
    # therefore not promoted to the proof available for a selected public path.
    origin_kind: Literal["address", "point"] = "point"


def metric(geometry):
    return transform(geometry, TO_METRIC.transform, interleaved=False)


def unknown_result(
    body,
    graph_version="unavailable",
    access_version="unavailable",
    total=0,
    reason="catalogue_unavailable",
):
    return {
        "schema_version": "2",
        "method": METHOD,
        "request": body.model_dump(mode="json"),
        "graph_version": graph_version,
        "access_version": access_version,
        "speed_walking_kmh": 4.8,
        "reference_departure": "2026-10-14T08:00:00+02:00",
        "computed_at": datetime.now(UTC).isoformat(),
        "cache_hit": False,
        "status": "unknown",
        "reason": reason,
        "best": None,
        "targets_total": total,
        "targets_checked": 0,
        "failures": 0,
        "catalogue_complete": False,
    }


class ParkAdapter:
    def __init__(self, base=None, dataset=None):
        self.base = base or RouteAdapter()
        self.zone = dataset is not None or (DATA / "park-zone-v3.json").is_file()
        name = dataset or ("park-zone-v3" if self.zone else "park-access-v2")
        manifest_path = ROOT / f"data/manifests/{name}.json"
        path = DATA / f"{name}.json"
        self.manifest = json.loads(manifest_path.read_text(encoding="utf8"))
        if sha256(path) != self.manifest["output_sha256"]:
            raise ValueError("Park access snapshot mismatch")
        runtime = packed(path, self.manifest["output_sha256"])
        data = runtime[0] if runtime else json.loads(path.read_text(encoding="utf8"))
        if data["osm_sha256"] != self.base.input_hashes["osm"]:
            raise ValueError("Access evidence and routing graph differ")
        self.targets = data["targets"]
        self.place_geometries = data.get("place_geometries", {})
        self.public_gates = data.get("public_gates", [])
        self.public = runtime[1]["public"] if runtime else geometry_array(data["public_lines"])
        self.barriers = runtime[1]["barriers"] if runtime else geometry_array(data["barriers"])
        self.public_tree, self.barrier_tree = STRtree(self.public), STRtree(self.barriers)
        self.coverage = data.get("coverage")
        self.origin_zone = data.get("origin_zone", self.coverage["zone"] if self.coverage else None)
        self.buildings = (
            runtime[1]["buildings"] if runtime else geometry_array(data.get("buildings", []))
        )
        self.building_tree = STRtree(self.buildings)
        self.cache = OrderedDict()
        self.batches = asyncio.Semaphore(2)

    def eligible_origin(self, origin):
        point = metric(Point(origin.longitude, origin.latitude))
        return any(
            self.public[i].distance(point) <= 1 for i in self.public_tree.query(point.buffer(1))
        ) and not any(
            self.barriers[i].distance(point) <= 3 for i in self.barrier_tree.query(point.buffer(3))
        )

    def proof(self, origin, target, result):
        if result.status != "ok" or not result.itineraries:
            return None
        itinerary = result.itineraries[0]
        if not itinerary.walking_only or any(leg.mode != "WALK" for leg in itinerary.legs):
            return None
        coordinates = []
        for leg in itinerary.legs:
            coordinates.extend(decode_polyline(leg.geometry_polyline))
        if len(coordinates) < 2:
            return None
        line = metric(LineString(coordinates))
        start, end = metric(Point(coordinates[0])), metric(Point(coordinates[-1]))
        park = metric(shape(target["park"]))
        if (
            start.distance(metric(Point(origin.longitude, origin.latitude))) > 2
            or end.distance(metric(Point(target["target"]))) > 2
            or not park.contains(end)
            or line.distance(metric(Point(target["entrance"]))) > 2
        ):
            return None
        return {
            "entrance_id": target["id"],
            "park_id": target["park_id"],
            "name": target["name"],
            "seconds": itinerary.elapsed_seconds,
            "distance_m": sum(leg.distance_m for leg in itinerary.legs),
            "entrance": target["entrance"],
            "target": target["target"],
            "path": {"type": "LineString", "coordinates": coordinates},
            "park": target["park"],
        }

    async def plan(self, body):
        if self.coverage:
            from services.api.park_zone import plan_zone

            return await plan_zone(self, body)
        key = (
            METHOD,
            self.manifest["output_sha256"],
            self.base.graph_version,
            body.model_dump_json(),
        )
        result = unknown_result(
            body,
            self.base.graph_version,
            self.manifest["output_sha256"],
            len(self.targets),
            "origin_access_unverified",
        )
        if not self.base.snapshot_matches():
            return {**result, "reason": "snapshot_mismatch"}
        if not self.eligible_origin(body.origin):
            return result
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            self.cache.move_to_end(key)
            return {**cached[1], "cache_hit": True}

        async def query(target):
            request = RouteRequest(
                origin=body.origin,
                destination=dict(zip(("longitude", "latitude"), target["target"], strict=True)),
                departure=result["reference_departure"],
                mode="walk",
                data_version=body.data_version,
            )
            native = await self.base.plan(request)
            try:
                proof = self.proof(body.origin, target, native)
            except (ValueError, KeyError, TypeError):
                proof = None
            return proof, native.status

        async with asyncio.timeout(10), self.batches:
            answers = await asyncio.gather(*(query(target) for target in self.targets))
        proofs = [proof for proof, _ in answers if proof is not None]
        result.update(
            targets_checked=len(answers),
            failures=sum(status not in {"ok", "no_route"} for _, status in answers),
        )
        if proofs:
            result.update(
                status="ok",
                reason="confirmed_sample_route",
                best=min(proofs, key=lambda p: p["seconds"]),
            )
        else:
            result["reason"] = "no_verified_route_in_partial_catalogue"
        if not result["failures"]:
            self.cache[key] = (time.monotonic(), result)
        while len(self.cache) > 16:
            self.cache.popitem(last=False)
        return result


@router.post("/park-access")
async def park_access(body: ParkRequest, request: Request):
    return await walking_access(body, request)


async def walking_access(body, request, key="park_adapter", factory=ParkAdapter):
    with closing(database()) as db:
        if body.data_version != version(db):
            raise HTTPException(409, "Location version changed")
        inside = any(
            from_wkb(row[0]).covers(Point(body.origin.longitude, body.origin.latitude))
            for row in db.execute("SELECT geometry FROM districts")
        )
    if not inside:
        raise HTTPException(422, "Choose a point in Krakow")
    try:
        adapter = await load_adapter(request.app.state, key, factory)
    except FileNotFoundError:
        return {**unknown_result(body), "method": getattr(factory, "method", METHOD)}
    except (OSError, ValueError, KeyError):
        raise HTTPException(503, "Park access evidence unavailable") from None
    if await request.is_disconnected():
        raise HTTPException(499, "Obsolete park request")
    task = asyncio.create_task(adapter.plan(body))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.1)
            if await request.is_disconnected():
                task.cancel()
                raise HTTPException(499, "Obsolete park request")
        return await task
    except TimeoutError:
        raise HTTPException(504, "Walking calculation deadline exceeded") from None
    finally:
        if not task.done():
            task.cancel()
