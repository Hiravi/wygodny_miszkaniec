"""Deterministic official snapshot import; analytical geometry never simplified."""

import hashlib
import json
import sqlite3
import unicodedata
from zipfile import ZipFile

import ijson
from pyproj import Transformer
from shapely import to_wkb
from shapely import transform as transform_geometry
from shapely.geometry import mapping, shape

from tools.data.acquire import ROOT, sha256
from tools.data.inspect_sources import require_crs


def normalize(text: str) -> str:
    text = text.casefold().replace("ł", "l")
    return " ".join(
        "".join(
            c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
        ).split()
    )


def write_json(path, value):
    path.write_text(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def features(source, entry):
    with ZipFile(ROOT / f"data/raw/{source}.zip") as archive:
        with archive.open(entry) as stream:
            require_crs(next(ijson.items(stream, "crs.properties.name")))
        with archive.open(entry) as stream:
            yield from ijson.items(stream, "features.item", use_float=True)


def main():
    sources = ["msip_addresses_1492", "msip_districts_1483", "msip_noise_1361_2022"]
    inputs = {}
    metadata = []
    for source in sources:
        manifest = json.loads((ROOT / f"data/manifests/{source}.json").read_text(encoding="utf-8"))
        inputs[source] = sha256(ROOT / f"data/raw/{source}.zip")
        if inputs[source] != manifest["sha256"]:
            raise ValueError("Source checksum mismatch")
        metadata.append(manifest)
    version = (
        "locations-v1-"
        + hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
    )
    output = ROOT / "data/derived/generated"
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / "locations.build.sqlite"
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    connection.executescript("""
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE addresses (id TEXT PRIMARY KEY, label TEXT NOT NULL, normalized TEXT NOT NULL,
                                longitude REAL NOT NULL, latitude REAL NOT NULL,
                                source_district TEXT);
        CREATE VIRTUAL TABLE address_search USING fts5(id UNINDEXED, normalized,
                                                     tokenize='unicode61 remove_diacritics 2');
        CREATE TABLE districts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, geometry BLOB NOT NULL);
        CREATE TABLE noise (id INTEGER PRIMARY KEY, lower_db REAL, upper_db REAL,
                            geometry BLOB NOT NULL);
        CREATE VIRTUAL TABLE noise_bounds USING rtree(id,minx,maxx,miny,maxy);
    """)
    to_wgs = Transformer.from_crs(2178, 4326, always_xy=True).transform
    to_metric = Transformer.from_crs(2178, 2180, always_xy=True).transform
    from_metric = Transformer.from_crs(2180, 4326, always_xy=True).transform
    counts = {}
    districts = []
    for feature in features(sources[1], "Dzielnice.geojson"):
        properties = feature["properties"]
        identifier = int(properties["id_dzielni"])
        geometry = transform_geometry(shape(feature["geometry"]), to_wgs, interleaved=False)
        name = properties["nazwa_peln"]
        connection.execute(
            "INSERT INTO districts VALUES (?,?,?)", (identifier, name, to_wkb(geometry))
        )
        display = transform_geometry(
            shape(feature["geometry"]), to_metric, interleaved=False
        ).simplify(3, preserve_topology=True)
        display = transform_geometry(display, from_metric, interleaved=False)
        districts.append(
            {
                "type": "Feature",
                "id": identifier,
                "properties": {"id": identifier, "name": name},
                "geometry": mapping(display),
            }
        )
    districts.sort(key=lambda f: f["id"])
    for feature in features(sources[0], "Adresy.geojson"):
        p = feature["properties"]
        identifier = str(feature["id"])
        label = f"{p['nazwa_ulicy']} {p['numer_adresowy']}, {p['miejscowosc']}"
        longitude, latitude = to_wgs(*feature["geometry"]["coordinates"])
        connection.execute(
            "INSERT INTO addresses VALUES (?,?,?,?,?,?)",
            (identifier, label, normalize(label), longitude, latitude, str(p["nazwa_dzielnicy"])),
        )
        connection.execute(
            "INSERT INTO address_search VALUES (?,?)", (identifier, normalize(label))
        )
    entry = "Mapa_imisyjna_2022_JSON/halas_2022_imisja_dr_LDWN.geojson"
    for feature in features(sources[2], entry):
        geometry = transform_geometry(shape(feature["geometry"]), to_wgs, interleaved=False)
        p = feature["properties"]
        identifier = int(p["objectid"])
        connection.execute(
            "INSERT INTO noise VALUES (?,?,?,?)",
            (identifier, p["isov1"], p["isov2"], to_wkb(geometry)),
        )
        minx, miny, maxx, maxy = geometry.bounds
        connection.execute(
            "INSERT INTO noise_bounds VALUES (?,?,?,?,?)", (identifier, minx, maxx, miny, maxy)
        )
    for name in ("addresses", "districts", "noise"):
        counts[name] = connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    connection.execute("INSERT INTO metadata VALUES ('version',?)", (version,))
    connection.commit()
    connection.execute("VACUUM")
    connection.close()
    temporary.replace(output / "locations.sqlite")
    write_json(output / "districts.geojson", {"type": "FeatureCollection", "features": districts})
    write_json(output / "sources.json", {"data_version": version, "sources": metadata})
    report = {
        "schema_version": "1",
        "etl_version": "locations-import-v1",
        "data_version": version,
        "input_sha256": inputs,
        "counts": counts,
        "source_crs": "EPSG:2178",
        "api_crs": "EPSG:4326",
        "display_simplification_m": 3,
        "analytical_geometry": "Full source coordinates transformed; no simplification.",
        "output_sha256": {
            p.name: sha256(p)
            for p in [
                output / "locations.sqlite",
                output / "districts.geojson",
                output / "sources.json",
            ]
        },
    }
    write_json(ROOT / "data/manifests/locations-import-v1.json", report)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
