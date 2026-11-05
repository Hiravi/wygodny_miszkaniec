"""Prepare immutable native destination geometry/linkages once per worker.

No origin, departure, service calendar or itinerary is cached here. R5 still
computes every requested time/target; only the fixed target objects are reused.
"""

from dataclasses import dataclass
from datetime import timedelta

POLICY = "native-stable-destination-linkage-v1"


def destination_key(frame):
    return tuple(zip(map(str, frame.id), frame.geometry.x, frame.geometry.y, strict=True))


@dataclass(frozen=True)
class PreparedDestinations:
    key: tuple
    point_sets: tuple

    def apply(self, request, destinations):
        if destination_key(destinations) != self.key:
            raise ValueError("Prepared destinations differ in ordered IDs or coordinates")
        # Retain point-set identity for native cache hits, not a mutable request
        # array that a later query could replace in place.
        request._regional_task.destinationPointSets = list(self.point_sets)


def prepare(network, destinations, batch_size, departure):
    """Use the pinned library's own point-set builder and WALK egress tables."""
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("Positive native destination capacity required")
    import com.conveyal.r5 as r5
    from r5py import TransportMode
    from r5py.r5.regional_task import RegionalTask

    native = network._transport_network
    batches = []
    for offset in range(0, len(destinations), batch_size):
        frame = destinations.iloc[offset : offset + batch_size].reset_index(drop=True)
        task = RegionalTask(
            network,
            destinations=frame,
            departure=departure,
            transport_modes=[TransportMode.WALK],
            speed_walking=4.8,
            max_time=timedelta(minutes=45),
        )
        point_sets = task._regional_task.destinationPointSets
        for point_set in point_sets:
            linkage = native.linkageCache.getLinkage(
                point_set, native.streetLayer, r5.profile.StreetMode.WALK
            )
            # This native table has no request date/time/speed argument. Build
            # exactly the same stable table TravelTimeComputer would build lazily.
            linkage.getEgressCostTable()
        batches.append(PreparedDestinations(destination_key(frame), tuple(point_sets)))
    return batches
