"""Reject uncertain WALK geometry at explicitly restricted source barrier nodes."""

import hashlib
import json

from pyproj import Transformer
from shapely import transform
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from tools.data.cells import decode_polyline
from tools.router.barrier_permissions import POLICY as SOURCE_POLICY

POLICY = "restricted-node-walk-geometry-v34"


class WalkAccess:
    def __init__(self, nodes):
        self.project = Transformer.from_crs(4326, 2180, always_xy=True).transform
        self.points = [Point(*self.project(n["longitude"], n["latitude"])) for n in nodes]
        self.tree = STRtree(self.points)

    @classmethod
    def load(cls, root, osm_hash):
        signature = hashlib.sha256(f"{SOURCE_POLICY}:{osm_hash}".encode()).hexdigest()
        path = root / f".cache/router/barrier-permissions-{signature}.json"
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def allows(self, coordinates, uncertainty_m):
        if len(coordinates) < 2:
            return False
        longitude, latitude = zip(*coordinates, strict=True)
        line = LineString(list(zip(*self.project(longitude, latitude), strict=True)))
        return self.tree.query(line, predicate="dwithin", distance=uncertainty_m).size == 0

    def allows_geometry(self, line, uncertainty_m):
        """Same metric guard for an already decoded, lossless native WALK line."""
        if line.geom_type != "LineString" or line.is_empty or len(line.coords) < 2:
            return False
        projected = transform(line, self.project, interleaved=False)
        return self.tree.query(projected, predicate="dwithin", distance=uncertainty_m).size == 0

    def allows_otp_leg(self, leg):
        if leg["mode"] != "WALK":
            return True
        if (
            leg["distance"] == 0
            and leg["from"]["lat"] == leg["to"]["lat"]
            and leg["from"]["lon"] == leg["to"]["lon"]
        ):
            return True  # Stationary stop linkage, not a barrier traversal.
        return self.allows(decode_polyline(leg["legGeometry"]["points"]), 0.75)
