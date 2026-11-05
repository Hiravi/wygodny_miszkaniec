"""Conservatively apply explicit source barrier restrictions to native WALK edges."""

import hashlib
import json

POLICY = "explicit-barrier-walk-permissions-v33"


def restricted(tags):
    if tags.get("locked") == "yes":
        return True
    if tags.get("foot") in {"yes", "designated", "permissive"}:
        return any(k.startswith("foot:") for k in tags)
    return (
        tags.get("foot") in {"no", "private"}
        or tags.get("access") in {"no", "private"}
        or tags.get("barrier") in {"wall", "fence", "hedge"}
        or any(k.startswith(("foot:", "access:")) for k in tags)
    )


def apply_barriers(network, root, osm_hash):
    import com.conveyal.r5 as r5

    signature = hashlib.sha256(f"{POLICY}:{osm_hash}".encode()).hexdigest()
    target = root / f".cache/router/barrier-permissions-{signature}.json"
    if not target.exists():
        raise ValueError("Prepare barrier data with python -m tools.router.barrier_permissions")
    nodes = json.loads(target.read_text(encoding="utf-8"))
    layer = network.streetLayer
    field = layer.getClass().getDeclaredField("vertexIndexForOsmNode")
    field.setAccessible(True)
    indices = field.get(layer)
    coordinate_vertices = {}
    if indices is None:
        # Serialized R5 graphs discard the OSM node index. Match only the
        # native fixed-coordinate quantum (1e-7 degree), never a radius snap.
        import numpy as np

        store = layer.vertexStore
        keys = (np.asarray(store.fixedLats.toArray(), dtype=np.int64) << 32) | np.asarray(
            store.fixedLons.toArray(), dtype=np.uint32
        )
        order = np.argsort(keys)
        ordered = keys[order]
        for node in nodes:
            lat = int(r5.streets.VertexStore.floatingDegreesToFixed(node["latitude"]))
            lon = int(r5.streets.VertexStore.floatingDegreesToFixed(node["longitude"]))
            found = set()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    key = ((lat + dy) << 32) | ((lon + dx) & 0xFFFFFFFF)
                    begin, end = (
                        np.searchsorted(ordered, key, side="left"),
                        np.searchsorted(ordered, key, side="right"),
                    )
                    found.update(int(i) for i in order[begin:end])
            if found:
                coordinate_vertices[node["id"]] = found
    edges, matched = set(), []
    for node in nodes:
        vertices = (
            coordinate_vertices.get(node["id"], ())
            if indices is None
            else [int(indices.get(node["id"]))]
            if indices.containsKey(node["id"])
            else ()
        )
        if not vertices:
            continue
        matched.append(node["id"])
        for vertex in vertices:
            for collection in (layer.outgoingEdges, layer.incomingEdges):
                for value in collection.get(vertex).toArray():
                    edge = layer.edgeStore.getCursor(int(value))
                    if edge.allowsStreetMode(r5.profile.StreetMode.WALK):
                        edges.add(int(value))
                        edge.clearFlag(r5.streets.EdgeStore.EdgeFlag.ALLOWS_PEDESTRIAN)
                        edge.clearFlag(r5.streets.EdgeStore.EdgeFlag.ALLOWS_WHEELCHAIR)
    # Cached distance propagation must obey the corrected permissions too.
    network.transitLayer.buildDistanceTables(None)
    proof = {
        "policy": POLICY,
        "signature": signature,
        "restricted_source_nodes": len(nodes),
        "native_vertex_nodes": len(matched),
        "changed_directed_edges": len(edges),
        "matched_node_ids": matched,
        "match_method": "osm-node-id" if indices is not None else "native-coordinate-quantum",
        "artifact": target.relative_to(root).as_posix(),
    }
    applied = root / f".cache/router/barrier-applied-{signature}.json"
    applied.write_text(json.dumps(proof, indent=2), encoding="utf-8")
    return {k: v for k, v in proof.items() if k != "matched_node_ids"} | {
        "applied_artifact": applied.relative_to(root).as_posix()
    }


def main():
    import osmium

    from tools.data.acquire import ROOT, sha256

    signature = hashlib.sha256(
        f"{POLICY}:{sha256(ROOT / 'data/raw/malopolskie-261011.osm.pbf')}".encode()
    ).hexdigest()
    target = ROOT / f".cache/router/barrier-permissions-{signature}.json"
    if target.exists():
        print("Barrier source cache ready")
        return
    nodes = []
    for entity in osmium.FileProcessor(ROOT / "data/raw/malopolskie-261011.osm.pbf").with_filter(
        osmium.filter.KeyFilter("barrier")
    ):
        if entity.is_node() and restricted(dict(entity.tags)):
            nodes.append(
                {
                    "id": entity.id,
                    "longitude": entity.location.lon,
                    "latitude": entity.location.lat,
                    "tags": dict(entity.tags),
                }
            )
    target.write_text(json.dumps(nodes), encoding="utf-8")
    print(
        json.dumps({"barrier_source_nodes": len(nodes), "artifact": str(target.relative_to(ROOT))})
    )


if __name__ == "__main__":
    main()
