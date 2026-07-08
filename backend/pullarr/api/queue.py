import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..download.qbittorrent import QbtClient
from ..jobs.tasks import cancel_downloads, enqueue_direct, enqueue_torrent
from ..models import Download, DownloadKind, DownloadStatus, HistoryEvent, Issue, Series
from ..schemas import GrabIn, HistoryOut, QueueItemOut, QueueRemoveIn, QueueRemoveOut
from ..sources import registry

log = logging.getLogger(__name__)

router = APIRouter(tags=["queue"])

ACTIVE = [DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING, DownloadStatus.IMPORTING]


@router.get("/queue", response_model=list[QueueItemOut])
async def get_queue(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Download, Series.title)
        .outerjoin(Series, Download.series_id == Series.id)
        .where(Download.status.in_(ACTIVE))
        .order_by(Download.id)
    )
    items = []
    for dl, series_title in result.all():
        out = QueueItemOut.model_validate(dl)
        out.series_title = series_title or ""
        items.append(out)
    return items


async def _remove_downloads(session: AsyncSession, ids: list[int]) -> int:
    """Mark downloads as removed and stop matching qBittorrent transfers."""
    result = await session.execute(select(Download).where(Download.id.in_(ids)))
    downloads = result.scalars().all()
    hashes = [
        dl.torrent_hash for dl in downloads
        if dl.kind == DownloadKind.TORRENT and dl.torrent_hash and dl.status in ACTIVE
    ]
    for dl in downloads:
        dl.status = DownloadStatus.FAILED
        dl.error = "removed by user"
    await session.commit()
    cancel_downloads([
        dl.id for dl in downloads
        if dl.kind == DownloadKind.DIRECT and dl.status == DownloadStatus.FAILED
    ])
    if hashes:
        values = await registry.apply_settings(session)
        if values["qbittorrent_enabled"] == "true":
            client = QbtClient(
                values["qbittorrent_url"], values["qbittorrent_username"],
                values["qbittorrent_password"],
            )
            try:
                await client.delete_torrents(hashes)
            except Exception as exc:
                log.warning("failed to delete %d torrent(s) from qBittorrent: %s",
                            len(hashes), exc)
            finally:
                await client.close()
    return len(downloads)


@router.delete("/queue/{download_id}", status_code=204)
async def remove_from_queue(download_id: int, session: AsyncSession = Depends(get_session)):
    removed = await _remove_downloads(session, [download_id])
    if removed == 0:
        raise HTTPException(404, "Download not found")


@router.post("/queue/remove", response_model=QueueRemoveOut)
async def remove_many_from_queue(
    body: QueueRemoveIn, session: AsyncSession = Depends(get_session)
):
    if not body.ids:
        raise HTTPException(422, "No download ids given")
    return QueueRemoveOut(removed=await _remove_downloads(session, body.ids))


@router.post("/queue/grab", response_model=QueueItemOut, status_code=201)
async def grab(body: GrabIn, session: AsyncSession = Depends(get_session)):
    values = await registry.apply_settings(session)

    if body.source_name and body.external_id:
        issue = None
        if body.issue_id is not None:
            issue = await session.get(Issue, body.issue_id)
            if issue is None:
                raise HTTPException(404, "Issue not found")
            series = await session.get(Series, issue.series_id)
        elif body.series_id is not None:
            series = await session.get(Series, body.series_id)
        else:
            raise HTTPException(422, "issue_id or series_id required for a DDL grab")
        if series is None:
            raise HTTPException(404, "Series not found")
        dl = await enqueue_direct(
            session, series, issue, body.source_name, body.external_id,
            body.title or "",
        )
    elif body.magnet:
        if values["qbittorrent_enabled"] != "true":
            raise HTTPException(400, "qBittorrent is not enabled in settings")
        series = await session.get(Series, body.series_id) if body.series_id else None
        try:
            dl = await enqueue_torrent(
                session, series, body.magnet, body.title or "manual torrent", values
            )
        except Exception as exc:
            raise HTTPException(502, f"qBittorrent error: {exc}") from exc
    else:
        raise HTTPException(422, "Provide source_name+external_id or magnet")

    out = QueueItemOut.model_validate(dl)
    out.series_title = series.title if series else ""
    return out


@router.get("/history", response_model=list[HistoryOut])
async def get_history(limit: int = 100, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(HistoryEvent, Series.title)
        .outerjoin(Series, HistoryEvent.series_id == Series.id)
        .order_by(HistoryEvent.id.desc())
        .limit(limit)
    )
    items = []
    for ev, series_title in result.all():
        out = HistoryOut.model_validate(ev)
        out.series_title = series_title or ""
        items.append(out)
    return items
