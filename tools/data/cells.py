"""Stable pilot grid and strict eligibility of source-mapped walking access."""

import math

from pyproj import Transformer

SIZE = 250
ACCESS_VERSION = "public-node-native-walk-v1"
TO_METRIC = Transformer.from_crs(4326, 2180, always_xy=True)
TO_WGS = Transformer.from_crs(2180, 4326, always_xy=True)
WALKABLE = {
    "footway",
    "pedestrian",
    "path",
    "steps",
    "living_street",
    "residential",
    "unclassified",
    "service",
}
PUBLIC = {"yes", "designated", "permissive"}


def grid_index(longitude, latitude):
    x, y = TO_METRIC.transform(longitude, latitude)
    return math.floor(x / SIZE), math.floor(y / SIZE)


def cell_id(longitude, latitude):
    x, y = grid_index(longitude, latitude)
    return f"2180:{x}:{y}"


def eligible_way(tags):
    # Explicit public foot permission required; no assumption from highway class alone.
    access, foot = tags.get("access"), tags.get("foot")
    return (
        tags.get("highway") in WALKABLE
        and foot in (None, *PUBLIC)
        and access in (None, "yes", "permissive", "destination")
        and (foot in PUBLIC or access in {"yes", "permissive"})
        and not any(
            ":" in key and (key.startswith("foot:") or key.startswith("access:")) for key in tags
        )
        and tags.get("construction") is None
        and tags.get("seasonal") not in {"yes", "winter", "summer"}
        and tags.get("opening_hours") is None
    )


def decode_polyline(encoded):
    """Decode the standard Google 1e-5-degree encoding returned by OTP."""
    values, cursor, latitude, longitude = [], 0, 0, 0
    while cursor < len(encoded):
        deltas = []
        for _ in range(2):
            number, shift = 0, 0
            while True:
                if cursor >= len(encoded) or shift > 30:
                    raise ValueError("Malformed native polyline")
                byte = ord(encoded[cursor]) - 63
                cursor += 1
                if not 0 <= byte <= 63:
                    raise ValueError("Malformed native polyline")
                number |= (byte & 31) << shift
                shift += 5
                if byte < 32:
                    break
            deltas.append(~(number >> 1) if number & 1 else number >> 1)
        latitude += deltas[0]
        longitude += deltas[1]
        values.append((longitude / 1e5, latitude / 1e5))
    return values
