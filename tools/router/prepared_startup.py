"""Versioned, query-free native startup state prepared from the pinned inputs."""

import gzip
import hashlib
import json
import sqlite3
from pathlib import Path

from tools.data.acquire import sha256

NAME = "native-startup-v1"


def read(root, policies):
    path = root / f".cache/router/{NAME}/state.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        state = json.load(stream)
    if state["policies"] != policies or state["format"] != NAME:
        raise ValueError("Prepared native policies changed; prepare again")
    if not isinstance(state.get("trip_lineage"), dict):
        raise ValueError("Prepared native trip lineage missing; prepare again")
    proof = json.loads((root / "data/manifests/otp-point-spike-v1.json").read_text("utf-8"))
    expected = {"malopolskie-261011.osm.pbf": proof["input_sha256"]["osm"]}
    expected.update(
        {f"ztp_gtfs_{feed}.zip": proof["input_sha256"][f"ztp_gtfs_{feed}"] for feed in "amt"}
    )
    if state["metadata"]["sources"] != expected:
        raise ValueError("Prepared native inputs differ from OTP")
    database = root / "data/derived/generated/locations.sqlite"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        version = db.execute(
            "SELECT value FROM metadata WHERE key IN ('data_version','version') "
            "ORDER BY CASE key WHEN 'data_version' THEN 0 ELSE 1 END LIMIT 1"
        ).fetchone()
    if version is None or version[0] != state["metadata"]["data_version"]:
        raise ValueError("Prepared native location version changed; prepare again")
    graph = root / state["network"]
    if not graph.resolve().is_relative_to(root) or sha256(graph) != state["network_sha256"]:
        raise ValueError("Prepared native graph differs")
    return state


def network(state, root, r5py):
    """Use the pinned SDK deserializer, with no WorkingCopy/source/build fallback."""
    from r5py.r5.transport_network import GoodEnoughEquidistantCrs

    path = root / state["network"]
    if not path.with_suffix(".warnings").is_file():
        raise ValueError("Prepared native warnings missing")
    value = r5py.TransportNetwork.__new__(r5py.TransportNetwork)
    value._transport_network = value._load_pickled_transport_network(path)
    value.EQUIDISTANT_CRS = GoodEnoughEquidistantCrs(value.extent)
    return value


def write(root, value, records, metadata, route_engine, service_dates, policies, input_cache):
    import os
    import shutil

    import com.conveyal.r5 as r5
    import java.io

    output_root = Path(os.environ.get("WM_PREPARE_TARGET", str(root)))
    folder = output_root / f".cache/router/{NAME}"
    folder.mkdir(parents=True, exist_ok=True)
    graph = folder / "network.transport_network"
    r5.kryo.KryoNetworkSerializer.write(value._transport_network, java.io.File(str(graph)))
    warnings = next(input_cache.glob("*.warnings"))
    shutil.copy2(warnings, graph.with_suffix(".warnings"))
    state = {
        "format": NAME,
        "policies": policies,
        "network": graph.relative_to(output_root).as_posix(),
        "network_sha256": sha256(graph),
        "metadata": metadata,
        "records": records,
        "service_dates": [day.isoformat() for day in service_dates],
        "trip_lineage": route_engine.trip_lineage,
        "witness": {
            "trips": route_engine.trips,
            "shapes": route_engine.shapes,
            "stops": route_engine.stops,
        },
    }
    with gzip.open(folder / "state.json.gz", "wt", encoding="utf-8", compresslevel=6) as stream:
        json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
    print(
        json.dumps(
            {
                "prepared_startup": True,
                "network_sha256": state["network_sha256"],
                "records_sha256": hashlib.sha256(
                    json.dumps(records, sort_keys=True).encode()
                ).hexdigest(),
                "records": len(records),
            }
        ),
        flush=True,
    )


def exists(root):
    return (Path(root) / f".cache/router/{NAME}/state.json.gz").is_file()
