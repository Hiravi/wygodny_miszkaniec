"""Serve one versioned cell matrix, never recompute data when weights change."""

from contextlib import closing

from fastapi import APIRouter, HTTPException

from services.api.cell_data import matrix
from services.api.locations import database, version

router = APIRouter(prefix="/api")


@router.get("/cells")
def cells():
    try:
        result, context, _ = matrix()
        with closing(database()) as connection:
            if version(connection) != context["locations_version"]:
                raise ValueError("Location snapshot mismatch")
        return result
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(503, "Oceny komórek są niedostępne; adresy nadal działają.") from error
