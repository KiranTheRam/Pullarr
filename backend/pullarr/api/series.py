import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..jobs.tasks import refresh_series_full
from ..library.naming import series_folder
from ..metadata.comicvine import ComicVineError, provider as comicvine
from ..models import Issue, Series
from ..schemas import (
    AddSeriesIn,
    IssueMonitorIn,
    SeriesDetailOut,
    SeriesOut,
    SeriesUpdateIn,
)
from ..sources import registry

router = APIRouter(prefix="/series", tags=["series"])


def _series_out(series: Series, issue_count: int, downloaded_count: int) -> SeriesOut:
    out = SeriesOut.model_validate(series)
    out.issue_count = issue_count
    out.downloaded_count = downloaded_count
    return out


async def _normalize_folder_name(session: AsyncSession, series: Series, folder_name: str) -> str:
    """Store the folder relative to the series' root folder when the given path
    is under it (so it survives a root-folder move); otherwise keep as given."""
    from pathlib import Path

    from ..models import RootFolder

    folder_name = folder_name.strip()
    if series.root_folder_id is not None and folder_name.startswith("/"):
        root = await session.get(RootFolder, series.root_folder_id)
        if root is not None:
            try:
                return str(Path(folder_name).relative_to(root.path))
            except ValueError:
                pass  # outside the root — keep absolute
    return folder_name.strip("/") if not folder_name.startswith("/") else folder_name


@router.get("", response_model=list[SeriesOut])
async def list_series(session: AsyncSession = Depends(get_session)):
    counts: dict[int, tuple[int, int]] = {}
    rows = await session.execute(
        select(
            Issue.series_id,
            func.count(Issue.id),
            func.sum(cast(Issue.downloaded, Integer)),
        ).group_by(Issue.series_id)
    )
    for series_id, total, downloaded in rows.all():
        counts[series_id] = (total, int(downloaded or 0))
    result = await session.execute(select(Series).order_by(Series.sort_title, Series.title))
    return [
        _series_out(s, *counts.get(s.id, (0, 0)))
        for s in result.scalars().all()
    ]


@router.post("", response_model=SeriesDetailOut, status_code=201)
async def add_series(body: AddSeriesIn, session: AsyncSession = Depends(get_session)):
    existing = await session.execute(
        select(Series).where(Series.comicvine_id == body.comicvine_id)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(409, "Series already in library")
    await registry.apply_settings(session)  # configures the ComicVine key
    try:
        meta = await comicvine.get_series(str(body.comicvine_id))
    except ComicVineError as exc:
        raise HTTPException(400, str(exc)) from exc
    if meta is None:
        raise HTTPException(404, "ComicVine volume not found")
    series = Series(
        comicvine_id=body.comicvine_id,
        title=meta.title,
        sort_title=meta.title.lower(),
        alt_titles="\n".join(meta.alt_titles),
        description=meta.description,
        publisher=meta.publisher,
        year=meta.year,
        cover_url=meta.cover_url,
        genres=",".join(meta.genres),
        total_issues=meta.total_issues,
        monitored=body.monitored,
        root_folder_id=body.root_folder_id,
        folder_name=series_folder(meta.title, meta.year),
    )
    session.add(series)
    await session.commit()
    await session.refresh(series)
    # fetch the issue list + link sources in the background; a monitored add
    # immediately queues available issues instead of waiting for the next
    # scheduled monitor interval
    asyncio.get_running_loop().create_task(
        refresh_series_full(series.id, grab_missing=body.monitored)
    )
    return await get_series(series.id, session)


@router.get("/{series_id}", response_model=SeriesDetailOut)
async def get_series(series_id: int, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Series)
        .options(selectinload(Series.issues), selectinload(Series.source_links))
        .where(Series.id == series_id)
    )
    series = result.scalar_one_or_none()
    if series is None:
        raise HTTPException(404, "Series not found")
    out = SeriesDetailOut.model_validate(series)
    out.issue_count = len(series.issues)
    out.downloaded_count = sum(1 for i in series.issues if i.downloaded)
    return out


@router.put("/{series_id}", response_model=SeriesDetailOut)
async def update_series(
    series_id: int, body: SeriesUpdateIn, session: AsyncSession = Depends(get_session)
):
    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    if body.monitored is not None:
        series.monitored = body.monitored
    if body.root_folder_id is not None:
        series.root_folder_id = body.root_folder_id
    if body.folder_name is not None:
        series.folder_name = await _normalize_folder_name(session, series, body.folder_name)
    await session.commit()
    return await get_series(series_id, session)


@router.delete("/{series_id}", status_code=204)
async def delete_series(series_id: int, session: AsyncSession = Depends(get_session)):
    from sqlalchemy import delete as sa_delete

    from ..models import Download, HistoryEvent

    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    # remove this series' download + history rows too, so a later series that
    # reuses the id doesn't inherit stale failed-grab records (which would
    # otherwise block re-grabbing those issues)
    await session.execute(sa_delete(Download).where(Download.series_id == series_id))
    await session.execute(sa_delete(HistoryEvent).where(HistoryEvent.series_id == series_id))
    await session.delete(series)
    await session.commit()


@router.post("/{series_id}/refresh", status_code=202)
async def refresh_series(series_id: int, session: AsyncSession = Depends(get_session)):
    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(404, "Series not found")
    asyncio.get_running_loop().create_task(refresh_series_full(series_id))
    return {"status": "refreshing"}


@router.put("/{series_id}/issues/monitor", status_code=204)
async def monitor_issues(
    series_id: int, body: IssueMonitorIn, session: AsyncSession = Depends(get_session)
):
    result = await session.execute(
        select(Issue).where(Issue.series_id == series_id, Issue.id.in_(body.issue_ids))
    )
    for issue in result.scalars().all():
        issue.monitored = body.monitored
    await session.commit()
