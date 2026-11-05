"""Separate park/forest walking evidence; whole-area permission is not inferred from a path."""

import json

from fastapi import APIRouter, HTTPException, Request
from shapely.geometry import LineString, Point, shape

from services.api.locations import DATA
from services.api.park_zone import follows_public_network
from services.api.parks import ParkAdapter, ParkRequest, metric, walking_access
from tools.data.acquire import ROOT, sha256
from tools.data.cells import decode_polyline
from tools.data.green_semantics import permission

router = APIRouter(prefix="/api")
NAME = "green-walk-v36"
CITY_NAME = "green-walk-city-v37"


def current_name():
    return CITY_NAME if (DATA / f"{CITY_NAME}.json").is_file() else NAME


class GreenWalkAdapter(ParkAdapter):
    method = NAME

    def __init__(self, base=None, dataset=None):
        self.method = dataset or current_name()
        super().__init__(base, self.method)
        self.gates = [shape(g["point"]) for g in self.public_gates]

    def select_targets(self, origin):
        point = metric(Point(origin.longitude, origin.latitude))
        ranked = sorted(
            self.targets, key=lambda t: (point.distance(metric(Point(t["entrance"]))), t["id"])
        )
        place_ids = []
        for kind in ("park", "forest"):
            for target in ranked:
                if target["kind"] == kind and target["park_id"] not in place_ids:
                    place_ids.append(target["park_id"])
                    if (
                        sum(
                            next(t["kind"] for t in ranked if t["park_id"] == i) == kind
                            for i in place_ids
                        )
                        == 3
                    ):
                        break
        # Up to3 parks/3 woods, two actual entrance candidates each; no nearest-time claim.
        return [
            target
            | ({"park": self.place_geometries[target["park_id"]]} if self.place_geometries else {})
            for identifier in place_ids
            for target in [t for t in ranked if t["park_id"] == identifier][:2]
        ]

    def proof(self, origin, target, result):
        area_access = target["park_tags"]["foot_access"]
        entry_access = permission(target["way_tags"], "foot")
        if (
            area_access == "restricted"
            or entry_access == "restricted"
            or (area_access == "unknown" and entry_access != "open")
        ):
            return None
        proof = super().proof(origin, target, result)
        if proof is None:
            return None
        coordinates = [
            p for leg in result.itineraries[0].legs for p in decode_polyline(leg.geometry_polyline)
        ]
        line = metric(LineString(coordinates))
        if not follows_public_network(self, line):
            return None
        for i in self.barrier_tree.query(line, predicate="intersects"):
            barrier = self.barriers[i]
            if barrier.geom_type == "Point":
                return None
            intersection = barrier.intersection(line)
            points = (
                list(intersection.geoms)
                if intersection.geom_type == "MultiPoint"
                else [intersection]
            )
            if any(
                p.geom_type != "Point" or not any(p.distance(g) <= 0.75 for g in self.gates)
                for p in points
            ):
                return None
        proof.update(
            place_kind=target["kind"],
            foot_access=target["park_tags"]["foot_access"],
            entrance_provenance=target["provenance"],
            source_way_id=target["way_id"],
            public_gate_ids=target["gate_ids_near_entrance"],
            access_scope=(
                "Source-mapped public network to a specific entry; not all land in the area"
            ),
            network_path_verified=True,
        )
        return proof


@router.get("/green-walk-coverage")
def coverage():
    name = current_name()
    path = ROOT / f"data/manifests/{name}.json"
    if not path.is_file() or not (DATA / f"{name}.json").is_file():
        raise HTTPException(503, "Katalog dojść do parków i lasów jest niedostępny.")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if name == CITY_NAME:
            sidecar = DATA / manifest["coverage_file"].split("/")[-1]
            if sha256(sidecar) != manifest["coverage_sha256"]:
                raise ValueError("Coverage version mismatch")
            return json.loads(sidecar.read_text("utf-8"))
        return manifest["coverage"]
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "Wersja zakresu dojść jest niedostępna.") from None


@router.post("/green-walk")
async def green_walk(body: ParkRequest, request: Request):
    return await walking_access(body, request, "green_walk_adapter", GreenWalkAdapter)
