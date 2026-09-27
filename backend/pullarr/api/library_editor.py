"""Bulk library actions, with per-series results and safe folder moves."""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..jobs.service import create_job
from ..jobs.tasks import refresh_series_full
from ..library.move import (MoveError, finish_move, moved_paths, prepare_move,
                            rebase_file_paths, undo_move)
from ..library.work import run_library_work
from ..models import (Download, DownloadStatus, Job, JobKind, JobStatus, RootFolder,
                      Series, SeriesFolder)
from ..monitoring import apply_mode, issue_wanted
from ..schemas import SeriesBulkEditIn, SeriesBulkIn, SeriesBulkRefreshIn
from .series import remove_series_record

log = logging.getLogger(__name__)
router = APIRouter(prefix="/series/editor", tags=["series"])


async def _load(session: AsyncSession, series_id: int) -> Series | None:
    return (await session.execute(select(Series).options(
        selectinload(Series.issues), selectinload(Series.extra_folders),
        selectinload(Series.root_folder),
    ).where(Series.id == series_id))).scalar_one_or_none()


async def _busy(session: AsyncSession, series_id: int) -> bool:
    active_download = (await session.execute(select(Download.id).where(
        Download.series_id == series_id,
        Download.status.in_([DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING,
                            DownloadStatus.IMPORTING]),
    ).limit(1))).scalar_one_or_none()
    active_job = (await session.execute(select(Job.id).where(
        Job.series_id == series_id,
        Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
    ).limit(1))).scalar_one_or_none()
    return active_download is not None or active_job is not None


async def _change_root(session: AsyncSession, series: Series, root: RootFolder,
                       move_files: bool) -> bool:
    if series.root_folder is None:
        raise MoveError("Series has no current root folder")
    if await _busy(session, series.id):
        raise MoveError("Downloads or a series job are in progress")
    old_root = Path(series.root_folder.path)
    source, destination, relative = moved_paths(series, Path(root.path))
    if source.exists() and source.resolve() == destination.resolve():
        # Two mount paths can point at the same directory.
        series.root_folder = root
        series.folder_name = relative
        await session.commit()
        return False
    if destination.exists():
        raise MoveError(f"Destination already exists: {destination}")
    if move_files and source.exists():
        copied = await run_library_work(prepare_move, source, destination)
        try:
            rebase_file_paths(series, source, destination, old_root, Path(root.path))
            series.root_folder = root
            series.folder_name = relative
            await session.commit()
        except Exception:
            await session.rollback()
            await run_library_work(undo_move, source, destination, copied)
            raise
        try:
            await run_library_work(finish_move, source, copied)
        except OSError as exc:
            log.warning("Old copy remains after moving %s: %s", series.title, exc)
        return True
    # Keep existing files in place and let new downloads use the new root.
    for extra in series.extra_folders:
        if not Path(extra.path).is_absolute():
            extra.path = str(old_root / extra.path)
    if source.exists() and not any(Path(extra.path) == source for extra in series.extra_folders):
        series.extra_folders.append(SeriesFolder(path=str(source)))
    series.root_folder = root
    series.folder_name = relative
    await session.commit()
    return False


@router.put("")
async def edit_series_bulk(body: SeriesBulkEditIn, session: AsyncSession = Depends(get_session)):
    if body.monitor_mode == "from_issue" and (body.monitor_from is None or body.monitor_from <= 0):
        raise HTTPException(422, "Starting issue must be greater than zero")
    new_root = None
    if body.root_folder_id is not None:
        new_root = await session.get(RootFolder, body.root_folder_id)
        if new_root is None:
            raise HTTPException(404, "Root folder not found")
    result: dict = {"updated": 0, "moved": 0, "problems": []}
    for series_id in dict.fromkeys(body.series_ids):
        series = await _load(session, series_id)
        if series is None:
            result["problems"].append({"series_id": series_id, "detail": "Series not found"})
            continue
        title = series.title
        try:
            if new_root is not None and series.root_folder_id != new_root.id:
                if await _change_root(session, series, new_root, body.move_files):
                    result["moved"] += 1
            changed_monitor = body.monitored is not None and body.monitored != series.monitored
            if changed_monitor:
                series.monitored = body.monitored
            if body.monitor_mode is not None and (
                body.monitor_mode != series.monitor_mode
                or (body.monitor_mode == "from_issue" and body.monitor_from != series.monitor_from)
            ):
                series.monitor_mode = body.monitor_mode
                series.monitor_from = body.monitor_from if body.monitor_mode == "from_issue" else None
                apply_mode(series, series.issues)
            elif changed_monitor:
                for issue in series.issues:
                    issue.monitored = issue_wanted(series, issue.number)
            await session.commit()
            result["updated"] += 1
        except (MoveError, OSError) as exc:
            await session.rollback()
            result["problems"].append({"series_id": series_id, "title": title,
                                       "detail": str(exc)})
    return result


async def _refresh_in_order(jobs: list[tuple[int, int]], search: bool) -> None:
    for series_id, job_id in jobs:
        await refresh_series_full(series_id, grab_missing=search,
                                  only_monitored=search, job_id=job_id)


@router.post("/refresh", status_code=202)
async def refresh_series_bulk(body: SeriesBulkRefreshIn, session: AsyncSession = Depends(get_session)):
    jobs = []
    for series_id in dict.fromkeys(body.series_ids):
        if await session.get(Series, series_id) is None:
            continue
        job = await create_job(session,
            JobKind.SEARCH_MISSING if body.search_missing else JobKind.REFRESH_SERIES,
            series_id=series_id)
        jobs.append((series_id, job.id))
    if jobs:
        asyncio.get_running_loop().create_task(_refresh_in_order(jobs, body.search_missing))
    return {"count": len(jobs)}


@router.post("/delete")
async def delete_series_bulk(body: SeriesBulkIn, session: AsyncSession = Depends(get_session)):
    result: dict = {"removed": 0, "problems": []}
    for series_id in dict.fromkeys(body.series_ids):
        series = await session.get(Series, series_id)
        if series is None:
            continue
        if await _busy(session, series_id):
            result["problems"].append({"series_id": series_id,
                                       "title": series.title,
                                       "detail": "Downloads or a series job are in progress"})
            continue
        await remove_series_record(series, session)
        result["removed"] += 1
    return result
