"""Background tasks: series refresh (ComicVine metadata + issue list),
source linking, grabbing, DDL download processing, qBittorrent sync, and the
monitor loop."""

import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import config
from ..db import session_scope
from ..download.ddl import download_release
from ..download.qbittorrent import QbtClient
from ..library.importer import import_payload
from ..metadata.comicvine import derive_status, provider as comicvine
from ..models import (
    Download,
    DownloadKind,
    DownloadStatus,
    HistoryEvent,
    Issue,
    Series,
    SeriesSourceLink,
    SeriesStatus,
)
from ..sources import registry
from ..sources.base import SourceRelease
from ..util import normalize_title, strip_issue_suffix

log = logging.getLogger(__name__)

BTIH_RE = re.compile(r"btih:([0-9a-fA-F]{40}|[A-Z2-7]{32})")

# per-series cap on targeted per-issue searches in one monitor pass, so a
# freshly added long series doesn't hammer the source
ISSUE_SEARCH_CAP = 5


# ---------------------------------------------------------------- metadata

async def refresh_series_metadata(session: AsyncSession, series: Series) -> None:
    if not series.comicvine_id:
        return
    meta = await comicvine.get_series(str(series.comicvine_id))
    if meta is None:
        return
    series.title = meta.title
    series.alt_titles = "\n".join(meta.alt_titles)
    series.description = meta.description
    series.publisher = meta.publisher
    series.year = meta.year
    series.cover_url = meta.cover_url
    series.genres = ",".join(meta.genres)
    series.total_issues = meta.total_issues
    await session.commit()


async def update_issues(session: AsyncSession, series: Series) -> int:
    """Sync the issue list from ComicVine into the DB. Returns # new."""
    if not series.comicvine_id:
        return 0
    issue_meta = await comicvine.list_issues(str(series.comicvine_id))
    existing = {i.number: i for i in series.issues}
    added = 0
    for im in issue_meta:
        issue = existing.get(im.number)
        if issue is None:
            issue = Issue(
                comicvine_id=int(im.provider_id) if im.provider_id else None,
                number=im.number,
                title=im.title,
                released_at=im.released_at,
                monitored=series.monitored,
            )
            # append via the relationship so series.issues is current for
            # the scan/grab logic later in this same pass
            series.issues.append(issue)
            existing[im.number] = issue
            added += 1
        else:
            if not issue.title and im.title:
                issue.title = im.title
            if issue.released_at is None and im.released_at is not None:
                issue.released_at = im.released_at
            if issue.comicvine_id is None and im.provider_id:
                issue.comicvine_id = int(im.provider_id)
    # ComicVine volumes carry no status — derive it from issue recency
    series.status = SeriesStatus(derive_status(issue_meta, series.total_issues))
    await session.commit()
    return added


# ---------------------------------------------------------- source linking

def _titles_of(series: Series) -> list[str]:
    return [series.title, *[t for t in series.alt_titles.split("\n") if t]]


async def link_sources(session: AsyncSession, series: Series, values: dict[str, str]) -> None:
    """Link the series on every enabled DDL source it isn't linked to. For a
    DDL source the link is the search term whose posts belong to this series;
    when the source has no matching posts (yet) we still link by title so the
    monitor can start finding releases as soon as they appear."""
    linked = {sl.source_name for sl in series.source_links}
    titles = _titles_of(series)
    wanted = {nt for t in titles if (nt := normalize_title(t))}
    for src in registry.enabled_ddl_sources(values):
        if src.name in linked:
            continue
        match = None
        try:
            candidates = await src.search_series(series.title)
        except Exception as exc:
            log.warning("source %s search failed for %r: %s", src.name, series.title, exc)
            candidates = []
        for cand in candidates:
            if normalize_title(cand.title) in wanted:
                match = cand
                break
        external_id = match.external_id if match else series.title
        series.source_links.append(
            SeriesSourceLink(
                source_name=src.name,
                external_id=external_id,
                external_title=match.title if match else series.title,
                external_url=match.url if match else "",
            )
        )
        log.info("Linked %r to %s search term %r", series.title, src.name, external_id)
    await session.commit()


# --------------------------------------------------------------- refreshes

async def reconcile_downloaded_files(session: AsyncSession, series: Series) -> int:
    """Clear downloaded state for issues whose recorded media file is gone."""
    missing = 0
    for issue in series.issues:
        if not issue.downloaded:
            continue
        if not issue.file_path or not Path(issue.file_path).is_file():
            issue.downloaded = False
            issue.file_path = ""
            missing += 1
    if missing:
        await session.commit()
        log.info("Marked %d missing file(s) for %r", missing, series.title)
    return missing


async def scan_series_folder(session: AsyncSession, series: Series) -> None:
    """Adopt existing library folders for the series and mark issues that are
    already on disk as owned (so they aren't re-downloaded)."""
    from ..library.scanner import find_existing_folder, resolve_folders, scan_series

    if series.root_folder is None:
        return
    root = Path(series.root_folder.path)
    extras = [f.path for f in series.extra_folders]
    folders = resolve_folders(root, series, extras)
    if not folders[0].exists() and not extras:
        found = find_existing_folder(root, series)
        if found:
            series.folder_name = found
            folders = resolve_folders(root, series, extras)
    scan_series(series, list(series.issues), folders)
    await session.commit()


async def refresh_series_full(series_id: int, grab_missing: bool = False) -> None:
    async with session_scope() as session:
        series = await _load_series(session, series_id)
        if series is None:
            return
        values = await registry.apply_settings(session)
        try:
            await refresh_series_metadata(session, series)
            await update_issues(session, series)
        except Exception as exc:
            log.warning("metadata refresh failed for series %d: %s", series_id, exc)
        await link_sources(session, series, values)
        # adopt existing on-disk files before the monitor considers grabbing
        if values.get("library_scan_on_add", "true") == "true":
            try:
                await scan_series_folder(session, series)
            except Exception as exc:
                log.warning("library scan failed for series %d: %s", series_id, exc)
        await reconcile_downloaded_files(session, series)
        # monitored adds should immediately queue available issues instead of
        # waiting for the next scheduled monitor interval
        if grab_missing and series.monitored:
            try:
                await grab_missing_issues(session, series, values)
            except Exception as exc:
                log.warning("add-time grab failed for series %d: %s", series_id, exc)


async def scan_all_series() -> None:
    """Scan every series' folder to adopt on-disk files (background job)."""
    async with session_scope() as session:
        series_ids = [row[0] for row in (await session.execute(select(Series.id))).all()]
    for series_id in series_ids:
        async with session_scope() as session:
            series = await _load_series(session, series_id)
            if series is not None:
                try:
                    await scan_series_folder(session, series)
                except Exception as exc:
                    log.warning("library scan failed for series %d: %s", series_id, exc)


async def _load_series(session: AsyncSession, series_id: int) -> Series | None:
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(Series)
        .options(selectinload(Series.issues), selectinload(Series.source_links),
                 selectinload(Series.root_folder), selectinload(Series.extra_folders))
        .where(Series.id == series_id)
    )
    return result.scalar_one_or_none()


# ------------------------------------------------------------------- grabs

def _release_matches_series(release: SourceRelease, wanted_titles: set[str]) -> bool:
    return normalize_title(strip_issue_suffix(release.title)) in wanted_titles


async def find_release_for_issue(
    series: Series, issue: Issue, values: dict[str, str]
) -> tuple[str, SourceRelease] | None:
    """Best release for one issue: (source_name, release), by priority.
    Used by interactive search-driven grabs."""
    links = {sl.source_name: sl for sl in series.source_links}
    wanted_titles = {nt for t in _titles_of(series) if (nt := normalize_title(t))}
    for src in registry.enabled_ddl_sources(values):
        link = links.get(src.name)
        if link is None:
            continue
        wanted_titles.add(normalize_title(link.external_id))
        try:
            releases = await src.list_releases(link.external_id)
        except Exception as exc:
            log.warning("list_releases failed on %s: %s", src.name, exc)
            continue
        for r in releases:
            if r.issue_number == issue.number and _release_matches_series(r, wanted_titles):
                return src.name, r
    return None


async def enqueue_direct(
    session: AsyncSession, series: Series, issue: Issue | None,
    source_name: str, external_id: str, title: str = "",
) -> Download:
    dl = Download(
        series_id=series.id,
        issue_id=issue.id if issue else None,
        kind=DownloadKind.DIRECT,
        status=DownloadStatus.QUEUED,
        title=title or (f"{series.title} #{issue.number:g}" if issue else series.title),
        source_name=source_name,
        payload=external_id,
    )
    session.add(dl)
    session.add(HistoryEvent(
        series_id=series.id, issue_id=issue.id if issue else None, event="grabbed",
        source_name=source_name, detail=external_id,
    ))
    await session.commit()
    return dl


async def enqueue_torrent(
    session: AsyncSession, series: Series | None, magnet: str, title: str, values: dict[str, str],
) -> Download:
    m = BTIH_RE.search(magnet)
    torrent_hash = m.group(1).lower() if m else ""
    client = QbtClient(
        values["qbittorrent_url"], values["qbittorrent_username"], values["qbittorrent_password"]
    )
    try:
        category = values["qbittorrent_category"]
        # put grabs in a category subfolder so they stay organized and separate
        # from other qBittorrent downloads, regardless of its auto-management
        base = await client.default_save_path()
        save_path = f"{base.rstrip('/')}/{category}" if base else None
        await client.ensure_category(category, save_path)
        await client.add_magnet(magnet, category=category, save_path=save_path)
    finally:
        await client.close()
    dl = Download(
        series_id=series.id if series else None,
        kind=DownloadKind.TORRENT,
        status=DownloadStatus.DOWNLOADING,
        title=title,
        source_name="manual",
        payload=magnet,
        torrent_hash=torrent_hash,
    )
    session.add(dl)
    session.add(HistoryEvent(
        series_id=series.id if series else None, event="grabbed",
        source_name="manual", detail=title,
    ))
    await session.commit()
    return dl


# ------------------------------------------------------------ DDL downloads

def _staging_dir(values: dict[str, str]) -> Path:
    configured = values.get("ddl_directory", "").strip()
    return Path(configured) if configured else (config.data_dir / "ddl")


async def process_direct_queue() -> None:
    """Processes all queued DDL downloads, one release at a time."""
    while True:
        async with session_scope() as session:
            result = await session.execute(
                select(Download)
                .where(Download.kind == DownloadKind.DIRECT,
                       Download.status == DownloadStatus.QUEUED)
                .order_by(Download.id)
                .limit(1)
            )
            dl = result.scalar_one_or_none()
            if dl is None:
                return
            await _run_direct_download(session, dl)


async def _run_direct_download(session: AsyncSession, dl: Download) -> None:
    values = await registry.apply_settings(session)
    series = await _load_series(session, dl.series_id) if dl.series_id else None
    issue = await session.get(Issue, dl.issue_id) if dl.issue_id else None
    source = registry.DDL_SOURCES.get(dl.source_name)
    if series is None or source is None:
        dl.status = DownloadStatus.FAILED
        dl.error = "series/source no longer exists"
        await session.commit()
        return

    root = series.root_folder.path if series.root_folder else None
    if not root:
        dl.status = DownloadStatus.FAILED
        dl.error = "series has no root folder configured"
        await session.commit()
        return

    dl.status = DownloadStatus.DOWNLOADING
    await session.commit()

    def on_progress(done: int, total: int) -> None:
        if total > 0:
            dl.progress = min(done / total, 1.0)

    payload_dir: Path | None = None
    try:
        payload_dir = await download_release(
            source, dl.payload, _staging_dir(values), progress_cb=on_progress
        )
        dl.status = DownloadStatus.IMPORTING
        await session.commit()
        imported = import_payload(
            payload_dir, series, list(series.issues), Path(root),
            values["naming_template"], force_issue=issue, move=True,
        )
    except Exception as exc:
        log.exception("DDL download %d failed", dl.id)
        dl.status = DownloadStatus.FAILED
        dl.error = str(exc)[:500]
        session.add(HistoryEvent(
            series_id=series.id, issue_id=issue.id if issue else None, event="failed",
            source_name=dl.source_name, detail=dl.error,
        ))
        await session.commit()
        return
    finally:
        if payload_dir is not None:
            shutil.rmtree(payload_dir, ignore_errors=True)

    _mark_imported(series, imported)
    dl.status = DownloadStatus.DONE
    dl.progress = 1.0
    session.add(HistoryEvent(
        series_id=series.id, issue_id=issue.id if issue else None, event="imported",
        source_name=dl.source_name,
        detail=f"{len(imported)} file(s) from {dl.title}",
    ))
    await session.commit()


def _mark_imported(series: Series, imported: list[tuple[Path, Issue | None, int | None]]) -> None:
    for dest, issue, volume in imported:
        if issue is not None:
            issue.downloaded = True
            issue.file_path = str(dest)
        elif volume is not None:
            # a volume archive covers every issue assigned to that volume
            for i in series.issues:
                if i.volume == volume and not i.downloaded:
                    i.downloaded = True
                    i.file_path = str(dest)


# --------------------------------------------------------------- qbt sync

async def sync_qbittorrent() -> None:
    async with session_scope() as session:
        values = await registry.apply_settings(session)
        if values["qbittorrent_enabled"] != "true":
            return
        result = await session.execute(
            select(Download).where(
                Download.kind == DownloadKind.TORRENT,
                Download.status.in_([DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING,
                                     DownloadStatus.IMPORTING]),
            )
        )
        downloads = result.scalars().all()
        if not downloads:
            return
        client = QbtClient(
            values["qbittorrent_url"], values["qbittorrent_username"],
            values["qbittorrent_password"],
        )
        try:
            for dl in downloads:
                if not dl.torrent_hash:
                    continue
                torrent = await client.get_torrent(dl.torrent_hash)
                if torrent is None:
                    continue
                dl.progress = torrent.progress
                if torrent.is_complete and torrent.content_path:
                    dl.status = DownloadStatus.IMPORTING
                    await session.commit()
                    await _import_torrent(session, dl, Path(torrent.content_path), values)
                else:
                    await session.commit()
        finally:
            await client.close()


async def _import_torrent(
    session: AsyncSession, dl: Download, content_path: Path, values: dict[str, str]
) -> None:
    series = await _load_series(session, dl.series_id) if dl.series_id else None
    if series is None or not series.root_folder:
        dl.status = DownloadStatus.FAILED
        dl.error = "torrent has no linked series/root folder; import manually"
        await session.commit()
        return
    if not content_path.exists():
        # path as seen by qBittorrent may not be mounted here yet
        dl.error = f"content path not found: {content_path}"
        await session.commit()
        return
    try:
        imported = import_payload(
            content_path, series, list(series.issues), Path(series.root_folder.path),
            values["naming_template"],
        )
    except Exception as exc:
        log.exception("torrent import %d failed", dl.id)
        dl.status = DownloadStatus.FAILED
        dl.error = str(exc)[:500]
        await session.commit()
        return
    _mark_imported(series, imported)
    dl.status = DownloadStatus.DONE
    dl.progress = 1.0
    session.add(HistoryEvent(
        series_id=series.id, event="imported", source_name=dl.source_name or "torrent",
        detail=f"{len(imported)} file(s) from {dl.title}",
    ))
    await session.commit()


# ------------------------------------------------------------ monitor loop

def _releasable(issue: Issue) -> bool:
    """Only hunt issues that are actually out (or undated)."""
    if issue.released_at is None:
        return True
    return issue.released_at <= datetime.now(timezone.utc)


async def grab_missing_issues(
    session: AsyncSession, series: Series, values: dict[str, str]
) -> int:
    """Queue missing monitored, released issues from linked DDL sources.

    Shared by the scheduled monitor and the add-time refresh path, so a newly
    added monitored series starts pulling available issues as soon as its
    source links and issue list exist, instead of waiting for the next
    scheduled monitor interval. Returns the number of issues queued."""
    # active downloads for this series → don't double-grab
    result = await session.execute(
        select(Download.issue_id).where(
            Download.series_id == series.id,
            Download.status.in_([DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING,
                                 DownloadStatus.IMPORTING]),
        )
    )
    active = {row[0] for row in result.all()}
    if None in active:
        # a series-level download (a pack/TPB) is in flight — its issue
        # coverage is unknown until it imports, so grabbing per-issue now
        # would duplicate everything
        log.info("monitor: %r has a series-level download in flight; skipping grabs",
                 series.title)
        return 0
    wanted = [
        i for i in series.issues
        if i.monitored and not i.downloaded and i.id not in active
        and _releasable(i)
    ]
    if not wanted:
        return 0

    # a release that already failed from a source shouldn't be retried there
    # (dead links etc.)
    result = await session.execute(
        select(Download.issue_id, Download.source_name).where(
            Download.series_id == series.id,
            Download.status == DownloadStatus.FAILED,
            Download.issue_id.isnot(None),
        )
    )
    failed_pairs = {(iid, name) for iid, name in result.all()}

    links = {sl.source_name: sl for sl in series.source_links}
    wanted_titles = {nt for t in _titles_of(series) if (nt := normalize_title(t))}
    remaining = {i.number: i for i in wanted}
    queued = 0
    for src in registry.enabled_ddl_sources(values):
        if not remaining:
            break
        link = links.get(src.name)
        if link is None:
            continue
        wanted_titles.add(normalize_title(link.external_id))

        # one series-wide search, then match all wanted numbers
        try:
            releases = await src.list_releases(link.external_id)
        except Exception as exc:
            log.warning("monitor: %s list failed for %r: %s", src.name, series.title, exc)
            continue
        queued += await _grab_matches(session, series, src.name, releases,
                                      remaining, wanted_titles, failed_pairs)

        # targeted per-issue searches for stragglers (older issues that fell
        # off the recent-posts pages), capped per pass
        for issue in list(remaining.values())[:ISSUE_SEARCH_CAP]:
            if (issue.id, src.name) in failed_pairs:
                continue
            query = f"{link.external_id} {issue.number:g}"
            try:
                results = await src.search_releases(query)
            except Exception as exc:
                log.warning("monitor: %s search %r failed: %s", src.name, query, exc)
                continue
            queued += await _grab_matches(session, series, src.name, results,
                                          remaining, wanted_titles, failed_pairs)
    if queued:
        log.info("Queued %d missing issue(s) for %r", queued, series.title)
    return queued


async def monitor_all() -> None:
    """Refresh monitored series and grab missing monitored issues."""
    async with session_scope() as session:
        result = await session.execute(select(Series.id).where(Series.monitored == True))  # noqa: E712
        series_ids = [row[0] for row in result.all()]

    for series_id in series_ids:
        async with session_scope() as session:
            values = await registry.apply_settings(session)
            series = await _load_series(session, series_id)
            if series is None:
                continue
            try:
                await refresh_series_metadata(session, series)
                await update_issues(session, series)
            except Exception as exc:
                log.warning("monitor: metadata refresh failed for %r: %s", series.title, exc)
            await link_sources(session, series, values)
            await grab_missing_issues(session, series, values)


async def _grab_matches(
    session: AsyncSession,
    series: Series,
    source_name: str,
    releases: list[SourceRelease],
    remaining: dict[float, Issue],
    wanted_titles: set[str],
    failed_pairs: set[tuple[int, str]],
) -> int:
    """Enqueue every release that matches a still-wanted issue. Returns count."""
    queued = 0
    for r in releases:
        if r.issue_number is None:
            continue
        issue = remaining.get(r.issue_number)
        if issue is None or (issue.id, source_name) in failed_pairs:
            continue
        if not _release_matches_series(r, wanted_titles):
            continue
        remaining.pop(r.issue_number, None)
        await enqueue_direct(session, series, issue, source_name, r.external_id, r.title)
        queued += 1
    return queued
