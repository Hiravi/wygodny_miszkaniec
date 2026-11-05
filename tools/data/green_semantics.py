"""Source vegetation, recreational designation, ownership and mode permissions are independent."""

OPEN = {"yes", "designated", "permissive", "official"}
RESTRICTED = {
    "no",
    "private",
    "customers",
    "permit",
    "destination",
    "agricultural",
    "forestry",
    "delivery",
}


def permission(tags, mode):
    value = tags.get(mode, tags.get("access"))
    if value in RESTRICTED:
        return "restricted"
    if any(k.startswith(("access:", mode + ":")) for k in tags):
        return "unknown"
    if tags.get("opening_hours") not in (None, "24/7") or tags.get("seasonal"):
        return "restricted"  # Time/access conditions have not been evaluated for now.
    return "open" if value in OPEN else "unknown"


def properties(tags):
    vegetation = (
        "wood"
        if tags.get("natural") == "wood" or tags.get("landuse") == "forest"
        else "scrub"
        if tags.get("natural") == "scrub"
        else "grass"
        if tags.get("natural") == "grassland"
        or tags.get("landuse") in {"grass", "meadow", "recreation_ground", "village_green"}
        else "unknown"
    )
    recreation = (
        "park"
        if tags.get("leisure") == "park"
        else ("recreation_ground" if tags.get("landuse") == "recreation_ground" else "unknown")
    )
    return {
        "vegetation": vegetation,
        "recreation": recreation,
        "foot_access": permission(tags, "foot"),
        "bicycle_access": permission(tags, "bicycle"),
        "access_basis": "Mode tag then access; no ownership inference; conditions unevaluated",
        "source_access_tags": {
            k: v
            for k, v in tags.items()
            if k
            in {"access", "foot", "bicycle", "ownership", "opening_hours", "seasonal", "barrier"}
            or k.startswith(("access:", "foot:", "bicycle:"))
        },
    }
