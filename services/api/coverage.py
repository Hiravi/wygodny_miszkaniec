"""Validated public adapter for the serial loopback one-to-many worker."""

import asyncio
import json
from contextlib import closing
from datetime import timedelta
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, Field, field_validator, model_validator
from shapely import from_wkb
from shapely.geometry import Point

from services.api.coverage_routes import RouteNetwork
from services.api.locations import ROOT, database, version
from services.api.routing import Coordinate, Model, RouteRequest
from tools.router.final_walk import seconds_band
from tools.router.reachable_routes import ROUTE_METHOD
from tools.router.service_days import BASE_SECONDS, anchor, query_clock
from tools.router.service_days import METHOD as SERVICE_METHOD
from tools.router.service_days import ROUTE_METHOD as SERVICE_ROUTE_METHOD

router = APIRouter(prefix="/api")
METHOD = "r5-7.5.1-minute-grid150-v3"


class CoverageRequest(Model):
    origin: Coordinate
    departure: AwareDatetime
    data_version: str = Field(min_length=1, max_length=80)

    @field_validator("departure")
    @classmethod
    def warsaw_minute(cls, value):
        return RouteRequest.warsaw_minute(value)


class CoverageCounts(Model):
    grid_m: Literal[150]
    grid_samples: int = Field(ge=0)
    rejected_grid_samples: int = Field(ge=0)
    platforms: int = Field(ge=0)
    regional_platforms: int = Field(ge=0)
    routes: int = Field(ge=0)
    verified_transfer_pairs: int | None = Field(default=None, ge=0)
    omitted_transfer_pairs: int | None = Field(default=None, ge=0)
    egress_searches: int | None = Field(default=None, ge=0)
    direct_searches: int | None = Field(default=None, ge=0)
    confirmed_points: int | None = Field(default=None, ge=0)


class CoverageProperties(Model):
    id: str
    kind: Literal["sample", "stop"]
    name: str
    longitude: float
    latitude: float
    minutes_floor: int | None = Field(ge=0, lt=45)
    band: Literal[15, 30, 45] | None
    estimated_band: Literal[15, 30, 45] | None = None
    confirmation: Literal["confirmed", "approximate", "unknown"] = "unknown"
    confirmed_seconds: int | None = Field(default=None, ge=0, le=2700)
    final_walk_seconds: int | None = Field(default=None, ge=0, le=2700)
    egress_stop: str | None = None
    witness_id: str | None = None
    section_id: str | None = None
    candidate_seconds: int | None = Field(default=None, ge=0, le=2700)
    endpoint_link_m: float | None = Field(default=None, ge=0)


class CoverageFeature(Model):
    type: Literal["Feature"]
    geometry: dict
    properties: CoverageProperties

    @model_validator(mode="after")
    def confirmed_point_has_raw_network_time(self):
        p = self.properties
        if p.confirmation == "confirmed" and (
            p.confirmed_seconds is None
            or p.band != seconds_band(p.confirmed_seconds)
            or p.final_walk_seconds is None
            or p.final_walk_seconds > p.confirmed_seconds
            or self.geometry.get("type") != "Point"
        ):
            raise ValueError("Confirmation requires raw seconds and one network point")
        return self


class CoverageFeatures(Model):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[CoverageFeature] = Field(default_factory=list, max_length=30000)


class CoverageResult(Model):
    schema_version: Literal["4"] = "4"
    routes: RouteNetwork | None = None
    routes_error: str | None = None
    method: Literal["r5-7.5.1-minute-grid150-v3", "r5-7.5.1-service-days-grid150-v4"] = METHOD
    data_version: str
    graph_version: str = "unavailable"
    request: CoverageRequest
    cache_hit: bool = False
    effective_origin: Coordinate | None = None
    suggested_origin: Coordinate | None = None
    origin_link_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    origin_access_confirmed: bool | None = Field(default=None, strict=True)
    features: CoverageFeatures = Field(default_factory=CoverageFeatures)
    coverage: CoverageCounts | None = None
    status: Literal[
        "ok",
        "unsupported_date",
        "unsupported_time",
        "outside",
        "insufficient_coverage",
        "unavailable",
        "timeout",
    ]
    explanation: str
    calculation_seconds: float | None = None
    rss_bytes: int | None = None

    @model_validator(mode="after")
    def same_route_context(self):
        origin_fields = (self.effective_origin, self.origin_link_m, self.origin_access_confirmed)
        if any(value is not None for value in origin_fields):
            if any(value is None for value in origin_fields) or (
                self.origin_access_confirmed != (self.origin_link_m <= 0.02)
            ):
                raise ValueError("Inconsistent coverage origin link proof")
        if self.routes:
            if (
                self.coverage is None
                or self.routes.regional_platforms != self.coverage.regional_platforms
            ):
                raise ValueError("Route/grid graph coverage differs")
            departure = self.request.departure
            multi_day = self.method == SERVICE_METHOD
            clock = (
                query_clock(departure)
                if multi_day
                else departure.hour * 3600 + departure.minute * 60
            )
            if multi_day and (
                self.routes.method != SERVICE_ROUTE_METHOD
                or self.routes.native_timeline_start
                != anchor(departure.date()) - timedelta(seconds=BASE_SECONDS)
            ):
                raise ValueError("Route timeline belongs to another query")
            source_dates = {
                (departure.date() + timedelta(days=shift)).isoformat() for shift in (-1, 0, 1)
            }
            for witness in self.routes.witnesses.values():
                first = witness.legs[0]
                if (
                    first.board_clock - first.walk_seconds - first.verified_wait_seconds != clock
                    or any(
                        leg.service_date not in source_dates
                        if multi_day
                        else leg.service_date != departure.date().isoformat()
                        for leg in witness.legs
                    )
                ):
                    raise ValueError("Route witness belongs to another departure")
        return self


class CoverageAdapter:
    def __init__(self, transport=None, timeout=20):
        self.transport, self.timeout = transport, timeout
        self.semaphore = asyncio.Semaphore(1)

    async def calculate(self, body, include_samples=False):
        fallback = {"request": body, "data_version": body.data_version}
        try:
            async with asyncio.timeout(self.timeout):
                async with self.semaphore:
                    async with httpx.AsyncClient(
                        transport=self.transport, timeout=self.timeout
                    ) as client:
                        health = await client.get("http://127.0.0.1:8810/health")
                        health.raise_for_status()
                        metadata = health.json()
                        proof = json.loads(
                            (ROOT / "data/manifests/otp-point-spike-v1.json").read_text()
                        )
                        expected = {
                            ("malopolskie-261011.osm.pbf" if k == "osm" else k + ".zip"): v
                            for k, v in proof["input_sha256"].items()
                            if k == "osm" or k.startswith("ztp_gtfs_")
                        }
                        if (
                            (metadata["method"], metadata.get("route_method"))
                            not in {(METHOD, ROUTE_METHOD), (SERVICE_METHOD, SERVICE_ROUTE_METHOD)}
                            or metadata.get("schema_version") != "4"
                            or metadata["sources"] != expected
                            or metadata["data_version"] != body.data_version
                        ):
                            raise ValueError("One-to-many source/runtime mismatch")
                        response = await client.post(
                            "http://127.0.0.1:8810/calculate",
                            json=body.model_dump(mode="json")
                            | {"include_samples": include_samples},
                        )
                        response.raise_for_status()
                        value = CoverageResult.model_validate(response.json())
                        if (
                            value.request != body
                            or value.method != metadata["method"]
                            or value.graph_version != metadata["graph_version"]
                        ):
                            raise ValueError("Mismatched one-to-many response")
                        return value
        except (TimeoutError, httpx.TimeoutException):
            return CoverageResult(
                **fallback,
                status="timeout",
                explanation="Obliczenie zasięgu przekroczyło limit czasu. Ponów zasięg. "
                "Wybrane miejsce i mapa pozostają dostępne; możesz też sprawdzić Trasę A→B.",
            )
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            return CoverageResult(
                **fallback,
                status="unavailable",
                explanation="Silnik zasięgu jest niedostępny lub ma inną wersję danych. "
                "Trasy A→B i pozostała mapa działają niezależnie.",
            )


@router.post("/transit-coverage", response_model=CoverageResult)
async def coverage(body: CoverageRequest, request: Request, samples: bool = False):
    with closing(database()) as db:
        if body.data_version != version(db):
            raise HTTPException(409, "Wersja danych zmieniła się. Wybierz miejsce ponownie.")
        if not any(
            from_wkb(r[0]).covers(Point(body.origin.longitude, body.origin.latitude))
            for r in db.execute("SELECT geometry FROM districts")
        ):
            return CoverageResult(
                request=body,
                data_version=body.data_version,
                status="outside",
                explanation="Wybierz punkt w Krakowie.",
            )
    if not hasattr(request.app.state, "coverage_adapter"):
        request.app.state.coverage_adapter = CoverageAdapter()
    return await request.app.state.coverage_adapter.calculate(body, include_samples=samples)
