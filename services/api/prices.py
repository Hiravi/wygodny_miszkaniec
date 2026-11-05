"""Prepared RCN references; no raw sales archive or runtime aggregation."""

import json
import sqlite3
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.api.locations import DATA

router = APIRouter(prefix="/api")
Market = Literal["all", "primary", "secondary"]
Window = Literal["2025", "2026_partial", "2025_2026", "2024_2026"]


class PriceReference(BaseModel):
    value: float | None
    count: int
    transactions: int
    reference_level: str
    period: str
    market: Market


@router.get("/price-reference/{identifier}", response_model=PriceReference)
def reference(identifier: str, market: Market = "all", window: Window = "2025_2026"):
    path = DATA / "price-references.sqlite"
    if not path.is_file():
        raise HTTPException(503, "Ceny chwilowo niedostępne")
    kind = "district" if identifier.startswith("district:") else "reference"
    key = identifier.split(":", 1)[-1]
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT value FROM references_by_area WHERE window=? AND market=? AND kind=? AND id=?",
            (window, market, kind, key),
        ).fetchone()
    data = json.loads(row[0]) if row else {"value": None, "count": 0, "transactions": 0}
    level = data.get(
        "reference_level", "district_reference" if data["value"] is not None else "unavailable"
    )
    return PriceReference(
        **{k: data[k] for k in ("value", "count", "transactions")},
        reference_level=level,
        period={
            "2025": "2025",
            "2026_partial": "2026 · część roku",
            "2025_2026": "2025–2026",
            "2024_2026": "2024–2026",
        }[window],
        market=market,
    )
