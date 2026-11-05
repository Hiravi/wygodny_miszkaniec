"""Calibrate native R5 transfer tables with its own directed WALK router.

FastRaptor divides distance by speed, omitting per-edge WALK rounding/costs.
Only existing native transfer candidates are validated; this is not a planner.
"""

import hashlib
import json
import time

POLICY = "native-directed-walk-seconds-v33"


def encoded_walk_cost(original_mm, seconds, speed_mm):
    if seconds is None:
        return None
    if seconds < 0 or speed_mm <= 0:
        raise ValueError("Invalid native WALK cost")
    return max(original_mm, seconds * speed_mm)


def calibrate(network, root, sources, jar_hash):
    import com.conveyal.r5 as r5
    import jpype

    started = time.perf_counter()
    native = network.transitLayer
    speed = 4.8 / 3.6
    speed_mm = int(speed * 1000)
    original = [
        ([] if row is None else [int(x) for x in row.toArray()]) for row in native.streetTransfers
    ]
    signature = hashlib.sha256(
        json.dumps(
            {
                "policy": POLICY,
                "sources": sources,
                "jar": jar_hash,
                "speed": speed,
                "candidate_limit_m": 1000,
                "original": original,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    path = root / f".cache/router/walk-transfers-{signature}.json"
    cached = path.exists()
    if cached:
        result = json.loads(path.read_text(encoding="utf-8"))
        assert result["signature"] == signature and result["policy"] == POLICY
    else:
        rows, changed, dropped, count = [], 0, 0, 0
        request = r5.profile.ProfileRequest()
        request.walkSpeed = speed
        for origin, values in enumerate(original):
            vertex = int(native.streetVertexForStop.get(origin))
            output = []
            if values and vertex >= 0:
                walk = r5.streets.StreetRouter(network.streetLayer)
                walk.profileRequest = request
                walk.streetMode = r5.profile.StreetMode.WALK
                walk.quantityToMinimize = (
                    r5.streets.StreetRouter.State.RoutingVariable.DURATION_SECONDS
                )
                walk.distanceLimitMeters = 1000  # Same native TransferFinder candidate boundary.
                walk.timeLimitSeconds = 2700
                walk.setOrigin(vertex)
                walk.route()
                for i in range(0, len(values), 2):
                    target, distance = values[i : i + 2]
                    target_vertex = int(native.streetVertexForStop.get(target))
                    state = walk.getStateAtVertex(target_vertex) if target_vertex >= 0 else None
                    seconds = None if state is None else int(state.getDurationSeconds())
                    cost = encoded_walk_cost(distance, seconds, speed_mm)
                    count += 1
                    if cost is None:
                        dropped += 1
                    else:
                        changed += cost // speed_mm != distance // speed_mm
                        output.extend([target, cost])
            else:
                dropped += len(values) // 2
                count += len(values) // 2
            rows.append(output)
            if origin % 500 == 0:
                print(
                    json.dumps({"transfer_calibration": origin, "total": len(original)}), flush=True
                )
        result = {
            "policy": POLICY,
            "signature": signature,
            "rows": rows,
            "candidate_pairs": count,
            "changed_seconds": changed,
            "unconfirmed_pairs_removed": dropped,
            "gtfs_transfer_origins": int(native.gtfsTransfers.size()),
            "preparation_seconds": round(time.perf_counter() - started, 3),
        }
        path.write_text(json.dumps(result), encoding="utf-8")
    assert len(result["rows"]) == len(original)
    array_list = jpype.JClass("gnu.trove.list.array.TIntArrayList")
    for i, values in enumerate(result["rows"]):
        native.streetTransfers.set(i, array_list(jpype.JArray(jpype.JInt)(values)))
    return {k: v for k, v in result.items() if k != "rows"} | {
        "cache_hit": cached,
        "apply_seconds": round(time.perf_counter() - started, 3),
        "artifact": path.relative_to(root).as_posix(),
    }
