"""Offer a separate network origin, never an invented walk from an address."""


def nearby_network_origin(street_layer, point, city):
    # The normal 30m admission rule is unchanged. This is an explicit alternative
    # selected by the user; no straight-line connector or travel time is added.
    candidate = street_layer.find_split(point, radius=200)
    if candidate.is_empty or not city.covers(candidate):
        return None
    return {"longitude": candidate.x, "latitude": candidate.y}
