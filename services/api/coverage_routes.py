"""Public contracts for native scheduled-hop witnesses, separate from grid times."""

from datetime import date, timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from services.api.network import PathGeometry
from services.api.routing import Model
from tools.router.reachable_routes import ROUTE_METHOD
from tools.router.service_days import ROUTE_METHOD as SERVICE_ROUTE_METHOD
from tools.router.service_days import anchor


class RouteSectionProperties(Model):
    id: str
    method: Literal["r5-native-scheduled-hops-v31", "r5-native-service-days-hops-v4"] = ROUTE_METHOD
    mode: Literal["BUS", "TRAM"]
    route_id: str
    route_name: str
    direction: str
    headsign: str
    service_date: str
    trip_id: str
    witness_id: str
    leg_index: int = Field(ge=0, le=7)
    transfers: int = Field(ge=0, le=7)
    from_stop: str
    to_stop: str
    to_name: str
    to_longitude: float = Field(ge=-180, le=180)
    to_latitude: float = Field(ge=-90, le=90)
    alighting: bool
    start_cumulative_seconds: int = Field(ge=0, le=2700)
    cumulative_seconds: int = Field(ge=0, le=2700)
    band: Literal[15, 30, 45]


class RouteSection(Model):
    type: Literal["Feature"]
    id: str
    geometry: PathGeometry
    properties: RouteSectionProperties


class RouteSections(Model):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[RouteSection] = Field(default_factory=list, max_length=30000)


class WitnessLeg(Model):
    trip_id: str
    equivalent_trip_ids: list[str]
    route_id: str
    route_name: str
    mode: Literal["BUS", "TRAM"]
    direction: str
    headsign: str
    service_date: str
    board_stop: str
    alight_stop: str
    board_clock: int = Field(ge=0)
    alight_clock: int = Field(ge=0)
    wait_seconds: int = Field(ge=0)
    walk_seconds: int = Field(ge=0)
    verified_wait_seconds: int = Field(ge=60)
    ready_clock: int = Field(ge=0)
    source_board_clock: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    source_alight_clock: int | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    equivalent_service_dates: dict[str, str] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class RouteWitness(Model):
    access_seconds: int = Field(ge=0, le=2700)
    arrival_seconds: int = Field(ge=0, le=2700)
    legs: list[WitnessLeg] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def complete_clock(self):
        first = self.legs[0]
        origin_clock = first.board_clock - first.walk_seconds - first.verified_wait_seconds
        previous = origin_clock
        for leg in self.legs:
            if (
                leg.board_clock != previous + leg.walk_seconds + leg.verified_wait_seconds
                or leg.alight_clock < leg.board_clock
            ):
                raise ValueError("Incomplete native walking/waiting/ride clock")
            previous = leg.alight_clock
        if (
            previous - origin_clock != self.arrival_seconds
            or self.access_seconds != first.walk_seconds
        ):
            raise ValueError("Native witness elapsed/access mismatch")
        return self


class AncillaryCollection(Model):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[dict] = Field(default_factory=list, max_length=20000)


class RouteNetwork(Model):
    method: Literal["r5-native-scheduled-hops-v31", "r5-native-service-days-hops-v4"] = ROUTE_METHOD
    native_timeline_start: AwareDatetime | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    sections: RouteSections
    witnesses: dict[str, RouteWitness]
    walks: AncillaryCollection
    transfers: AncillaryCollection
    seconds: float = Field(ge=0)
    regional_platforms: int = Field(ge=0)
    access_platforms: int = Field(ge=0)
    omitted: dict[str, int]

    @model_validator(mode="after")
    def witnessed_sections(self):
        if self.method == SERVICE_ROUTE_METHOD:
            if self.native_timeline_start is None:
                raise ValueError("Missing native service-day timeline")
            for witness in self.witnesses.values():
                for leg in witness.legs:
                    if leg.source_board_clock is None or leg.source_alight_clock is None:
                        raise ValueError("Missing original GTFS clocks")
                    start = anchor(date.fromisoformat(leg.service_date))
                    if start + timedelta(
                        seconds=leg.source_board_clock
                    ) != self.native_timeline_start + timedelta(
                        seconds=leg.board_clock
                    ) or start + timedelta(
                        seconds=leg.source_alight_clock
                    ) != self.native_timeline_start + timedelta(seconds=leg.alight_clock):
                        raise ValueError("Source/native service-day clock mismatch")
                    if leg.equivalent_service_dates is not None and (
                        set(leg.equivalent_service_dates) != set(leg.equivalent_trip_ids)
                        or leg.equivalent_service_dates.get(leg.trip_id) != leg.service_date
                    ):
                        raise ValueError("Ambiguous equivalent source service dates")
        for section in self.sections.features:
            value = section.properties
            witness = self.witnesses.get(value.witness_id)
            if witness is None or value.leg_index >= len(witness.legs):
                raise ValueError("Missing native section witness")
            leg = witness.legs[value.leg_index]
            first = witness.legs[0]
            origin_clock = first.board_clock - first.walk_seconds - first.verified_wait_seconds
            if (
                value.trip_id != leg.trip_id
                or value.method != self.method
                or value.route_id != leg.route_id
                or value.mode != leg.mode
                or value.transfers != value.leg_index
                or value.start_cumulative_seconds > value.cumulative_seconds
                or value.band != next(n for n in (15, 30, 45) if value.cumulative_seconds <= n * 60)
                or value.cumulative_seconds > witness.arrival_seconds
                or value.service_date != leg.service_date
                or value.start_cumulative_seconds < leg.board_clock - origin_clock
                or value.cumulative_seconds > leg.alight_clock - origin_clock
            ):
                raise ValueError("Native section proof or clock mismatch")
        return self
