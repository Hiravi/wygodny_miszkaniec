"""Validated optional precomputed matrix; point observations stay separate."""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tools.data.acquire import sha256
from tools.data.cells import cell_id

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / os.environ.get("WM_DATA_DIR", "data/derived/generated")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CellMetric(Model):
    criterion: Literal["transport", "quiet", "green", "services"]
    status: Literal["derived", "unavailable"]
    score: float | None = Field(ge=0, le=100, allow_inf_nan=False)
    raw_value: str | None
    unit: str | None
    source_ids: list[str] = Field(min_length=1)
    method_version: str | None

    @model_validator(mode="after")
    def available_score(self):
        if self.status == "derived" and (
            self.score is None or self.method_version is None or self.raw_value is None
        ):
            raise ValueError("Derived metric requires validated value and method")
        if self.status == "unavailable" and self.score is not None:
            raise ValueError("Missing cannot become zero")
        return self


class Representative(Model):
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    node_id: int = Field(gt=0)
    way_id: int = Field(gt=0)
    access_version: str
    native_walk_seconds: int = Field(gt=0)


class Cell(Model):
    cell_id: str = Field(pattern=r"^2180:-?\d+:-?\d+$")
    representative: Representative
    metrics: list[CellMetric] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def consistency(self):
        if len({metric.criterion for metric in self.metrics}) != 4:
            raise ValueError("Four unique criteria required")
        if cell_id(self.representative.longitude, self.representative.latitude) != self.cell_id:
            raise ValueError("Representative must be in its cell")
        return self


class CellDetail(Model):
    matrix_version: str
    locations_version: str
    scoring_version: str
    reference: str
    cell: Cell | None


@lru_cache(maxsize=1)
def load_matrix(path: Path, signature: tuple, manifest_path: Path, manifest_signature: tuple):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if sha256(path) != manifest["output_sha256"]:
        raise ValueError("Matrix checksum mismatch")
    result = json.loads(path.read_text(encoding="utf-8"))
    context = {
        key: result[key]
        for key in ("matrix_version", "locations_version", "scoring_version", "reference")
    }
    if any(context[key] != manifest[key] for key in context):
        raise ValueError("Matrix context mismatch")
    cells = {}
    for feature in result["features"]:
        cell = Cell.model_validate(feature["properties"])
        if cell.cell_id in cells or cell.cell_id != feature["id"]:
            raise ValueError("Duplicate or inconsistent cell ID")
        cells[cell.cell_id] = cell
    return result, context, cells


def matrix():
    path, manifest_path = DATA / "cells.geojson", DATA / "cells-manifest.json"
    # The full import uses the committed manifest; explicitly isolated test data uses its own.
    if DATA == ROOT / "data/derived/generated":
        manifest_path = ROOT / "data/manifests/cell-matrix-v1.json"
    a, b = path.stat(), manifest_path.stat()
    return load_matrix(path, (a.st_mtime_ns, a.st_size), manifest_path, (b.st_mtime_ns, b.st_size))


def lookup(longitude: float, latitude: float, locations_version: str) -> CellDetail | None:
    try:
        _, context, cells = matrix()
        if context["locations_version"] != locations_version:
            return None
        return CellDetail(**context, cell=cells.get(cell_id(longitude, latitude)))
    except (OSError, ValueError, KeyError, TypeError):
        return None  # Matrix outage does not disable real address/point observations.
