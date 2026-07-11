from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .. import settings_service
from ..db import get_session
from ..download.qbittorrent import QbtError, test_connection
from ..metadata.comicvine import ComicVineError, provider as comicvine
from ..metadata.metron import MetronError, provider as metron
from ..schemas import ComicVineTestIn, MetronTestIn, QbtTestIn

router = APIRouter(prefix="/settings", tags=["settings"])

MASK = "••••••••"


@router.get("")
async def get_settings(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    values = await settings_service.get_all(session)
    for key in settings_service.SECRET_KEYS:
        if values.get(key):
            values[key] = MASK
    return values


@router.put("")
async def update_settings(
    body: dict[str, str], session: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    # ignore masked secrets that the user did not change
    to_save = {k: v for k, v in body.items() if v != MASK}
    try:
        await settings_service.set_many(session, to_save)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # apply a changed monitor interval immediately (no restart needed)
    if "monitor_interval_minutes" in to_save:
        from ..jobs.scheduler import reschedule_monitor

        try:
            reschedule_monitor(settings_service.parse_monitor_interval(to_save["monitor_interval_minutes"]))
        except ValueError:
            pass
    return await get_settings(session)


@router.post("/qbittorrent/test")
async def qbt_test(body: QbtTestIn, session: AsyncSession = Depends(get_session)):
    password = body.password
    if password == MASK:
        password = await settings_service.get(session, "qbittorrent_password")
    try:
        version = await test_connection(body.url, body.username, password)
    except QbtError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "version": version}


@router.post("/comicvine/test")
async def comicvine_test(body: ComicVineTestIn, session: AsyncSession = Depends(get_session)):
    api_key = body.api_key
    if api_key == MASK:
        api_key = await settings_service.get(session, "comicvine_api_key")
    comicvine.configure(api_key)
    try:
        results = await comicvine.search("Batman", limit=1)
    except ComicVineError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, f"ComicVine request failed: {exc}") from exc
    if not results:
        raise HTTPException(400, "ComicVine responded but returned no results")
    return {"ok": True}


@router.post("/metron/test")
async def metron_test(body: MetronTestIn, session: AsyncSession = Depends(get_session)):
    username = body.username
    password = body.password
    if password == MASK:
        password = await settings_service.get(session, "metron_password")
    metron.configure(username, password)
    try:
        result = await metron.test()
    except MetronError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, f"Metron request failed: {exc}") from exc
    return result
