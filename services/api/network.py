"""Bounded, snapshot-scoped native stop exploration; confirmed journeys, not a city surface."""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from contextlib import closing, suppress
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, Field, field_validator
from shapely import from_wkb
from shapely.geometry import Point

from services.api.locations import database, version
from services.api.network_geometry import PATH_METHOD, journey_features
from services.api.network_native import LIMIT, METHOD, QUERY, verify
from services.api.routing import (
    ENDPOINT,
    Coordinate,
    Itinerary,
    Leg,
    Model,
    RouteAdapter,
    RouteRequest,
    normalize_itinerary,
)
from tools.data.acquire import ROOT
from tools.data.cells import TO_METRIC
from tools.router.normalize import instant

router = APIRouter(prefix="/api")
MANIFEST = json.loads((ROOT / "data/manifests/network-probe-v2.json").read_text(encoding="utf-8"))


class NetworkRequest(Model):
    origin: Coordinate
    departure: AwareDatetime
    data_version: str = Field(min_length=1, max_length=80)

    @field_validator("departure")
    @classmethod
    def warsaw_minute(cls, value):
        return RouteRequest.warsaw_minute(value)


class Platform(Model):
    id: str
    name: str
    code: str | None
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    district_ids: list[int]
    native_ids: list[str]
    route_ids: list[str]


class StopCall(Model):
    stop_id: str
    name: str
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    position: int = Field(ge=0)
    arrival: AwareDatetime
    departure: AwareDatetime


class NetworkLeg(Leg):
    trip_id: str | None
    direction_id: str | None
    headsign: str | None
    from_stop_id: str | None
    to_stop_id: str | None
    from_position: int | None
    to_position: int | None
    pickup_type: str
    dropoff_type: str
    calls: list[StopCall]


class NetworkJourney(Itinerary):
    legs: list[NetworkLeg]
    effective_origin: Coordinate
    effective_destination: Coordinate


class PathProperties(Model):
    journey_id: str
    leg_index: int = Field(ge=0)
    segment_index: int = Field(ge=0)
    mode: Literal["WALK", "BUS", "TRAM"]
    method: Literal["native-calls-end-clock-v2"]
    cumulative_seconds: float = Field(ge=0, allow_inf_nan=False)
    start_cumulative_seconds: float = Field(ge=0, allow_inf_nan=False)
    band_upper_seconds: Literal[900, 1800, 2700] | None
    arrival: AwareDatetime
    departure: AwareDatetime
    route_id: str | None
    route_name: str | None
    trip_id: str | None
    headsign: str | None
    direction_id: str | None
    service_date: str | None
    from_stop_id: str | None
    to_stop_id: str | None
    from_position: int | None
    to_position: int | None
    maximum_call_offset_m: float = Field(ge=0, le=30, allow_inf_nan=False)


class PathGeometry(Model):
    type: Literal["LineString"]
    coordinates: list[
        tuple[
            Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)],
            Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)],
        ]
    ] = Field(min_length=2)


class PathFeature(Model):
    type: Literal["Feature"]
    id: str
    geometry: PathGeometry
    properties: PathProperties


class PathCollection(Model):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[PathFeature] = []


class Omission(Model):
    leg_index: int
    reason: str
    maximum_offset_m: float | None = None


class StopResult(Model):
    platform: Platform
    status: Literal["confirmed", "no_candidate", "restricted", "unavailable", "timeout"]
    itinerary: NetworkJourney | None = None
    native_candidates: int = Field(default=0, ge=0, le=5)
    rejected_candidates: int = Field(default=0, ge=0, le=5)
    path_omissions: list[Omission] = []


class NetworkResult(Model):
    schema_version: Literal["2"] = "2"
    status: Literal[
        "ok", "partial", "unsupported_date", "unavailable", "timeout", "outside", "canceled"
    ]
    request: NetworkRequest
    graph_version: str
    method: Literal["native-stop-pilot-v2"] = "native-stop-pilot-v2"
    path_method: Literal["native-calls-end-clock-v2"] = "native-calls-end-clock-v2"
    destination_set_version: str
    total_platforms: int = Field(ge=1, le=32)
    max_duration_seconds: Literal[2700] = 2700
    native_candidates_per_destination: Literal[5] = 5
    computed_at: AwareDatetime
    query_seconds: float = Field(ge=0, allow_inf_nan=False)
    cache_hit: bool = False
    stops: list[StopResult] = []
    paths: PathCollection = PathCollection()
    source_ids: list[str] = ["ztp_gtfs_a", "ztp_gtfs_m", "ztp_gtfs_t", "osm"]
    explanation: str


def normalize_journey(node, departure):
    base = normalize_itinerary(node, departure)
    legs = []
    for leg, raw in zip(base.legs, node["legs"], strict=True):
        position = (raw["from"].get("stopPosition") or {}).get("position")
        calls = (
            [
                StopCall(
                    stop_id=call["stopLocation"]["gtfsId"],
                    name=call["stopLocation"]["name"],
                    longitude=call["stopLocation"]["lon"],
                    latitude=call["stopLocation"]["lat"],
                    position=position + index,
                    arrival=call["schedule"]["time"]["arrival"],
                    departure=call["schedule"]["time"]["departure"],
                )
                for index, call in enumerate(raw["stopCalls"])
            ]
            if raw["mode"] != "WALK"
            else []
        )
        legs.append(
            NetworkLeg(
                **leg.model_dump(exclude={"trip_id"}),
                trip_id=(raw["trip"] or {}).get("gtfsId"),
                direction_id=(raw["trip"] or {}).get("directionId"),
                headsign=raw["headsign"],
                from_stop_id=(raw["from"].get("stop") or {}).get("gtfsId"),
                to_stop_id=(raw["to"].get("stop") or {}).get("gtfsId"),
                from_position=position,
                to_position=(raw["to"].get("stopPosition") or {}).get("position"),
                pickup_type=raw["pickupType"],
                dropoff_type=raw["dropoffType"],
                calls=calls,
            )
        )
    first, last = node["legs"][0]["from"], node["legs"][-1]["to"]
    return NetworkJourney(
        **base.model_dump(exclude={"legs"}),
        legs=legs,
        effective_origin=Coordinate(longitude=first["lon"], latitude=first["lat"]),
        effective_destination=Coordinate(longitude=last["lon"], latitude=last["lat"]),
    )


class NetworkAdapter:
    def __init__(self, transport=None, platforms=None, timeout=8.0, capacity=8, ttl=300):
        self.base = RouteAdapter(transport=transport)
        self.platforms = [
            Platform.model_validate(value) for value in (platforms or MANIFEST["platforms"])
        ]
        if not 1 <= len(self.platforms) <= LIMIT:
            raise ValueError("Bounded destination set required")
        self.transport, self.timeout, self.capacity, self.ttl = transport, timeout, capacity, ttl
        self.destination_version = hashlib.sha256(
            json.dumps(
                [p.model_dump() for p in self.platforms], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        self.semaphore = asyncio.Semaphore(2)
        self.active = 0
        self.cache = OrderedDict()

    def result(self, body, status, explanation, started, **values):
        return NetworkResult(
            request=body,
            status=status,
            graph_version=self.base.graph_version,
            destination_set_version=self.destination_version,
            total_platforms=len(self.platforms),
            computed_at=datetime.now(UTC),
            query_seconds=time.monotonic() - started,
            explanation=explanation,
            **values,
        )

    async def plan(self, body, disconnected=None):
        started = time.monotonic()
        if body.departure.date().isoformat() not in self.base.service_dates:
            return self.result(
                body, "unsupported_date", "Brak pełnego pokrycia rozkładów dla tej daty.", started
            )
        if not self.base.snapshot_matches() or MANIFEST["graph_version"] != self.base.graph_version:
            return self.result(
                body, "unavailable", "Graf i źródła nie odpowiadają zapisanej próbie.", started
            )
        key = (
            self.base.graph_version,
            self.destination_version,
            METHOD,
            PATH_METHOD,
            hashlib.sha256(QUERY.encode()).hexdigest(),
            4.8,
            2700,
            body.model_dump_json(),
        )
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < self.ttl:
            self.cache.move_to_end(key)
            return cached[1].model_copy(
                update={"cache_hit": True, "query_seconds": time.monotonic() - started}
            )
        if self.active >= 2:
            return self.result(
                body,
                "unavailable",
                "Dwa zapytania są już obliczane. Spróbuj ponownie za chwilę.",
                started,
            )
        self.active += 1
        records, features = {}, []
        canceled = False

        async def query(client, platform):
            async with self.semaphore:
                try:
                    response = await client.post(
                        ENDPOINT,
                        json={
                            "query": QUERY,
                            "variables": {
                                "origin": {"location": {"coordinate": body.origin.model_dump()}},
                                "destination": {
                                    "location": {"stopLocation": {"stopLocationId": platform.id}}
                                },
                                "dateTime": {"earliestDeparture": body.departure.isoformat()},
                            },
                        },
                    )
                    response.raise_for_status()
                    raw = response.json()
                    if raw.get("errors"):
                        raise ValueError("Native query failed")
                    connection = raw["data"]["planConnection"]
                    if instant(connection["searchDateTime"]) != body.departure:
                        raise ValueError("Native departure changed")
                    nodes = [edge["node"] for edge in connection["edges"] or []]
                    accepted, restricted, rejected = [], False, 0
                    for node in nodes:
                        try:
                            summary = verify(
                                node, body.departure.isoformat(), platform.model_dump()
                            )
                            first = node["legs"][0]["from"]
                            if (
                                Point(TO_METRIC.transform(first["lon"], first["lat"])).distance(
                                    Point(
                                        TO_METRIC.transform(
                                            body.origin.longitude, body.origin.latitude
                                        )
                                    )
                                )
                                > 30
                            ):
                                raise ValueError("Native effective origin differs from request")
                            accepted.append((summary["elapsed_seconds"], node))
                        except (ValueError, KeyError, TypeError) as error:
                            rejected += 1
                            restricted |= isinstance(error, ValueError) and str(error).startswith(
                                "Conditional boarding"
                            )
                    if accepted:
                        chosen = min(accepted, key=lambda item: item[0])[1]
                        path, omissions = journey_features(
                            chosen, body.departure.isoformat(), platform.id
                        )
                        result = StopResult(
                            platform=platform,
                            status="confirmed",
                            itinerary=normalize_journey(chosen, body.departure.isoformat()),
                            native_candidates=len(nodes),
                            rejected_candidates=rejected,
                            path_omissions=omissions,
                        )
                        validated = [PathFeature.model_validate(feature) for feature in path]
                        features.extend(validated)
                    else:
                        result = StopResult(
                            platform=platform,
                            status="no_candidate"
                            if not nodes
                            else "restricted"
                            if restricted
                            else "unavailable",
                            native_candidates=len(nodes),
                            rejected_candidates=rejected,
                        )
                except (TimeoutError, httpx.TimeoutException):
                    result = StopResult(platform=platform, status="timeout")
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    result = StopResult(platform=platform, status="unavailable")
                records[platform.id] = result

        async def watch(group):
            nonlocal canceled
            while True:
                if await disconnected():
                    canceled = True
                    group.cancel()
                    return
                await asyncio.sleep(0.15)

        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout) as client:
                group = asyncio.gather(*(query(client, platform) for platform in self.platforms))
                watcher = asyncio.create_task(watch(group)) if disconnected else None
                try:
                    async with asyncio.timeout(self.timeout):
                        await group
                except TimeoutError:
                    pass
                except asyncio.CancelledError:
                    if not canceled:
                        raise
                finally:
                    if watcher:
                        watcher.cancel()
                        with suppress(asyncio.CancelledError):
                            await watcher
            if canceled:
                return self.result(body, "canceled", "Zapytanie zostało anulowane.", started)
            stops = [
                records.get(p.id, StopResult(platform=p, status="timeout")) for p in self.platforms
            ]
            failures = [s for s in stops if s.status in {"unavailable", "timeout"}]
            status = (
                "ok"
                if not failures
                else "partial"
                if len(failures) < len(stops)
                else "timeout"
                if all(s.status == "timeout" for s in stops)
                else "unavailable"
            )
            result = self.result(
                body,
                status,
                (
                    f"Próba {len(self.platforms)} wybranych platform; potwierdzone połączenia "
                    "według rozkładu. To nie pełne pokrycie miasta ani dowód najkrótszego czasu."
                ),
                started,
                stops=stops,
                paths=PathCollection(features=features),
            )
            if status == "ok":
                self.cache[key] = (time.monotonic(), result)
                self.cache.move_to_end(key)
                while len(self.cache) > self.capacity:
                    self.cache.popitem(last=False)
            return result
        finally:
            self.active -= 1


@router.post("/transit-network", response_model=NetworkResult)
async def transit_network(body: NetworkRequest, request: Request):
    with closing(database()) as connection:
        if body.data_version != version(connection):
            raise HTTPException(409, "Wersja danych zmieniła się. Wybierz miejsce ponownie.")
        inside = any(
            from_wkb(row[0]).covers(Point(body.origin.longitude, body.origin.latitude))
            for row in connection.execute("SELECT geometry FROM districts")
        )
    if not hasattr(request.app.state, "network_adapter"):
        request.app.state.network_adapter = NetworkAdapter()
    adapter = request.app.state.network_adapter
    if not inside:
        return adapter.result(
            body, "outside", "Punkt znajduje się poza granicami Krakowa.", time.monotonic()
        )
    return await adapter.plan(body, request.is_disconnected)
