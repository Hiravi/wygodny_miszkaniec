"""Loopback, serial pinned-R5 worker. No custom transit planner or hull polygonization."""

import hashlib
import json
import math
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore
from zoneinfo import ZoneInfo

from tools.release.response_cache import ResponseCache
from tools.router.barrier_permissions import POLICY as BARRIER_POLICY
from tools.router.barrier_permissions import apply_barriers
from tools.router.coverage_policy import minute_band
from tools.router.final_walk import POLICY as FINAL_WALK_POLICY
from tools.router.final_walk import confirm, matrix_class
from tools.router.reachable_routes import ReachableRoutes
from tools.router.service_days import METHOD, ROUTE_METHOD, normalize_task, prepare, query_clock
from tools.router.service_days import POLICY as SERVICE_DAY_POLICY
from tools.router.service_overlap import overlap_windows
from tools.router.transfer_costs import POLICY, calibrate
from tools.router.walk_access import POLICY as WALK_ACCESS_POLICY
from tools.router.walk_access import WalkAccess

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = {
    "departure_window_seconds": 60,
    "speed_walking_kmh": 4.8,
    "maximum_minutes": 45,
    "maximum_rides": 8,
    "sample_snap_radius_m": 30,
    "boarding_minimum_seconds": 60,
    "time_resolution": "floor minutes; never precise reconstructed seconds",
    "timezone": "Europe/Warsaw",
    "egress": "WALK network",
    "transfer_cost_policy": POLICY,
    "service_day_policy": SERVICE_DAY_POLICY,
    "barrier_policy": BARRIER_POLICY,
    "walk_access_validation": WALK_ACCESS_POLICY,
    "final_walk_policy": FINAL_WALK_POLICY,
}


def initialize():
    phase_started = time.perf_counter()
    phase_previous = phase_started

    def stage(name):
        nonlocal phase_previous
        now = time.perf_counter()
        print(
            json.dumps(
                {
                    "event": "native_startup",
                    "stage": name,
                    "seconds": now - phase_previous,
                    "elapsed": now - phase_started,
                    "process_cpu_seconds": time.process_time(),
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        phase_previous = now

    from tools.router.path_capacity import runtime_jar

    os.environ["JAVA_HOME"] = str(ROOT / ".cache/router/jdk-25.0.4.1+1")
    os.environ["JAVA_TOOL_OPTIONS"] = "-XX:ActiveProcessorCount=2 -XX:HeapBaseMinAddress=32g"
    sys.argv.extend(
        [
            "--max-memory",
            "4G",
            "--r5-classpath",
            str(runtime_jar(ROOT)),
        ]
    )
    stage("runtime_verification")
    import geopandas as gpd
    import pandas as pd
    import psutil
    import r5py
    from pyproj import Transformer
    from shapely import from_wkb
    from shapely.geometry import Point, box, mapping, shape
    from shapely.ops import transform, unary_union

    from tools.data.acquire import sha256
    from tools.router.spike import R5_SHA256

    stage("python_imports_jvm")
    started = time.perf_counter()
    jar = ROOT / ".cache/router/r5-v7.5.1-r5py-all.jar"
    if sha256(jar) != R5_SHA256 or r5py.__version__ != "1.1.7":
        raise ValueError("Pinned R5/r5py mismatch")
    from tools.router import prepared_startup

    policies = [
        METHOD,
        ROUTE_METHOD,
        BARRIER_POLICY,
        POLICY,
        WALK_ACCESS_POLICY,
        FINAL_WALK_POLICY,
        SERVICE_DAY_POLICY,
    ]
    prepared = None
    if (
        (ROOT / "MINIMAL_RUNTIME.json").exists()
        and not prepared_startup.exists(ROOT)
        and not os.environ.get("WM_PREPARE_STARTUP")
    ):
        raise ValueError("Prepared native startup state missing; prepare the package again")
    if (
        prepared_startup.exists(ROOT)
        and not os.environ.get("WM_PREPARE_STARTUP")
        and not os.environ.get("WM_STARTUP_LEGACY")
    ):
        prepared = prepared_startup.read(ROOT, policies)
    if prepared:
        old = prepared["metadata"]
        if old["settings"] != SETTINGS or old["runtime_jar_sha256"] != sha256(runtime_jar(ROOT)):
            raise ValueError("Prepared native settings/runtime changed; prepare again")
        hashes, normalized_hashes = old["sources"], old["normalized_gtfs"]
        service_dates = [date.fromisoformat(day) for day in prepared["service_dates"]]
        trip_lineage = prepared["trip_lineage"]
        input_cache = ROOT / ".cache/router/native-startup-v1"
        stage("prepared_state_read")
        network = prepared_startup.network(prepared, ROOT, r5py)
        reloaded_after_build = False
        barrier_proof, transfer_proof = old["barrier_permissions"], old["transfer_calibration"]
        stage("prepared_graph_deserialization")
    else:
        proof = json.loads((ROOT / "data/manifests/otp-point-spike-v1.json").read_text())
        inputs = [ROOT / "data/raw/malopolskie-261011.osm.pbf"] + [
            ROOT / f"data/raw/ztp_gtfs_{f}.zip" for f in "amt"
        ]
        hashes = {p.name: sha256(p) for p in inputs}
        for p in inputs:
            key = "osm" if p.suffix == ".pbf" else p.stem
            if hashes[p.name] != proof["input_sha256"][key]:
                raise ValueError("R5 and OTP must use identical sources")
        stage("source_hashes")
        normalized, trip_lineage, service_dates = prepare(ROOT)
        normalized_hashes = {path.name: sha256(path) for path in normalized}
        inputs = [inputs[0], *normalized]
        from tools.router.input_cache import isolate_cache, load_network

        input_cache = isolate_cache(ROOT, inputs, R5_SHA256)
        stage("normalized_inputs_and_fingerprints")
        network, reloaded_after_build = load_network(r5py, inputs, input_cache)
        stage("graph_deserialization")
        barrier_proof = apply_barriers(network._transport_network, ROOT, hashes[inputs[0].name])
        transfer_proof = calibrate(
            network._transport_network,
            ROOT,
            hashes
            | {"normalized_gtfs": normalized_hashes, "barrier_policy": barrier_proof["signature"]},
            R5_SHA256,
        )
        stage("barriers_transfers")
    route_engine = ReachableRoutes(
        network,
        ROOT,
        WalkAccess.load(ROOT, hashes["malopolskie-261011.osm.pbf"]),
        trip_lineage,
        witness=prepared["witness"] if prepared else None,
    )
    if str(network.timezone) != SETTINGS["timezone"]:
        raise ValueError("Unexpected network timezone")
    native = network._transport_network.transitLayer
    if native.hasFrequencies:
        raise ValueError("Exact scheduled-minute policy rejects stochastic frequency feeds")
    to_metric = Transformer.from_crs(4326, 2180, always_xy=True).transform
    if prepared:
        city = shape(prepared["metadata"]["city"])
        data_version = prepared["metadata"]["data_version"]
        records = prepared["records"]
        grid_count = prepared["metadata"]["grid_samples"]
        rejected = prepared["metadata"]["rejected_grid_samples"]
        points = [Point(row["longitude"], row["latitude"]) for row in records]
        stage("prepared_grid_and_witness")
    else:
        to_wgs = Transformer.from_crs(2180, 4326, always_xy=True).transform
        with sqlite3.connect(ROOT / "data/derived/generated/locations.sqlite") as db:
            city = unary_union(
                [from_wkb(row[0]) for row in db.execute("SELECT geometry FROM districts")]
            )
            data_version = db.execute(
                "SELECT value FROM metadata WHERE key='data_version'"
            ).fetchone()
            if data_version is None:
                data_version = db.execute(
                    "SELECT value FROM metadata WHERE key='version'"
                ).fetchone()
            data_version = data_version[0]
        stage("route_engine_city")
        metric_city = transform(to_metric, city)
        x0, y0, x1, y1 = metric_city.bounds
        # Display mask only; these simplified polygons never affect routing or endpoint eligibility.
        with sqlite3.connect(ROOT / "data/derived/generated/map-context.sqlite") as db:
            water = unary_union(
                [
                    from_wkb(r[0])
                    for r in db.execute("SELECT geometry FROM features WHERE kind='water'")
                ]
            )
        stage("water_mask")
        points, records = [], []
        rejected = 0
        for x in range(math.floor(x0 / 150) * 150, math.ceil(x1), 150):
            for y in range(math.floor(y0 / 150) * 150, math.ceil(y1), 150):
                point = Point(*to_wgs(x + 75, y + 75))
                if not city.covers(point):
                    continue
                snapped = network.street_layer.find_split(point, radius=30)
                if snapped.is_empty:
                    rejected += 1
                    continue
                # The calculated destination is the actual WALK-network sample, explicitly exposed.
                points.append(snapped)
                cell = (
                    transform(to_wgs, box(x, y, x + 150, y + 150))
                    .intersection(city)
                    .difference(water)
                )
                records.append(
                    {
                        "id": f"g:{x}:{y}",
                        "kind": "sample",
                        "name": "Próbka sieci pieszej",
                        "longitude": snapped.x,
                        "latitude": snapped.y,
                        "geometry": mapping(cell),
                    }
                )
        stage("grid_geometry_snapping")
        grid_count = len(points)
        import csv
        import io
        from zipfile import ZipFile

        for namespace, f in enumerate("amt", 1):
            with ZipFile(ROOT / f"data/raw/ztp_gtfs_{f}.zip") as archive:
                rows = csv.DictReader(
                    io.TextIOWrapper(archive.open("stops.txt"), encoding="utf-8-sig")
                )
                for row in rows:
                    if row.get("location_type", "0") not in ("", "0"):
                        continue
                    point = Point(float(row["stop_lon"]), float(row["stop_lat"]))
                    if not city.covers(point):
                        continue
                    points.append(point)
                    records.append(
                        {
                            "id": f"{namespace}:{row['stop_id']}",
                            "kind": "stop",
                            "name": row["stop_name"],
                            "longitude": point.x,
                            "latitude": point.y,
                            "geometry": mapping(point),
                        }
                    )
        stage("platform_records")
    destinations = gpd.GeoDataFrame({"id": [r["id"] for r in records]}, geometry=points, crs=4326)
    import com.conveyal.r5 as r5

    destination_splits = [
        network._transport_network.streetLayer.findSplit(
            r["latitude"], r["longitude"], 30.0, r5.profile.StreetMode.WALK
        )
        for r in records
    ]
    stage("destination_splits")
    from tools.router.destination_linkage import POLICY as DESTINATION_LINKAGE_POLICY
    from tools.router.destination_linkage import prepare as prepare_destinations

    batch_size = int(r5.analyst.cluster.PathResult.MAX_PATH_DESTINATIONS)
    linkage_started = time.perf_counter()
    destination_batches = prepare_destinations(
        network,
        destinations,
        batch_size,
        datetime.combine(min(service_dates), datetime.min.time()) + timedelta(hours=12),
    )
    linkage_seconds = time.perf_counter() - linkage_started
    stage("walk_linkages_egress_tables")
    signature = hashlib.sha256(
        json.dumps(
            {
                "method": METHOD,
                "inputs": hashes,
                "normalized_gtfs": normalized_hashes,
                "jar": R5_SHA256,
                "r5py": r5py.__version__,
                "settings": SETTINGS,
                "route_method": ROUTE_METHOD,
                "records": records,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    metadata = {
        "method": METHOD,
        "route_method": ROUTE_METHOD,
        "schema_version": "4",
        "graph_version": signature,
        "data_version": data_version,
        "sources": hashes,
        "normalized_gtfs": normalized_hashes,
        "input_cache": input_cache.relative_to(ROOT).as_posix(),
        "runtime_jar_sha256": sha256(runtime_jar(ROOT)),
        "destination_linkage_policy": DESTINATION_LINKAGE_POLICY,
        "prepared_destination_batches": len(destination_batches),
        "destination_linkage_seconds": round(linkage_seconds, 3),
        "matrix_path_capacity": int(r5.analyst.cluster.PathResult.MAX_PATH_DESTINATIONS),
        "reloaded_after_build": reloaded_after_build,
        "transfer_calibration": transfer_proof,
        "barrier_permissions": barrier_proof,
        "settings": SETTINGS,
        "grid_m": 150,
        "grid_samples": grid_count,
        "rejected_grid_samples": rejected,
        "platforms": len(records) - grid_count,
        "regional_platforms": len(native.stopIdForIndex),
        "routes": len(native.routes),
        "city": mapping(city),
        "load_seconds": round(time.perf_counter() - started, 3),
        "rss_bytes": psutil.Process().memory_info().rss,
    }
    stage("metadata_signature")
    dates = {day.isoformat() for day in service_dates}
    overlap = overlap_windows(ROOT, hashes, sorted(dates))
    cache = ResponseCache(max_bytes=24 * 1024 * 1024, max_entries=4)
    from tools.router.instant_point_runtime import POLICY as POINT_POLICY
    from tools.router.instant_point_runtime import verified_runtime
    from tools.router.native_point import NativePoint

    point_jar = verified_runtime(ROOT)
    point_engine = (
        NativePoint(
            network, route_engine, route_engine.walk_access, signature, sampler_verified=True
        )
        if point_jar is not None and runtime_jar(ROOT) == point_jar
        else None
    )
    stage("overlap_point_engine")
    metadata["point_method"] = POINT_POLICY if point_engine is not None else None
    if os.environ.get("WM_PREPARE_STARTUP") == "1":
        prepared_startup.write(
            ROOT, network, records, metadata, route_engine, service_dates, policies, input_cache
        )

    def project_samples(result, include_samples):
        """Keep the full cached calculation; omit hidden grid points only on the wire.

        Route sections, stops, counts and every native proof stay in the default
        result. The existing "Próbki terenu" request explicitly asks for the
        already calculated 150m points, so this is not a smaller territorial
        calculation or a negative-access shortcut.
        """
        if include_samples:
            return result
        return result | {
            "features": result["features"]
            | {
                "features": [
                    feature
                    for feature in result["features"]["features"]
                    if feature["properties"]["kind"] != "sample"
                ]
            }
        }

    def calculate(body):
        include_samples = body.get("include_samples", True)
        if not isinstance(include_samples, bool):
            raise ValueError("include_samples must be boolean")
        # Wire-only projection selection is not a routing input or part of the
        # public request echoed in results/cache keys.
        body = {key: body[key] for key in ("origin", "departure", "data_version")}
        origin, departure = body["origin"], datetime.fromisoformat(body["departure"])
        local = departure.astimezone(ZoneInfo("Europe/Warsaw"))
        result = {
            "schema_version": "4",
            "method": METHOD,
            "data_version": data_version,
            "graph_version": signature,
            "request": body,
            "cache_hit": False,
            "routes": None,
            "routes_error": None,
            "features": {"type": "FeatureCollection", "features": []},
            "coverage": {
                k: metadata[k]
                for k in (
                    "grid_m",
                    "grid_samples",
                    "rejected_grid_samples",
                    "platforms",
                    "regional_platforms",
                    "routes",
                )
            },
        }

        def finish(status, explanation):
            return result | {"status": status, "explanation": explanation}

        result["coverage"]["verified_transfer_pairs"] = (
            transfer_proof["candidate_pairs"] - transfer_proof["unconfirmed_pairs_removed"]
        )
        result["coverage"]["omitted_transfer_pairs"] = transfer_proof["unconfirmed_pairs_removed"]

        if local.date().isoformat() not in dates:
            return finish(
                "unsupported_date", "Brak wspólnego pokrycia rozkładów A/M/T dla tej daty."
            )
        previous_day = (local.date() - timedelta(days=1)).isoformat()
        clock = local.hour * 3600 + local.minute * 60 + local.second
        if previous_day not in dates and clock <= max(overlap.values(), default=0):
            return finish(
                "unsupported_time",
                "Brak poprzedniej doby w źródle; nocny zasięg jest niepotwierdzony. Użyj Trasa.",
            )
        if (local + timedelta(minutes=45)).date().isoformat() not in dates:
            return finish("unsupported_time", "Brak kolejnej doby w źródle. Użyj Trasa.")
        if departure.replace(tzinfo=None) != local.replace(tzinfo=None):
            raise ValueError("Use Warsaw time")
        p = Point(origin["longitude"], origin["latitude"])
        if not city.covers(p):
            return finish("outside", "Punkt poza Krakowem; sieć regionalna nie jest przycięta.")
        effective = network.street_layer.find_split(p, radius=30)
        if effective.is_empty:
            from tools.router.network_origin import nearby_network_origin

            result["suggested_origin"] = nearby_network_origin(network.street_layer, p, city)
            return finish(
                "insufficient_coverage",
                "Brak powiązania z siecią pieszą do 30 m. Wybierz punkt na drodze publicznej.",
            )
        link_m = Point(*to_metric(p.x, p.y)).distance(Point(*to_metric(effective.x, effective.y)))
        result.update(
            {
                "effective_origin": {"longitude": effective.x, "latitude": effective.y},
                "origin_link_m": link_m,
                "origin_access_confirmed": link_m <= 0.02,
            }
        )
        key = json.dumps(body, sort_keys=True) + signature
        cached = cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            cache.move_to_end(key)
            return project_samples(cached[1] | {"cache_hit": True}, include_samples)
        begin = time.perf_counter()
        stages = {}
        origins = gpd.GeoDataFrame({"id": ["origin"]}, geometry=[p], crs=4326)
        candidates = []
        # Respect the native detailed-path return capacity. The capacity-only
        # derived JAR handles this full target set in one search; the original
        # runtime retains its supported batches. Neither truncates destinations.
        chunks = []
        for offset in range(0, len(destinations), batch_size):
            matrix = matrix_class(
                r5py,
                native,
                candidates,
                query_clock(departure),
                prepared_destinations=destination_batches[offset // batch_size],
            )(
                network,
                origins=origins,
                destinations=destinations.iloc[offset : offset + batch_size].reset_index(drop=True),
                departure=local.replace(tzinfo=None),
                departure_time_window=timedelta(minutes=1),
                transport_modes=[
                    r5py.TransportMode.BUS,
                    r5py.TransportMode.TRAM,
                    r5py.TransportMode.WALK,
                ],
                speed_walking=4.8,
                max_time=timedelta(minutes=45),
                max_public_transport_rides=8,
            )
            chunks.append(pd.DataFrame(matrix))
        stages["matrix_seconds"] = time.perf_counter() - begin
        features = []
        eligible_final_walk = set()
        for index, (record, minutes) in enumerate(
            zip(records, pd.concat(chunks)["travel_time"], strict=True)
        ):
            measured = not pd.isna(minutes) and 0 <= minutes < 45
            if measured:
                # R5's matrix already proves this destination can be reached
                # in the fixed 45-minute horizon.  A final direct/egress
                # walk cannot create a valid proof for a matrix-unreachable
                # sample; avoid re-reading those StreetRouter states.
                eligible_final_walk.add(index)
            if not measured and record["kind"] != "stop":
                continue
            if record["kind"] == "sample" and not record["geometry"].get("coordinates"):
                continue  # Display mask may remove a cell; never emit invalid empty GeoJSON.
            # A minute interval is only a model estimate, never a walking proof.
            band = minute_band(minutes) if measured else None
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [record["longitude"], record["latitude"]],
                    },
                    "properties": {
                        k: record[k] for k in ("id", "kind", "name", "longitude", "latitude")
                    }
                    | {
                        "minutes_floor": int(minutes) if measured else None,
                        "band": None,
                        "estimated_band": band,
                        "confirmation": "approximate" if band else "unknown",
                    },
                }
            )
        result["features"]["features"] = features
        import copy

        witness_request = copy.copy(matrix.request)
        witness_request.origin = p
        normalize_task(witness_request._regional_task, departure)
        route_started = time.perf_counter()
        try:
            result["routes"] = route_engine.compute(witness_request._regional_task)
        except (ValueError, TypeError, RuntimeError) as error:
            print(
                f"Native route-section extraction failed: {type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            result["routes_error"] = (
                "Potwierdzone odcinki tras są niedostępne. "
                "Próbki czasu i trasy OTP działają niezależnie."
            )
        stages["routes_seconds"] = time.perf_counter() - route_started
        walk_started = time.perf_counter()
        proofs, final_walk_counts = confirm(
            network,
            witness_request._regional_task,
            records,
            candidates,
            result["routes"],
            route_engine.walk_access,
            destination_splits,
            eligible_final_walk,
        )
        stages["final_walk_seconds"] = time.perf_counter() - walk_started
        by_id = {f["properties"]["id"]: f for f in features}
        for index, proof in proofs.items():
            record = records[index]
            feature = by_id.get(record["id"])
            if feature is None:
                feature = {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [record["longitude"], record["latitude"]],
                    },
                    "properties": {
                        k: record[k] for k in ("id", "kind", "name", "longitude", "latitude")
                    }
                    | {"minutes_floor": None, "estimated_band": None},
                }
                features.append(feature)
            feature["properties"].update(proof)
        result["coverage"].update(final_walk_counts)
        result["calculation_seconds"] = round(time.perf_counter() - begin, 3)
        print(
            json.dumps(
                {
                    "event": "coverage_stages",
                    "departure": body["departure"],
                    **stages,
                    "total_seconds": result["calculation_seconds"],
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        result["rss_bytes"] = psutil.Process().memory_info().rss
        result = finish(
            "ok",
            "Według rozkładu A/M/T. Siatka 150 m pokazuje próbki sieci pieszej, "
            "nie gwarancję dla całej komórki. Czas R5 jest szacunkiem kosztu modelu "
            "z ostatnim dojściem, zaokrąglonym w dół do minut. "
            "Brak koloru nie dowodzi niedostępności; "
            "kliknij cel, aby sprawdzić trasę.",
        )
        if result["routes"] is not None:
            cache[key] = (time.monotonic(), result)
        while len(cache) > 4:
            cache.popitem(last=False)
        return project_samples(result, include_samples)

    def controls(body):
        if body.get("point_query") is True:
            if point_engine is None:
                raise ValueError("Verified exact point runtime is not prepared")
            query = {
                key: body[key] for key in ("origin", "destination", "departure", "data_version")
            }
            if query["data_version"] != data_version:
                raise ValueError("Point source version mismatch")
            local = datetime.fromisoformat(query["departure"]).astimezone(ZoneInfo("Europe/Warsaw"))
            day = local.date().isoformat()
            previous = (local.date() - timedelta(days=1)).isoformat()
            clock = local.hour * 3600 + local.minute * 60
            if day not in dates:
                value = {
                    "status": "unsupported_date",
                    "method": POINT_POLICY,
                    "graph_version": signature,
                    "itineraries": [],
                }
            elif (previous not in dates and clock <= max(overlap.values(), default=0)) or (
                local + timedelta(minutes=45)
            ).date().isoformat() not in dates:
                value = {
                    "status": "unsupported_time",
                    "method": POINT_POLICY,
                    "graph_version": signature,
                    "itineraries": [],
                }
            else:
                value = point_engine.calculate(
                    query["origin"], query["destination"], query["departure"]
                )
            return value | {"request": query, "data_version": data_version}
        if body.get("clear_result_cache") is True:
            cache.clear()
            return {"cleared": True}
        if len(body["destinations"]) > 32:
            raise ValueError("Bounded control probe only")
        maximum = body.get("maximum_minutes", 45)
        if not isinstance(maximum, int) or not 1 <= maximum <= 90:
            raise ValueError("Control horizon must be 1..90 minutes")
        if body.get("inventory_diagnostics"):
            loaded_trips = {str(s.tripId) for p in native.tripPatterns for s in p.tripSchedules}
            raw_trips = set(route_engine.trips)
            raw_stops = set(route_engine.stops)
            linked = [
                str(native.stopIdForIndex.get(i))
                for i in range(len(native.stopIdForIndex))
                if int(native.streetVertexForStop.get(i)) < 0
            ]
            return {
                "raw_trips": len(raw_trips),
                "loaded_trips": len(loaded_trips),
                "missing_trip_ids": sorted(raw_trips - loaded_trips),
                "raw_stops": len(raw_stops),
                "loaded_stops": len(native.stopIdForIndex),
                "missing_stop_ids": sorted(raw_stops - {str(x) for x in native.stopIdForIndex}),
                "unlinked_stop_ids": linked,
                "transfer_calibration": transfer_proof,
            }
        origins = gpd.GeoDataFrame({"id": ["origin"]}, geometry=[Point(*body["origin"])], crs=4326)
        targets = gpd.GeoDataFrame(
            {"id": [str(i) for i in range(len(body["destinations"]))]},
            geometry=[Point(*p) for p in body["destinations"]],
            crs=4326,
        )
        aware_departure = datetime.fromisoformat(body["departure"])
        departure = aware_departure.replace(tzinfo=None)
        if query_clock(aware_departure) + maximum * 60 > 86400 * 2 + 2700:
            raise ValueError("Control horizon exceeds the prepared service-day window")
        begin = time.perf_counter()
        paths = []
        matrix_class = r5py.TravelTimeMatrix
        if body.get("matrix_paths"):
            import copy

            import com.conveyal.r5
            import jpype

            class MatrixWithPaths(r5py.TravelTimeMatrix):
                def _travel_times_per_origin(self, from_id):
                    request = copy.copy(self.request)
                    request.origin = self.origins.geometry.item()
                    request._regional_task.includePathResults = True
                    request._regional_task.csvResultOptions = jpype.JClass(
                        "com.conveyal.analysis.models.CsvResultOptions"
                    )()
                    request._regional_task.csvResultOptions.feedRepresentation = (
                        com.conveyal.r5.transit.TransitLayer.EntityRepresentation.ID_ONLY
                    )
                    result = com.conveyal.r5.analyst.TravelTimeComputer(
                        request, self.transport_network
                    ).computeTravelTimes()
                    if result.paths is not None:
                        columns = [str(x) for x in result.paths.DATA_COLUMNS]
                        summaries = result.paths.summarizeIterations(
                            com.conveyal.r5.analyst.cluster.PathResult.Stat.MINIMUM
                        )
                        for index, rows in enumerate(summaries):
                            paths.append(
                                {
                                    "target": index,
                                    "paths": [
                                        dict(zip(columns, [str(x) for x in row], strict=True))
                                        for row in rows
                                    ],
                                    "native_iterations": [
                                        {
                                            "departure_seconds": int(
                                                entry.getValue().departureTime
                                            ),
                                            "total_seconds": int(entry.getValue().totalTime),
                                            "access_seconds": int(
                                                entry.getKey().stopSequence.access.time
                                            ),
                                            "egress_seconds": int(
                                                entry.getKey().stopSequence.egress.time
                                            ),
                                            "wait_seconds": [
                                                int(x) for x in entry.getValue().waitTimes.toArray()
                                            ],
                                            "ride_seconds": [
                                                int(x)
                                                for x in (
                                                    entry.getKey().stopSequence.rideTimesSeconds.toArray()
                                                )
                                            ],
                                            "board_stop_ids": [
                                                str(native.stopIdForIndex.get(int(x)))
                                                for x in (
                                                    entry.getKey().stopSequence.boardStops.toArray()
                                                )
                                            ],
                                            "alight_stop_ids": [
                                                str(native.stopIdForIndex.get(int(x)))
                                                for x in (
                                                    entry.getKey().stopSequence.alightStops.toArray()
                                                )
                                            ],
                                            "transfer_seconds": int(
                                                entry.getKey().stopSequence.transferTime(
                                                    entry.getValue()
                                                )
                                            ),
                                        }
                                        for entry in result.paths.iterationsForPathTemplates[
                                            index
                                        ].entries()
                                    ],
                                }
                            )
                    return self._parse_results(from_id, result)

            matrix_class = MatrixWithPaths

        class NormalizedMatrix(matrix_class):
            def _compute(self):
                normalize_task(self.request._regional_task, aware_departure)
                return super()._compute()

        matrix = NormalizedMatrix(
            network,
            origins=origins,
            destinations=targets,
            departure=departure,
            departure_time_window=timedelta(minutes=1),
            transport_modes=[
                r5py.TransportMode.BUS,
                r5py.TransportMode.TRAM,
                r5py.TransportMode.WALK,
            ],
            speed_walking=4.8,
            max_time=timedelta(minutes=maximum),
            max_public_transport_rides=8,
        )
        if body.get("walk_diagnostics"):
            from tools.router.walk_diagnostics import inspect_walks

            return {
                "walks": inspect_walks(
                    network._transport_network,
                    matrix.request._regional_task,
                    body["walk_diagnostics"],
                )
            }
        if body.get("route_sections"):
            import copy

            witness_request = copy.copy(matrix.request)
            witness_request.origin = origins.geometry.item()
            return route_engine.compute(witness_request._regional_task)
        result = {
            "rows": json.loads(pd.DataFrame(matrix).to_json(orient="records")),
            "seconds": time.perf_counter() - begin,
            "from_time": int(matrix.request._regional_task.fromTime),
            "to_time": int(matrix.request._regional_task.toTime),
            "iterations": int(matrix.request._regional_task.getIterationsPerMinute(False)),
            "frequency_services": bool(native.hasFrequencies),
            "access": str(matrix.request._regional_task.accessModes),
            "egress": str(matrix.request._regional_task.egressModes),
            "matrix_paths": paths,
        }
        return result

    return metadata, calculate, controls


def create_server(metadata, calculate, controls, address=("127.0.0.1", 8810)):
    # Independent immutable native point queries must not queue behind area work.
    # Each engine has one slot; obsolete requests cannot accumulate compute work.
    gates = {"point": BoundedSemaphore(1), "coverage": BoundedSemaphore(1)}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # A cancelled browser/API request is not a native calculation failure.
                return

        def do_GET(self):
            self.send(200, metadata) if self.path == "/health" else self.send(404, {})

        def do_POST(self):
            if self.path not in ("/calculate", "/controls", "/point"):
                self.send(404, {})
                return
            gate = gates["point" if self.path == "/point" else "coverage"]
            if not gate.acquire(blocking=False):
                self.send(503, {"error": "Native query busy; retry"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 2048:
                    raise ValueError("Invalid body size")
                body = json.loads(self.rfile.read(length))
                if self.path == "/point":
                    self.send(200, controls(body | {"point_query": True}))
                    return
                if self.path == "/controls" and body.get("profile_calculation"):
                    import cProfile
                    import re

                    label = body["profile_calculation"]
                    if not isinstance(label, str) or not re.fullmatch(r"[a-z0-9]{1,32}", label):
                        raise ValueError("Invalid profile label")
                    query = {k: body[k] for k in ("origin", "departure", "data_version")}
                    profile = cProfile.Profile()
                    value = profile.runcall(calculate, query)
                    target = ROOT / ".cache/qa" / f"v032-native-{label}.prof"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    profile.dump_stats(str(target))
                    self.send(200, value)
                    return
                self.send(200, calculate(body) if self.path == "/calculate" else controls(body))
            except (ValueError, KeyError, TypeError) as error:
                self.send(
                    400,
                    {"error": str(error) if self.path == "/controls" else "Invalid coverage query"},
                )
            except Exception:
                # Native Java errors must not silently close the HTTP socket.
                # The API can preserve the map and offer retry on this status.
                import traceback

                traceback.print_exc()
                self.send(503, {"error": "Native coverage unavailable"})
            finally:
                gate.release()

    return ThreadingHTTPServer(address, Handler)


def main():
    prepare_only = "--prepare-only" in sys.argv
    if prepare_only:
        sys.argv.remove("--prepare-only")
    if os.environ.get("WM_PROFILE_STARTUP") == "1":
        import cProfile

        profiler = cProfile.Profile()
        metadata, calculate, controls = profiler.runcall(initialize)
        profile_path = ROOT / ".state/native-startup.prof"
        profile_path.parent.mkdir(exist_ok=True)
        profiler.dump_stats(profile_path)
    else:
        metadata, calculate, controls = initialize()
    if prepare_only:
        print(json.dumps({"prepared": True, **metadata}), flush=True)
        return

    print(
        json.dumps({"ready": True, **{k: v for k, v in metadata.items() if k != "city"}}),
        flush=True,
    )
    create_server(metadata, calculate, controls).serve_forever()


if __name__ == "__main__":
    main()
