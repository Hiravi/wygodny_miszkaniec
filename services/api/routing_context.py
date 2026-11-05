"""Choose a demo departure from the actual imported all-feed service dates."""

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

ROOT = Path(__file__).resolve().parents[2]
router = APIRouter(prefix="/api")


def warsaw_now():
    return datetime.now(ZoneInfo("Europe/Warsaw"))


def default_date(dates, today=None):
    days = sorted({date.fromisoformat(day) for day in dates})
    today = today or warsaw_now().date()
    return next((day for day in days if day >= today), days[-1] if days else None)


class RoutingContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_date: date | None
    today_supported: bool = False
    default_time: str = "08:00"


@router.get("/transit-context", response_model=RoutingContext)
def context():
    try:
        coverage = json.loads(
            (ROOT / "data/manifests/gtfs-coverage-v1.json").read_text(encoding="utf-8")
        )
        now = warsaw_now()
        dates = coverage["common_active_service_dates"]
        supported = now.date().isoformat() in dates
        return RoutingContext(
            default_date=default_date(dates, now.date()),
            today_supported=supported,
            default_time=now.strftime("%H:%M") if supported else "08:00",
        )
    except (OSError, ValueError, KeyError, TypeError):
        return RoutingContext(default_date=None)
