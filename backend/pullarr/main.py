import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .api import apikeys, calendar, discover, library, library_editor, library_import, queue, search, series, settings, system
from .api.deps import get_api_key, require_api_access
from .browser_setup import load_browser_login
from .config import config
from .db import init_db
from .frontend import install_frontend
from .jobs import scheduler
from .security import access_boundary

logging.basicConfig(
    level=config.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
# HTTP client request URLs can carry integration credentials: ComicVine, Torznab
# and Kavita all take their API key as a query parameter.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("pullarr")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.auth_username and load_browser_login() is None:
        if config.allow_unauthenticated_browser:
            log.warning("Browser login is disabled; reachable clients can obtain API access.")
        else:
            log.info("Open Pullarr on a trusted network to create the browser login.")
    await init_db()
    await scheduler.start()
    log.info("Pullarr %s ready on %s:%d", __version__, config.host, config.port)
    yield
    scheduler.shutdown()


app = FastAPI(title="Pullarr", version=__version__, lifespan=lifespan)
app.middleware("http")(access_boundary)

api = FastAPI(dependencies=[Depends(require_api_access)])
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


@app.get("/ping")
async def ping():
    """Public liveness only: no database, integrations, sessions, or secrets."""
    return JSONResponse({"status": "OK"}, headers={"Cache-Control": "no-store"})


@app.get("/initialize.json")
async def initialize(request: Request):
    """Browser bootstrap, protected by the browser access boundary."""
    mode = "managed" if request.state.browser_session else "environment" if config.auth_username else "external"
    return JSONResponse(
        {"apiKey": "" if mode == "managed" else get_api_key(), "authMode": mode,
         "version": __version__, "urlBase": ""},
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

    uvicorn.run(
        "pullarr.main:app", host=config.host, port=config.port, log_level="info",
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", ""),
    )


if __name__ == "__main__":
    run()
