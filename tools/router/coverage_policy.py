"""Presentation policy for quantized R5 results; never reconstruct precise seconds."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def minute_band(minutes):
    if minutes is None or not 0 <= minutes < 45:
        return None
    return next(n for n in (15, 30, 45) if minutes < n)


def transition_day(value: datetime):
    local = value.astimezone(ZoneInfo("Europe/Warsaw"))
    noon = local.replace(hour=12)
    return noon.utcoffset() != (noon - timedelta(days=1)).utcoffset()
