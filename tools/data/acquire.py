"""Acquire published source archives; downloading alone never implies validation."""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
USER_AGENT = "WygodnyMieszkaniecPilot/0.1"
MAX_ARCHIVE_BYTES = 1_000_000_000
MSIP_TERMS = "https://msip.krakow.pl/getHtml?dok_id=228972"
PORTAL_TERMS = (
    "https://otwartedane.um.krakow.pl/warunki-wykorzystania-danych-udostepnianych-w-portalu"
)
SOURCES = [
    (
        "msip_addresses_1492",
        "Punkty adresowe (EMUiA)",
        "https://msip.krakow.pl/dataset/1492",
        "https://msip.um.krakow.pl/Dane/Adresy_JSON.zip",
        MSIP_TERMS,
        None,
    ),
    (
        "msip_districts_1483",
        "Podział administracyjny - dzielnice",
        "https://msip.krakow.pl/dataset/1483",
        "https://msip.um.krakow.pl/Dane/Dzielnice_JSON.zip",
        MSIP_TERMS,
        None,
    ),
    (
        "msip_noise_1361_2022",
        "Mapy hałasu 2022",
        "https://msip.krakow.pl/dataset/1361",
        "https://msip.um.krakow.pl/Dane/Mapa_Halasu_2022_JSON.zip",
        MSIP_TERMS,
        "2022",
    ),
    *[
        (
            f"ztp_gtfs_{feed.lower()}",
            f"GTFS_KRK_{feed}",
            "https://gtfs.ztp.krakow.pl/",
            f"https://gtfs.ztp.krakow.pl/GTFS_KRK_{feed}.zip",
            PORTAL_TERMS,
            None,
        )
        for feed in ("A", "M", "T")
    ],
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def acquire(source: tuple, refresh: bool) -> dict:
    source_id, title, landing_url, download_url, terms_url, produced_period = source
    raw = ROOT / "data/raw" / f"{source_id}.zip"
    manifest_path = ROOT / "data/manifests" / f"{source_id}.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if not refresh and raw.exists() and manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("sha256") == sha256(raw):
            return {"source_id": source_id, "status": "cached_verified_checksum"}
    manifest = {
        "schema_version": "1",
        "etl_version": "acquire-v1",
        "source_id": source_id,
        "title": title,
        "publisher": "Zarząd Transportu Publicznego w Krakowie"
        if source_id.startswith("ztp")
        else "Urząd Miasta Krakowa",
        "landing_url": landing_url,
        "download_url": download_url,
        "produced_at": None,
        "produced_period": produced_period,
        "acquired_at": None,
        "terms_url": terms_url,
        "terms_status": "pending_review",
        "attribution": None,
        "source_crs": None,
        "spatial_resolution": None,
        "coverage": None,
        "status": "catalog_confirmed",
        "sha256": None,
        "issues": [],
    }
    partial = raw.with_suffix(".zip.partial")
    started = time.perf_counter()
    try:
        request = Request(download_url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=45) as response, partial.open("wb") as output:
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise ValueError("Archive exceeds acquisition size limit")
                output.write(chunk)
            manifest["http_last_modified"] = response.headers.get("Last-Modified")
            manifest["resolved_download_url"] = response.url
        with ZipFile(partial) as archive:
            manifest["archive_entries"] = [
                {"name": info.filename, "bytes": info.file_size} for info in archive.infolist()
            ]
        partial.replace(raw)
        manifest.update(
            {
                "status": "downloaded",
                "sha256": sha256(raw),
                "bytes": size,
                "acquired_at": datetime.now(UTC).isoformat(),
                "download_seconds": round(time.perf_counter() - started, 3),
            }
        )
    except Exception as error:
        manifest.update({"status": "blocked", "issues": [str(error)]})
        # A failed refresh must not discard a prior usable acquisition record.
        if manifest_path.exists():
            manifest_path = manifest_path.with_name(f"{source_id}.attempt.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "source_id": source_id,
        "status": manifest["status"],
        "bytes": manifest.get("bytes"),
        "issues": manifest["issues"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--source", action="append", choices=[source[0] for source in SOURCES])
    args = parser.parse_args()
    failed = False
    for source in SOURCES:
        if args.source and source[0] not in args.source:
            continue
        result = acquire(source, args.refresh)
        print(json.dumps(result), flush=True)
        failed |= result["status"] == "blocked"
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
