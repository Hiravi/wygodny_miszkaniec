"""Scheduled point routes via the actual OTP API; isochrones remain a separate gate."""

import asyncio
import json
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from shapely import from_wkb
from shapely.geometry import Point

from services.api.locations import database, version
from tools.data.acquire import sha256
from tools.release.response_cache import ResponseCache
from tools.router.normalize import summarize
from tools.router.walk_access import POLICY as WALK_ACCESS_POLICY
from tools.router.walk_access import WalkAccess

ROOT = Path(__file__).resolve().parents[2]
WARSAW = ZoneInfo("Europe/Warsaw")
ENDPOINT = "http://127.0.0.1:8800/otp/gtfs/v1"
QUERY = """query($origin:PlanLabeledLocationInput!,$destination:PlanLabeledLocationInput!,
 $dateTime:PlanDateTimeInput!,$modes:PlanModesInput!){planConnection(
 origin:$origin,destination:$destination,dateTime:$dateTime,first:5,modes:$modes,
 preferences:{street:{walk:{speed:1.3333333333333333}}}){
 searchDateTime edges{node{start end legs{mode distance serviceDate
 from{name lat lon departure{scheduledTime}} to{name lat lon arrival{scheduledTime}}
 route{gtfsId shortName} legGeometry{points}}}}}}"""
router = APIRouter(prefix="/api")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Coordinate(Model):
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)


class RouteRequest(Model):
    origin: Coordinate
    destination: Coordinate
    departure: AwareDatetime
    mode: Literal["transit", "walk"]
    data_version: str = Field(min_length=1, max_length=80)

    @field_validator("departure")
    @classmethod
    def warsaw_minute(cls, value: datetime) -> datetime:
        local = value.astimezone(WARSAW)
        if value.replace(tzinfo=None) != local.replace(tzinfo=None):
            raise ValueError("Use a valid Europe/Warsaw local time and its UTC offset")
        if value.second or value.microsecond:
            raise ValueError("Choose a whole departure minute")
        return local


class Leg(Model):
    mode: Literal["WALK", "BUS", "TRAM"]
    departure: AwareDatetime
    arrival: AwareDatetime
    from_name: str
    to_name: str
    route_name: str | None
    route_id: str | None
    distance_m: float = Field(ge=0, allow_inf_nan=False)
    geometry_polyline: str
    service_date: str | None
    trip_id: str | None = None


class Itinerary(Model):
    arrival: AwareDatetime
    elapsed_seconds: int = Field(ge=0)
    travel_seconds: int = Field(ge=0)
    waiting_seconds: int = Field(ge=0)
    transfers: int = Field(ge=0)
    walking_only: bool
    network_only: bool = False
    legs: list[Leg] = Field(min_length=1)


class RouteResult(Model):
    schema_version: Literal["1"] = "1"
    status: Literal[
        "ok",
        "unsupported_date",
        "no_route",
        "unavailable",
        "timeout",
        "outside",
        "insufficient_coverage",
    ]
    request: RouteRequest
    effective_origin: Coordinate | None = None
    effective_destination: Coordinate | None = None
    effective_departure: AwareDatetime | None = None
    origin_access_confirmed: bool | None = None
    destination_access_confirmed: bool | None = None
    origin_link_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    destination_link_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    graph_version: str
    routing_version: Literal["otp-2.10-point-v1", "r5-exact-scheduled-point-v1"] = (
        "otp-2.10-point-v1"
    )
    speed_walking_kmh: Literal[4.8] = 4.8
    source_ids: list[str] = ["ztp_gtfs_a", "ztp_gtfs_m", "ztp_gtfs_t", "osm"]
    computed_at: AwareDatetime
    cache_hit: bool = False
    itineraries: list[Itinerary] = []
    explanation: str
    isochrone_status: Literal["blocked"] = "blocked"


def normalize_itinerary(node, departure) -> Itinerary:
    summary = summarize(node, departure)
    legs = []
    for leg in node["legs"]:
        route = leg.get("route")
        legs.append(
            Leg(
                mode=leg["mode"],
                departure=leg["from"]["departure"]["scheduledTime"],
                arrival=leg["to"]["arrival"]["scheduledTime"],
                from_name=leg["from"]["name"] or "Punkt początkowy",
                to_name=leg["to"]["name"] or "Punkt docelowy",
                route_name=route.get("shortName") if route else None,
                route_id=route.get("gtfsId") if route else None,
                distance_m=leg["distance"],
                geometry_polyline=leg["legGeometry"]["points"],
                service_date=leg.get("serviceDate"),
            )
        )
    return Itinerary(
        arrival=node["end"],
        walking_only=all(leg.mode == "WALK" for leg in legs),
        legs=legs,
        **{k: int(v) for k, v in summary.items()},
    )


class RouteAdapter:
    def __init__(self, transport=None, timeout=8.0, capacity=64, ttl=300):
        self.transport = transport
        self.timeout = timeout
        self.capacity = capacity
        self.ttl = ttl
        self.cache = ResponseCache(max_entries=capacity)
        self.semaphore = asyncio.Semaphore(2)
        manifest = json.loads(
            (ROOT / "data/manifests/otp-point-spike-v1.json").read_text(encoding="utf-8")
        )
        coverage = json.loads(
            (ROOT / "data/manifests/gtfs-coverage-v1.json").read_text(encoding="utf-8")
        )
        self.graph_version = manifest["graph_sha256"]
        self.service_dates = set(coverage["common_active_service_dates"])
        self.input_hashes = manifest["input_sha256"]
        self.walk_access = None
        graph = ROOT / ".cache/router/otp-inputs/graph.obj"
        self.graph = graph
        self.graph_signature = graph.stat().st_mtime_ns if graph.exists() else None
        self.graph_verified = transport is not None or (
            graph.exists() and sha256(graph) == self.graph_version
        )

    def snapshot_matches(self):
        if self.transport is not None:
            return True  # Explicit test injection; normal startup has no transport override.
        if not self.graph.exists() or self.graph.stat().st_mtime_ns != self.graph_signature:
            return False
        for source, expected in self.input_hashes.items():
            filename = "router-runtime" if source == "osm" else source
            manifest = json.loads(
                (ROOT / f"data/manifests/{filename}.json").read_text(encoding="utf-8")
            )
            actual = manifest["osm_sha256"] if source == "osm" else manifest["sha256"]
            if actual != expected:
                return False
        return self.graph_verified

    def result(self, request, status, explanation, **values):
        return RouteResult(
            request=request,
            status=status,
            explanation=explanation,
            graph_version=self.graph_version,
            computed_at=datetime.now(UTC),
            **values,
        )

    async def plan(self, request: RouteRequest) -> RouteResult:
        if (
            request.mode == "transit"
            and request.departure.date().isoformat() not in self.service_dates
        ):
            return self.result(
                request, "unsupported_date", "Brak pełnego pokrycia rozkładów dla tej daty."
            )
        if not self.snapshot_matches():
            return self.result(request, "unavailable", "Graf tras nie odpowiada wersji danych.")
        key = (
            self.graph_version,
            "otp-2.10-point-v1",
            WALK_ACCESS_POLICY,
            4.8,
            request.model_dump_json(),
        )
        cached = self.cache.get(key)
        if cached and time.monotonic() - cached[0] < self.ttl:
            self.cache.move_to_end(key)
            return cached[1].model_copy(update={"cache_hit": True})
        try:
            async with asyncio.timeout(self.timeout):
                async with self.semaphore:
                    async with httpx.AsyncClient(
                        transport=self.transport, timeout=self.timeout
                    ) as client:
                        modes = {"direct": ["WALK"], "directOnly": request.mode == "walk"}
                        if request.mode == "transit":
                            modes["transit"] = {"transit": [{"mode": "BUS"}, {"mode": "TRAM"}]}
                        variables = {
                            "origin": {"location": {"coordinate": request.origin.model_dump()}},
                            "destination": {
                                "location": {"coordinate": request.destination.model_dump()}
                            },
                            "dateTime": {"earliestDeparture": request.departure.isoformat()},
                            "modes": modes,
                        }
                        response = await client.post(
                            ENDPOINT, json={"query": QUERY, "variables": variables}
                        )
                        response.raise_for_status()
                        raw = response.json()
            if raw.get("errors"):
                return self.result(
                    request, "unavailable", "Router zwrócił nieprawidłową odpowiedź."
                )
            connection = raw["data"]["planConnection"]
            nodes = [edge["node"] for edge in connection["edges"] or []]
            if nodes:
                if self.walk_access is None:
                    try:
                        self.walk_access = WalkAccess.load(ROOT, self.input_hashes["osm"])
                    except OSError:
                        return self.result(
                            request,
                            "insufficient_coverage",
                            "Brak danych do potwierdzenia dostępu pieszego. Przygotuj dane barier.",
                        )
                # OTP polyline precision1e-5deg has <0.7m rounding displacement
                # in this source region. Within0.75m the crossing is uncertain,
                # so reject proof rather than assert a legal public passage.
                nodes = [
                    node
                    for node in nodes
                    if all(self.walk_access.allows_otp_leg(leg) for leg in node["legs"])
                ]
                if not nodes:
                    return self.result(
                        request,
                        "insufficient_coverage",
                        "Brak potwierdzonego przejścia pieszego: warianty przecinają "
                        "oznaczoną barierę lub jej strefę błędu geometrii. "
                        "To nie dowodzi braku trasy.",
                    )
            if not nodes:
                result = self.result(
                    request, "no_route", "Nie znaleziono połączenia dla tych parametrów."
                )
            else:
                itineraries = sorted(
                    [normalize_itinerary(node, request.departure.isoformat()) for node in nodes],
                    key=lambda i: i.elapsed_seconds,
                )
                first_node = min(
                    nodes,
                    key=lambda n: summarize(n, request.departure.isoformat())["elapsed_seconds"],
                )
                first = first_node["legs"][0]["from"]
                last = first_node["legs"][-1]["to"]
                result = self.result(
                    request,
                    "ok",
                    "Rozkład statyczny; czas obejmuje dojścia, oczekiwanie i przesiadki.",
                    effective_origin=Coordinate(longitude=first["lon"], latitude=first["lat"]),
                    effective_destination=Coordinate(longitude=last["lon"], latitude=last["lat"]),
                    effective_departure=connection["searchDateTime"],
                    itineraries=itineraries,
                )
                if request.mode == "walk" and any(not i.walking_only for i in itineraries):
                    raise ValueError("Walk request returned transit")
                if result.effective_departure.timestamp() != request.departure.timestamp():
                    raise ValueError("Engine changed requested departure")
            self.cache[key] = (time.monotonic(), result)
            while len(self.cache) > self.capacity:
                self.cache.popitem(last=False)
            return result
        except (TimeoutError, httpx.TimeoutException):
            return self.result(
                request, "timeout", "Obliczenie trasy przekroczyło limit czasu. Spróbuj ponownie."
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return self.result(
                request, "unavailable", "Usługa tras jest niedostępna. Spróbuj ponownie."
            )


@router.post("/routes", response_model=RouteResult)
async def plan_route(body: RouteRequest, request: Request):
    with closing(database()) as connection:
        if body.data_version != version(connection):
            raise HTTPException(409, "Wersja danych zmieniła się. Wybierz lokalizację ponownie.")
        inside = any(
            from_wkb(row[0]).covers(Point(body.origin.longitude, body.origin.latitude))
            for row in connection.execute("SELECT geometry FROM districts")
        )
    if not hasattr(request.app.state, "route_adapter"):
        from services.api.native_routing import R5PreferredRouteAdapter

        request.app.state.route_adapter = R5PreferredRouteAdapter()
    adapter = request.app.state.route_adapter
    if not inside:
        return adapter.result(
            body, "outside", "Punkt początkowy znajduje się poza granicami Krakowa."
        )
    return await adapter.plan(body)
