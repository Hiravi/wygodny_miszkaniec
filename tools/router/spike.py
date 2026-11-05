"""Real-input R5 spike. A successful matrix is not a completed isochrone product."""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
R5_URL = "https://github.com/r5py/r5/releases/download/v7.5.1-r5py/r5-v7.5.1-r5py-all.jar"
R5_SHA256 = "d50be106cadd7b636cfc0e209052767d7df570629f79fdf98ecd5cf5d2d89be7"
ADDRESS_IDS = (2121713901465, 2121713928847, 2121713897635)


def main() -> None:
    cache = ROOT / ".cache/router"
    java_home = cache / "jdk-25.0.4.1+1"
    if not (java_home / "bin/java.exe").exists():
        raise RuntimeError("Install the pinned portable JDK documented in router-runtime.json")
    jar = cache / "r5-v7.5.1-r5py-all.jar"
    if not jar.exists():
        with (
            urlopen(
                Request(R5_URL, headers={"User-Agent": "WygodnyMieszkaniecPilot/0.1"}), timeout=45
            ) as response,
            jar.open("wb") as output,
        ):
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
    with jar.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != R5_SHA256:
            raise ValueError("R5 checksum mismatch")
    os.environ["JAVA_HOME"] = str(java_home)
    sys.argv.extend(["--max-memory", "6G", "--r5-classpath", str(jar)])

    import geopandas as gpd
    import pandas as pd
    import psutil
    import r5py
    from pyproj import Transformer
    from shapely.geometry import Point

    with ZipFile(ROOT / "data/raw/msip_addresses_1492.zip") as archive:
        addresses = json.loads(archive.read("Adresy.geojson"))
    selected = {
        feature["id"]: feature for feature in addresses["features"] if feature["id"] in ADDRESS_IDS
    }
    transformer = Transformer.from_crs("EPSG:2178", "EPSG:4326", always_xy=True)
    points = [
        Point(*transformer.transform(*selected[key]["geometry"]["coordinates"]))
        for key in ADDRESS_IDS
    ]
    origins = gpd.GeoDataFrame({"id": [str(ADDRESS_IDS[0])]}, geometry=[points[0]], crs=4326)
    destinations = gpd.GeoDataFrame(
        {"id": [str(key) for key in ADDRESS_IDS[1:]]}, geometry=points[1:], crs=4326
    )
    departure = datetime(2026, 10, 14, 8, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
    manifest = {
        "schema_version": "1",
        "engine": "R5",
        "r5py_version": r5py.__version__,
        "r5_version": "7.5.1-r5py",
        "r5_sha256": R5_SHA256,
        "departure": departure.isoformat(),
        "departure_window_seconds": 60,
        "jvm_heap_limit": "6G",
        "address_ids": list(ADDRESS_IDS),
        "coordinates": [[p.x, p.y] for p in points],
        "graph_verified": False,
        "matrix_verified": False,
        "isochrone_verified": False,
        "routing_ready": False,
    }
    manifest["input_sha256"] = {}
    for name in ("msip_addresses_1492", "ztp_gtfs_a", "ztp_gtfs_m", "ztp_gtfs_t"):
        source = json.loads((ROOT / f"data/manifests/{name}.json").read_text(encoding="utf-8"))
        with (ROOT / f"data/raw/{name}.zip").open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != source["sha256"]:
            raise ValueError(f"Source checksum mismatch: {name}")
        manifest["input_sha256"][name] = digest
    output = ROOT / "data/manifests/routing-spike-v1.json"
    started = time.perf_counter()
    try:
        from r5py.util import Config, FileDigest

        paths = [ROOT / "data/raw/malopolskie-261011.osm.pbf"] + [
            ROOT / f"data/raw/ztp_gtfs_{feed}.zip" for feed in ("a", "m", "t")
        ]
        digest = hashlib.sha256(
            ("".join(FileDigest(path) for path in paths) + "False").encode()
        ).hexdigest()
        cache_path = Config().CACHE_DIR / f"{digest}.transport_network"
        manifest["graph_cache_present_before_query"] = cache_path.exists()
        print("Building or loading real OSM + GTFS transport network", flush=True)
        network = r5py.TransportNetwork(
            ROOT / "data/raw/malopolskie-261011.osm.pbf",
            [ROOT / f"data/raw/ztp_gtfs_{feed}.zip" for feed in ("a", "m", "t")],
        )
        manifest["graph_seconds"] = round(time.perf_counter() - started, 3)
        manifest["graph_verified"] = True
        print(json.dumps({"graph_seconds": manifest["graph_seconds"]}), flush=True)
        for mode in (r5py.TransportMode.TRANSIT, r5py.TransportMode.WALK):
            query_started = time.perf_counter()
            result = r5py.TravelTimeMatrix(
                network,
                origins=origins,
                destinations=destinations,
                departure=departure,
                departure_time_window=timedelta(minutes=1),
                transport_modes=[mode],
                max_time=timedelta(minutes=60),
            )
            manifest[mode.name.lower()] = {
                "seconds": round(time.perf_counter() - query_started, 3),
                "rows": json.loads(pd.DataFrame(result).to_json(orient="records")),
            }
            print(json.dumps({mode.name: manifest[mode.name.lower()]}), flush=True)
        manifest["matrix_verified"] = True
        detailed_started = time.perf_counter()
        detail_frames = []
        for index in range(len(destinations)):
            detail_frames.append(
                r5py.DetailedItineraries(
                    network,
                    origins=origins,
                    destinations=destinations.iloc[[index]],
                    departure=departure,
                    departure_time_window=timedelta(minutes=1),
                    transport_modes=[r5py.TransportMode.TRANSIT],
                    max_time=timedelta(minutes=60),
                )
            )
        details = pd.concat(detail_frames, ignore_index=True)
        manifest["detailed_query_seconds"] = round(time.perf_counter() - detailed_started, 3)
        manifest["detailed_legs"] = json.loads(
            pd.DataFrame(details.drop(columns="geometry")).to_json(
                orient="records", date_format="iso", default_handler=str
            )
        )
        print(json.dumps({"detailed_leg_count": len(details)}), flush=True)
        manifest["note"] = (
            "Point-to-point probe only; barriers, snapping, waiting and isochrones need QA."
        )
    except Exception as error:
        manifest["error"] = str(error)
        raise
    finally:
        memory = psutil.Process().memory_info()
        manifest["process_rss_bytes"] = memory.rss
        manifest["process_peak_bytes"] = getattr(memory, "peak_wset", None)
        output.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
