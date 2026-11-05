"""Use the coverage engine's exact scheduled point model within its45min horizon.

OTP remains available for walk-only requests, longer journeys and installations
without the verified point runtime. A failed native proof is never an OTP proof.
"""

import asyncio
import json
import math
import time
from datetime import UTC, datetime, timedelta

import httpx

from services.api.routing import ROOT, WARSAW, Coordinate, Itinerary, Leg, RouteAdapter, RouteResult
from tools.release.response_cache import ResponseCache
from tools.router.instant_point_runtime import POLICY, verified_runtime
from tools.router.native_point import NativePoint
from tools.router.walk_access import POLICY as WALK_POLICY

ENDPOINT = "http://127.0.0.1:8810"


def encode_polyline(coordinates):
    """Google encoded polyline5, for display only; proofs use full native geometry."""
    previous = [0, 0]
    output = []
    if len(coordinates) < 2:
        raise ValueError("Missing point leg geometry")
    for coordinate in coordinates:
        if len(coordinate) != 2 or any(not math.isfinite(v) for v in coordinate):
            raise ValueError("Invalid point leg geometry")
        lon, lat = coordinate
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise ValueError("Invalid point leg coordinate")
        for axis, value in enumerate((lat, lon)):
            # Equivalent to JavaScript Math.round, including negative half values.
            current = math.floor(value * 100000 + 0.5)
            delta = current - previous[axis]
            previous[axis] = current
            encoded = ~(delta << 1) if delta < 0 else delta << 1
            while encoded >= 32:
                output.append(chr((encoded & 31) + 95))
                encoded >>= 5
            output.append(chr(encoded + 63))
    return "".join(output)


def normalize_point(raw, request, metadata):
    expected = {
        "origin": [request.origin.longitude, request.origin.latitude],
        "destination": [request.destination.longitude, request.destination.latitude],
        "departure": request.departure.isoformat(),
        "data_version": request.data_version,
    }
    if (
        raw["request"] != expected
        or raw["data_version"] != request.data_version
        or raw["method"] != POLICY
        or raw["graph_version"] != metadata["graph_version"]
    ):
        raise ValueError("Mismatched native point response")
    if raw["status"] in ("outside_horizon", "unsupported_time"):
        if raw["itineraries"]:
            raise ValueError("Unexpected horizon result")
        return None
    if raw["status"] not in ("ok", "insufficient_coverage", "unsupported_date"):
        raise ValueError("Unknown point status")
    values = {}
    for key in ("origin", "destination"):
        if f"effective_{key}" in raw:
            lon, lat = raw[f"effective_{key}"]
            values[f"effective_{key}"] = Coordinate(longitude=lon, latitude=lat)
            link = raw[f"{key}_link_m"]
            confirmed = raw[f"{key}_access_confirmed"]
            if type(confirmed) is not bool or confirmed != (link <= 0.02):
                raise ValueError("Inconsistent point access proof")
            values[f"{key}_link_m"] = link
            values[f"{key}_access_confirmed"] = confirmed
    itineraries = []
    for raw_itinerary in raw["itineraries"]:
        summary = NativePoint.itinerary(raw_itinerary["legs"])
        for key in (
            "elapsed_seconds",
            "travel_seconds",
            "waiting_seconds",
            "transfers",
            "walking_only",
        ):
            if raw_itinerary[key] != summary[key]:
                raise ValueError("Inconsistent native itinerary totals")
        legs, previous = [], 0
        for leg in raw_itinerary["legs"]:
            start, end = leg["start_seconds"], leg["end_seconds"]
            if type(start) is not int or type(end) is not int:
                raise ValueError("Native times must be integer seconds")
            if leg["mode"] != "WALK" and (
                start < previous + 60 or not leg["trip_id"] or not leg["service_date"]
            ):
                raise ValueError("Unverified scheduled boarding")
            clock = request.departure.astimezone(UTC)
            legs.append(
                Leg(
                    **{
                        key: leg[key]
                        for key in (
                            "mode",
                            "from_name",
                            "to_name",
                            "route_id",
                            "route_name",
                            "trip_id",
                            "service_date",
                            "distance_m",
                        )
                    },
                    departure=(clock + timedelta(seconds=start)).astimezone(WARSAW),
                    arrival=(clock + timedelta(seconds=end)).astimezone(WARSAW),
                    geometry_polyline=encode_polyline(leg["coordinates"]),
                )
            )
            previous = end
        itineraries.append(
            Itinerary(
                **{
                    key: summary[key]
                    for key in (
                        "elapsed_seconds",
                        "travel_seconds",
                        "waiting_seconds",
                        "transfers",
                        "walking_only",
                    )
                },
                arrival=legs[-1].arrival,
                legs=legs,
                network_only=(
                    values.get("origin_access_confirmed") is False
                    or values.get("destination_access_confirmed") is False
                ),
            )
        )
    if bool(itineraries) != (raw["status"] == "ok"):
        raise ValueError("Inconsistent point route status")
    if itineraries and len(values) != 6:
        raise ValueError("Missing native endpoint proof")
    explanation = {
        "ok": "Rozkład statyczny; czas w sieci obejmuje dojścia, oczekiwanie i przesiadki.",
        "insufficient_coverage": (
            "Brak potwierdzonej trasy w sieci. To nie dowodzi braku połączenia."
        ),
        "unsupported_date": "Brak pełnego pokrycia rozkładów dla tej daty i godziny.",
    }[raw["status"]]
    return RouteResult(
        request=request,
        status=raw["status"],
        routing_version=POLICY,
        graph_version=metadata["graph_version"],
        computed_at=datetime.now(UTC),
        effective_departure=request.departure,
        explanation=explanation,
        itineraries=sorted(itineraries, key=lambda value: value.elapsed_seconds),
        **values,
    )


class R5PreferredRouteAdapter(RouteAdapter):
    def __init__(self, point_transport=None, runtime_sha=None, **kwargs):
        super().__init__(**kwargs)
        self.point_transport = point_transport
        jar = verified_runtime(ROOT) if runtime_sha is None else None
        self.point_sha = runtime_sha
        if jar:
            proof = json.loads(jar.with_name("instant-point-v1.json").read_text(encoding="utf-8"))
            self.point_sha = proof["output_sha256"]
        self.point_cache = ResponseCache(max_entries=self.capacity)

    def point_sources_match(self):
        for source, expected in self.input_hashes.items():
            if source != "osm" and not source.startswith("ztp_gtfs_"):
                continue
            name = "router-runtime" if source == "osm" else source
            manifest = json.loads(
                (ROOT / f"data/manifests/{name}.json").read_text(encoding="utf-8")
            )
            if manifest["osm_sha256" if source == "osm" else "sha256"] != expected:
                return False
        return True

    async def plan(self, request):
        if request.mode == "walk" or not self.point_sha:
            return await super().plan(request)
        fallback_status = None
        try:
            if not self.point_sources_match():
                raise ValueError("Prepared native point sources are stale")
            async with asyncio.timeout(self.timeout):
                async with self.semaphore:
                    async with httpx.AsyncClient(
                        transport=self.point_transport, timeout=self.timeout
                    ) as client:
                        health = await client.get(ENDPOINT + "/health")
                        health.raise_for_status()
                        metadata = health.json()
                        if metadata.get("point_method") is not None:
                            expected = {
                                ("malopolskie-261011.osm.pbf" if k == "osm" else k + ".zip"): v
                                for k, v in self.input_hashes.items()
                                if k == "osm" or k.startswith("ztp_gtfs_")
                            }
                            if (
                                metadata["point_method"] != POLICY
                                or metadata["runtime_jar_sha256"] != self.point_sha
                                or metadata["sources"] != expected
                                or metadata["data_version"] != request.data_version
                                or metadata["schema_version"] != "4"
                            ):
                                raise ValueError("Native point runtime/source mismatch")
                            key = (
                                metadata["graph_version"],
                                self.point_sha,
                                POLICY,
                                WALK_POLICY,
                                request.model_dump_json(),
                            )
                            cached = self.point_cache.get(key)
                            if cached and time.monotonic() - cached[0] < self.ttl:
                                self.point_cache.move_to_end(key)
                                return cached[1].model_copy(update={"cache_hit": True})
                            response = await client.post(
                                ENDPOINT + "/point",
                                json={
                                    "origin": [request.origin.longitude, request.origin.latitude],
                                    "destination": [
                                        request.destination.longitude,
                                        request.destination.latitude,
                                    ],
                                    "departure": request.departure.isoformat(),
                                    "data_version": request.data_version,
                                },
                            )
                            response.raise_for_status()
                            raw = response.json()
                            result = normalize_point(raw, request, metadata)
                            fallback_status = raw["status"]
                            if result is not None:
                                self.point_cache[key] = (time.monotonic(), result)
                                while len(self.point_cache) > self.capacity:
                                    self.point_cache.popitem(last=False)
                                return result
            # An unsupported native window is a model boundary, not unreachability.
            fallback = await super().plan(request)
            if fallback_status == "unsupported_time" and fallback.status == "no_route":
                return fallback.model_copy(
                    update={
                        "status": "insufficient_coverage",
                        "explanation": "Nie potwierdzono połączenia na granicy pokrycia rozkładów. "
                        "To nie dowodzi braku trasy.",
                    }
                )
            return fallback
        except httpx.ConnectError:
            return await super().plan(request)
        except (TimeoutError, httpx.TimeoutException):
            return self.result(
                request, "timeout", "Obliczenie trasy przekroczyło limit czasu. Spróbuj ponownie."
            )
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
            return self.result(
                request, "unavailable", "Trasa nie została potwierdzona. Spróbuj ponownie."
            )
