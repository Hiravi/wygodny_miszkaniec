"""Read-only binary derivative; source identities and full geometry remain unchanged."""

import json
import sqlite3
from contextlib import closing
from functools import lru_cache

from shapely import from_wkb

from services.api.locations import DATA
from tools.data.acquire import ROOT, sha256


@lru_cache(maxsize=2)
def verified(path, manifest_path, signature, source_hash):
    manifest = json.loads(manifest_path.read_text("utf-8"))
    if (
        manifest["method"] != "green-walk-runtime-wkb-v40"
        or manifest["source_sha256"] != source_hash
        or manifest["geometry_crs"] != "EPSG:2180"
        or set(manifest["counts"]) != {"public", "barriers", "buildings"}
        or sha256(path) != manifest["output_sha256"]
    ):
        raise ValueError("Walking runtime derivative mismatch")
    return manifest


def packed(source, source_hash):
    if source.stem != "green-walk-city-v37":
        return None
    path = DATA / "green-walk-runtime-v40.sqlite"
    manifest_path = ROOT / "data/manifests/green-walk-runtime-v40.json"
    if not path.is_file():
        return None  # Complete verified legacy source is still a lawful fallback.
    signature = tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in (path, manifest_path))
    manifest = verified(path, manifest_path, signature, source_hash)
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
            if metadata["source_sha256"] != source_hash or metadata["method"] != manifest["method"]:
                raise ValueError("Walking runtime source differs")
            arrays = {}
            for kind, count in manifest["counts"].items():
                rows = db.execute(
                    "SELECT position,wkb FROM geometry WHERE kind=? ORDER BY position", (kind,)
                ).fetchall()
                if [r[0] for r in rows] != list(range(count)):
                    raise ValueError("Incomplete walking runtime geometry")
                arrays[kind] = from_wkb([r[1] for r in rows])
            return json.loads(metadata["payload"]), arrays
    except sqlite3.Error:
        raise ValueError("Walking runtime index unavailable") from None
