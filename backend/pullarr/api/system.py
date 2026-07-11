from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..db import get_session
from ..models import Issue, Download, DownloadStatus, Job, JobStatus, RootFolder, Series
from ..schemas import JobOut, RootFolderIn, RootFolderOut, SystemStatus, WantedItemOut
from ..util import is_released

router = APIRouter(tags=["system"])


@router.get("/system/status", response_model=SystemStatus)
async def system_status(session: AsyncSession = Depends(get_session)):
    series_count = (await session.execute(select(func.count(Series.id)))).scalar_one()
    issue_count = (await session.execute(select(func.count(Issue.id)))).scalar_one()
    downloaded = (
        await session.execute(select(func.sum(cast(Issue.downloaded, Integer))))
    ).scalar_one() or 0
    queue_count = (
        await session.execute(
            select(func.count(Download.id)).where(
                Download.status.in_(
                    [DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING,
                     DownloadStatus.IMPORTING, DownloadStatus.NEEDS_ATTENTION]
                )
            )
        )
    ).scalar_one()
    return SystemStatus(
        version=__version__,
        series_count=series_count,
        issue_count=issue_count,
        downloaded_count=int(downloaded),
        queue_count=queue_count,
    )


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    series_id: int | None = None,
    active: bool = False,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    query = (
        select(Job, Series.title)
        .outerjoin(Series, Job.series_id == Series.id)
        .order_by(Job.id.desc())
        .limit(min(max(limit, 1), 500))
    )
    if series_id is not None:
        query = query.where(Job.series_id == series_id)
    if active:
        query = query.where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
    rows = await session.execute(query)
    result: list[JobOut] = []
    for job, title in rows.all():
        out = JobOut.model_validate(job)
        out.series_title = title or ""
        result.append(out)
    return result


@router.get("/wanted", response_model=list[WantedItemOut])
async def wanted(
    limit: int = 100,
    scope: str = "automatic",
    session: AsyncSession = Depends(get_session),
):
    if scope not in {"automatic", "missing", "future"}:
        raise HTTPException(422, "scope must be automatic, missing, or future")
    query = (
        select(Issue, Series.title, Series.cover_url, Series.monitored)
        .join(Series, Issue.series_id == Series.id)
        .where(Issue.downloaded == False)  # noqa: E712
        .order_by(Series.title, Issue.number)
    )
    if scope in {"automatic", "future"}:
        query = query.where(
            Issue.monitored == True,  # noqa: E712
            Series.monitored == True,  # noqa: E712
        )
    result = await session.execute(query)
    items: list[WantedItemOut] = []
    for ch, title, cover, series_monitored in result.all():
        released = is_released(ch.released_at)
        if scope == "future" and released:
            continue
        if scope != "future" and not released:
            continue
        items.append(WantedItemOut(
            issue_id=ch.id,
            series_id=ch.series_id,
            series_title=title,
            cover_url=cover,
            number=ch.number,
            display_number=ch.display_number,
            volume=ch.volume,
            title=ch.title,
            released_at=ch.released_at,
            series_monitored=series_monitored,
            issue_monitored=ch.monitored,
        ))
        if len(items) >= limit:
            break
    return items


@router.get("/rootfolders", response_model=list[RootFolderOut])
async def list_root_folders(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(RootFolder).order_by(RootFolder.id))
    return result.scalars().all()


@router.post("/rootfolders", response_model=RootFolderOut, status_code=201)
async def add_root_folder(body: RootFolderIn, session: AsyncSession = Depends(get_session)):
    path = Path(body.path).expanduser()
    if not path.is_absolute():
        raise HTTPException(400, "Root folder path must be absolute")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"Cannot create folder: {exc}") from exc
    folder = RootFolder(path=str(path))
    session.add(folder)
    await session.commit()
    await session.refresh(folder)
    return folder


@router.delete("/rootfolders/{folder_id}", status_code=204)
async def delete_root_folder(folder_id: int, session: AsyncSession = Depends(get_session)):
    folder = await session.get(RootFolder, folder_id)
    if folder is None:
        raise HTTPException(404, "Root folder not found")
    in_use = (
        await session.execute(select(func.count(Series.id)).where(Series.root_folder_id == folder_id))
    ).scalar_one()
    if in_use:
        raise HTTPException(409, f"Root folder is used by {in_use} series")
    await session.delete(folder)
    await session.commit()
