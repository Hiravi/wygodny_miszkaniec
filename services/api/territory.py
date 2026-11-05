"""Prepared comparable district aggregates; no third-party fetch per selection."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from services.api.locations import DATA

router = APIRouter(prefix="/api")


@router.get("/district-statistics")
def district_statistics():
    path = DATA / "territory-statistics.json"
    if not path.is_file():
        raise HTTPException(503, "Dane dzielnic są niedostępne.")
    return FileResponse(path, media_type="application/json", headers={"Cache-Control": "no-cache"})
