import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from . import __version__
from .api import apikeys, calendar, discover, library, library_editor, library_import, queue, search, series, settings, system
from .api.deps import get_api_key, require_api_key
from .config import config
from .db import init_db
from .frontend import install_frontend
from .jobs import scheduler

logging.basicConfig(
    level=config.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("pullarr")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await scheduler.start()
    log.info("Pullarr %s ready on %s:%d", __version__, config.host, config.port)
    yield
    scheduler.shutdown()


app = FastAPI(title="Pullarr", version=__version__, lifespan=lifespan)

api = FastAPI(dependencies=[Depends(require_api_key)])
api.include_router(library_editor.router)
api.include_router(series.router)
api.include_router(library.router)
api.include_router(library_import.router)
api.include_router(search.router)
api.include_router(discover.router)
api.include_router(calendar.router)
api.include_router(queue.router)
api.include_router(settings.router)
api.include_router(apikeys.router)
api.include_router(system.router)
app.mount("/api/v1", api)


@app.get("/initialize.json")
async def initialize():
    """Bootstrap info for the web UI (same-origin only), mirroring how the
    *arr apps hand their UI the API key."""
    return JSONResponse(
        {"apiKey": get_api_key(), "version": __version__, "urlBase": ""},
        headers={"Cache-Control": "no-store"},
    )


# Serve the built frontend if present (production/Docker)
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.is_dir():
    install_frontend(app, STATIC_DIR)
else:

    @app.get("/")
    async def root():
        return JSONResponse({"app": "pullarr", "version": __version__, "ui": "not built"})


def run() -> None:
    import uvicorn

    uvicorn.run("pullarr.main:app", host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    run()
