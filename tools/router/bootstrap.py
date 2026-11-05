"""Download pinned runtime/OSM artifacts from the checked acquisition manifest."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]


def provision_verified(source: Path, target: Path, expected_sha256: str) -> None:
    """An offline artifact must match the pinned bytes before anything is copied."""
    for path in (source, target) if target.exists() else (source,):
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256:
                raise ValueError(f"Artifact checksum mismatch: {path.name}")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def download_verified(url: str, target: Path, expected_sha256: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        partial = target.with_suffix(target.suffix + ".partial")
        with (
            urlopen(
                Request(url, headers={"User-Agent": "WygodnyMieszkaniecPilot/0.1"}), timeout=45
            ) as response,
            partial.open("wb") as output,
        ):
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > 1_000_000_000:
                    raise ValueError("Artifact exceeds download bound")
                output.write(chunk)
        partial.replace(target)
    with target.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256:
            raise ValueError(f"Artifact checksum mismatch: {target.name}")
    print(f"Verified {target.name}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--osm-only", action="store_true", help="Acquire map context without Java/OTP"
    )
    parser.add_argument(
        "--analysis", action="store_true", help="Also acquire pinned R5 for one-to-many"
    )
    parser.add_argument(
        "--osm-file", type=Path, help="Verified local pinned OSM when the archive URL expires"
    )
    args = parser.parse_args()
    manifest = json.loads((ROOT / "data/manifests/router-runtime.json").read_text(encoding="utf-8"))
    if args.osm_file:
        provision_verified(
            args.osm_file.resolve(),
            ROOT / "data/raw/malopolskie-261011.osm.pbf",
            manifest["osm_sha256"],
        )
    if args.osm_only:
        download_verified(
            manifest["osm_url"],
            ROOT / "data/raw/malopolskie-261011.osm.pbf",
            manifest["osm_sha256"],
        )
        return
    cache = ROOT / ".cache/router"
    java_archive = cache / manifest["java_url"].rsplit("/", 1)[1]
    download_verified(manifest["java_url"], java_archive, manifest["java_sha256"])
    with ZipFile(java_archive) as archive:
        for entry in archive.infolist():
            if not (cache / entry.filename).resolve().is_relative_to(cache.resolve()):
                raise ValueError("Archive path outside runtime cache")
        if not (cache / "jdk-25.0.4.1+1/bin/java.exe").exists():
            archive.extractall(cache)
    download_verified(manifest["otp_url"], cache / "otp-shaded-2.10.0.jar", manifest["otp_sha256"])
    if args.analysis:
        from tools.router.spike import R5_SHA256, R5_URL

        download_verified(R5_URL, cache / "r5-v7.5.1-r5py-all.jar", R5_SHA256)
        from tools.router.instant_point_runtime import build

        build(ROOT)
    download_verified(
        manifest["osm_url"], ROOT / "data/raw/malopolskie-261011.osm.pbf", manifest["osm_sha256"]
    )


if __name__ == "__main__":
    main()
