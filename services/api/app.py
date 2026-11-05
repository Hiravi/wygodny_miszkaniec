"""Health reports process availability, not product readiness."""

from contextlib import closing
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, ConfigDict

from services.api.air import router as air_router
from services.api.buildings import router as buildings_router
from services.api.cells import router as cells_router
from services.api.comfort import router as comfort_router
from services.api.coverage import router as coverage_router
from services.api.green_walk import router as green_walk_router
from services.api.greenery import router as greenery_router
from services.api.locations import DATA, database, router, version
from services.api.map_context import router as map_router
from services.api.map_tiles import router as map_tiles_router
from services.api.neighbourhood import router as neighbourhood_router
from services.api.network import router as network_router
from services.api.noise_point import router as noise_router
from services.api.parks import router as parks_router
from services.api.prices import router as prices_router
from services.api.routing import router as routing_router
from services.api.routing_context import router as routing_context_router
from services.api.territory import router as territory_router
from services.api.ux_layers import router as ux_layers_router


class Health(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    status: Literal["ok"] = "ok"
    data_version: str | None = None
    routing_status: Literal["unavailable"] = "unavailable"


app = FastAPI(title="Wygodny mieszkaniec", version="0.0.0")
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.include_router(router)
app.include_router(routing_router)
app.include_router(routing_context_router)
app.include_router(cells_router)
app.include_router(map_router)
app.include_router(map_tiles_router)
app.include_router(network_router)
app.include_router(parks_router)
app.include_router(prices_router)
app.include_router(coverage_router)
app.include_router(greenery_router)
app.include_router(green_walk_router)
app.include_router(neighbourhood_router)
app.include_router(noise_router)
app.include_router(air_router)
app.include_router(territory_router)
app.include_router(buildings_router)
app.include_router(ux_layers_router)
app.include_router(comfort_router)


@app.get("/api/health", response_model=Health)
def health() -> Health:
    if not (DATA / "locations.sqlite").is_file():
        return Health()
    with closing(database()) as connection:
        return Health(data_version=version(connection))
