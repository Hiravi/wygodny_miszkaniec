"""Isolate pinned r5py1.1.7's basename-only WorkingCopy cache by actual inputs."""

import hashlib
import json

from tools.data.acquire import sha256


def cache_directory(root, inputs, engine_sha):
    fingerprints = {path.name: sha256(path) for path in inputs}
    if len(fingerprints) != len(inputs):
        raise ValueError("Native inputs must have distinct names")
    key = hashlib.sha256(
        json.dumps([engine_sha, fingerprints], sort_keys=True).encode()
    ).hexdigest()
    directory = root / ".cache/router/r5py-inputs" / key
    directory.mkdir(parents=True, exist_ok=True)
    for path in inputs:
        cached = directory / path.name
        if cached.exists() and sha256(cached) != fingerprints[path.name]:
            raise ValueError(f"Native working copy differs from pinned input: {path.name}")
    return directory


def isolate_cache(root, inputs, engine_sha):
    # Config is a singleton and CACHE_DIR is an overridable cached_property in
    # this pinned release. Do not alter HOME/LOCALAPPDATA or delete global caches.
    from r5py.util.config import Config

    directory = cache_directory(root, inputs, engine_sha)
    Config().CACHE_DIR = directory
    return directory


def load_network(r5py, inputs, directory):
    """Use the serialized network when a build closes its backing OSM MapDB.

    In pinned r5py1.1.7 the just-built StreetLayer still references that closed
    store. StreetSegment then raises IllegalAccessError; a deserialized network
    correctly uses its prepared attributes. Reload only after an actual rebuild.
    """
    before = {path.name: path.stat().st_mtime_ns for path in directory.glob("*.transport_network")}
    prepared_only = (directory.parents[3] / "MINIMAL_RUNTIME.json").is_file()
    original = None
    if prepared_only:
        original = r5py.TransportNetwork._load_pickled_transport_network
        if not before or any(
            not path.with_suffix(".warnings").is_file()
            for path in directory.glob("*.transport_network")
        ):
            raise ValueError("Prepared R5 graph/warnings missing; prepare package before launch")

        def read_prepared(self, path):
            try:
                return original(self, path)
            except Exception as error:
                # Upstream catches Java read errors and builds a graph silently.
                # A package launch must fail explicitly instead of rebuilding.
                raise ValueError("Prepared R5 graph cannot be loaded; rerun preparation") from error

        r5py.TransportNetwork._load_pickled_transport_network = read_prepared
    try:
        network = r5py.TransportNetwork(inputs[0], inputs[1:])
    finally:
        if prepared_only:
            r5py.TransportNetwork._load_pickled_transport_network = original
    after = {path.name: path.stat().st_mtime_ns for path in directory.glob("*.transport_network")}
    rebuilt = before != after
    if rebuilt:
        del network
        import java.lang

        java.lang.System.gc()
        network = r5py.TransportNetwork(inputs[0], inputs[1:])
    return network, rebuilt
