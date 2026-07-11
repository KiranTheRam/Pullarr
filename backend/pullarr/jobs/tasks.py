"""Background tasks: series refresh (ComicVine metadata + issue list),
source linking, grabbing, DDL download processing, qBittorrent sync, and the
monitor loop."""

import logging
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import config
from ..db import session_scope
from ..download.ddl import DownloadCancelled, download_release
from ..download.qbittorrent import QbtClient
from ..library.importer import import_payload
from ..metadata.comicvine import derive_status, provider as comicvine
from ..metadata.metron import provider as metron
from ..models import (
    Download,
    DownloadKind,
    DownloadStatus,
    HistoryEvent,
    Issue,
    JobStatus,
    Series,
    SeriesSourceLink,
    SeriesStatus,
)
from .service import update_job
from ..sources import registry
from ..sources.base import SourceRelease
from ..util import (
    is_released,
    normalize_title,
    parse_issue_range,
    release_covers_issue,
    strip_issue_suffix,
)

log = logging.getLogger(__name__)

BTIH_RE = re.compile(r"btih:([0-9a-fA-F]{40}|[A-Z2-7]{32})")

# per-series cap on targeted per-issue searches in one monitor pass, so a
# freshly added long series doesn't hammer the source
ISSUE_SEARCH_CAP = 5

_cancelled_downloads: set[int] = set()


def cancel_downloads(ids: list[int]) -> None:
    _cancelled_downloads.update(ids)


def _is_cancelled(download_id: int) -> bool:
    return download_id in _cancelled_downloads


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
    series.sort_title = meta.title.lower()
    await session.commit()


def metadata_refresh_due(series: Series, now: datetime | None = None) -> bool:
    """Refresh active series daily and stable finished series monthly."""
    if series.metadata_refreshed_at is None:
        return True
    current = now or datetime.now(timezone.utc)
    refreshed = series.metadata_refreshed_at
    if refreshed.tzinfo is None:
        refreshed = refreshed.replace(tzinfo=timezone.utc)
    age = current - refreshed
    if series.status in (SeriesStatus.RELEASING, SeriesStatus.HIATUS, SeriesStatus.UNKNOWN):
        return age >= timedelta(hours=24)
    return age >= timedelta(days=30)


async def update_issues(session: AsyncSession, series: Series) -> int:
    """Sync the issue list from ComicVine into the DB. Returns # new."""
    if not series.comicvine_id:
        return 0
    issue_meta = await comicvine.list_issues(str(series.comicvine_id))
    by_comicvine = {i.comicvine_id: i for i in series.issues if i.comicvine_id is not None}
    by_display = {
        (i.display_number or f"{i.number:g}"): i
        for i in series.issues
        if i.display_number or i.comicvine_id is None
    }
    by_number: dict[float, list[Issue]] = {}
    for issue in series.issues:
        by_number.setdefault(issue.number, []).append(issue)
    added = 0
    for im in issue_meta:
        comicvine_id = int(im.provider_id) if im.provider_id else None
        display_number = im.display_number or f"{im.number:g}"
        issue = by_comicvine.get(comicvine_id) if comicvine_id is not None else None
        if issue is None:
            issue = by_display.get(display_number)
        if issue is None:
            # Last-resort compatibility for old rows without provider/display
            # identity. Only use the numeric key when it is unambiguous.
            candidates = by_number.get(im.number, [])
            if len(candidates) == 1 and not candidates[0].display_number:
                issue = candidates[0]
        if issue is None:
            issue = Issue(
                comicvine_id=comicvine_id,
                number=im.number,
                display_number=display_number,
                title=im.title,
                released_at=im.released_at,
                monitored=series.monitored,
            )
            # append via the relationship so series.issues is current for
            # the scan/grab logic later in this same pass
            series.issues.append(issue)
            if comicvine_id is not None:
                by_comicvine[comicvine_id] = issue
            by_display[display_number] = issue
            by_number.setdefault(im.number, []).append(issue)
            added += 1
        else:
            issue.number = im.number
            issue.display_number = display_number
            if im.title:
                issue.title = im.title
            if im.released_at is not None:
                issue.released_at = im.released_at
            if issue.comicvine_id is None and comicvine_id is not None:
                issue.comicvine_id = comicvine_id
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
    # Empty normalized strings would match everything; skip them.
    wanted = {nt for t in titles if (nt := normalize_title(t))}
    for src in registry.enabled_ddl_sources(values):
        if src.name in linked:
            continue
        match = None
        for query in titles[:4]:
            if not normalize_title(query):
                continue
            try:
                candidates = await src.search_series(query)
            except Exception as exc:
                log.warning("source %s search failed for %r: %s", src.name, query, exc)
                candidates = []
            for cand in candidates:
                cand_titles = {n for t in [cand.title, *cand.alt_titles] if (n := normalize_title(t))}
                if wanted & cand_titles:
                    match = cand
                    break
            if match is None and candidates and len(normalize_title(query)) >= 4:
                top = candidates[0]
                if normalize_title(top.title).startswith(normalize_title(query)[:12]):
                    match = top
            if match:
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


async def refresh_series_full(
    series_id: int, grab_missing: bool = False, only_monitored: bool = False,
    job_id: int | None = None,
) -> None:
    try:
        await _refresh_series_full_impl(
            series_id, grab_missing=grab_missing,
            only_monitored=only_monitored, job_id=job_id,
        )
    except Exception as exc:
        log.exception("series job failed for %d", series_id)
        async with session_scope() as session:
            await update_job(
                session, job_id, status=JobStatus.FAILED,
                phase="failed", error=str(exc),
            )


async def _refresh_series_full_impl(
    series_id: int, grab_missing: bool = False, only_monitored: bool = False,
    job_id: int | None = None,
) -> None:
    async with session_scope() as session:
        await update_job(session, job_id, status=JobStatus.RUNNING,
                         phase="metadata", progress=0.05)
        series = await _load_series(session, series_id)
        if series is None:
            await update_job(session, job_id, status=JobStatus.FAILED,
                             phase="failed", error="Series no longer exists")
            return
        values = await registry.apply_settings(session)
        warnings: list[str] = []
        try:
            await refresh_series_metadata(session, series)
            await update_issues(session, series)
            series.metadata_refreshed_at = datetime.now(timezone.utc)
            await session.commit()
            if values.get("metron_enabled") == "true" and metron.enabled:
                try:
                    await metron.enrich_series(series)
                    await session.commit()
                    limit = int(values.get("metron_issue_enrichment_limit", "5"))
                    candidates = sorted(
                        (i for i in series.issues if i.comicvine_id and i.metadata_refreshed_at is None),
                        key=lambda i: (not i.downloaded, -i.number),
                    )[:limit]
                    for index, candidate in enumerate(candidates, start=1):
                        await update_job(
                            session, job_id, phase="enriching metadata",
                            progress=0.1 + (0.2 * index / max(len(candidates), 1)),
                            detail=f"Enriched {index} of {len(candidates)} issue records",
                        )
                        try:
                            await metron.enrich_issue(candidate)
                            await session.commit()
                        except Exception as exc:
                            warnings.append(f"Metron issue {candidate.display_number}: {exc}")
                except Exception as exc:
                    warnings.append(f"Metron: {exc}")
                    log.warning("Metron enrichment failed for series %d: %s", series_id, exc)
        except Exception as exc:
            warnings.append(f"Metadata: {exc}")
            log.warning("metadata refresh failed for series %d: %s", series_id, exc)
        await update_job(session, job_id, phase="sources", progress=0.35)
        await link_sources(session, series, values)
        # adopt existing on-disk files before the monitor considers grabbing
        if values.get("library_scan_on_add", "true") == "true":
            await update_job(session, job_id, phase="scanning", progress=0.55)
            try:
                await scan_series_folder(session, series)
            except Exception as exc:
                warnings.append(f"Scan: {exc}")
                log.warning("library scan failed for series %d: %s", series_id, exc)
        await reconcile_downloaded_files(session, series)
        # Explicit one-time search, independent of the ongoing monitor toggle:
        # if the user asks to search now, grab released missing issues after
        # metadata/source linking and disk adoption have completed.
        queued_downloads: int | None = None
        if grab_missing:
            await update_job(session, job_id, phase="searching", progress=0.75)
            try:
                # an explicit one-time search: hunt every missing issue now
                queued_downloads = await grab_missing_issues(
                    session, series, values,
                    only_monitored=only_monitored, straggler_cap=None,
                )
            except Exception as exc:
                warnings.append(f"Search: {exc}")
                log.warning("search-time grab failed for series %d: %s", series_id, exc)
        if queued_downloads is not None:
            noun = "download" if queued_downloads == 1 else "downloads"
            warnings.insert(0, f"Queued {queued_downloads} {noun}")
        await update_job(
            session, job_id, status=JobStatus.DONE, phase="complete", progress=1.0,
            detail="; ".join(warnings) if warnings else "Completed successfully",
        )


async def scan_all_series(job_id: int | None = None) -> None:
    """Scan every series' folder to adopt on-disk files (background job)."""
    async with session_scope() as session:
        await update_job(session, job_id, status=JobStatus.RUNNING,
                         phase="scanning", progress=0.0)
        series_ids = [row[0] for row in (await session.execute(select(Series.id))).all()]
    warnings: list[str] = []
    for index, series_id in enumerate(series_ids, start=1):
        async with session_scope() as session:
            series = await _load_series(session, series_id)
            if series is not None:
                try:
                    await scan_series_folder(session, series)
                except Exception as exc:
                    warnings.append(f"Series {series_id}: {exc}")
                    log.warning("library scan failed for series %d: %s", series_id, exc)
            await update_job(
                session, job_id, phase="scanning",
                progress=index / max(len(series_ids), 1),
                detail=f"Scanned {index} of {len(series_ids)} series",
            )
    async with session_scope() as session:
        await update_job(
            session, job_id, status=JobStatus.DONE, phase="complete", progress=1.0,
            detail=("; ".join(warnings[:5]) if warnings else f"Scanned {len(series_ids)} series"),
        )


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
            if release_covers_issue(r, issue) and _release_matches_series(r, wanted_titles):
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


def _quarantine_payload(payload_dir: Path, values: dict[str, str], download_id: int) -> Path:
    quarantine = _staging_dir(values) / "quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    target = quarantine / f"download-{download_id}"
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    shutil.move(str(payload_dir), target)
    return target


def _download_error_code(exc: Exception) -> str:
    if isinstance(exc, DownloadCancelled):
        return "cancelled"
    if isinstance(exc, TimeoutError):
        return "timeout"
    name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    if "timeout" in name or "timeout" in message:
        return "timeout"
    if "incomplete" in message or "peer closed" in message or "protocol" in name:
        return "incomplete_transfer"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return f"http_{status}"
    if "no usable download link" in message or "no files downloaded" in message:
        return "no_working_link"
    if "collision" in message:
        return "import_collision"
    if "invalid" in message or "corrupt" in message:
        return "invalid_archive"
    return "download_error"


def _retryable_download_error(code: str) -> bool:
    if code in {"timeout", "incomplete_transfer", "download_error"}:
        return True
    if code.startswith("http_"):
        try:
            status = int(code.split("_", 1)[1])
        except ValueError:
            return False
        return status in {408, 425, 429} or status >= 500
    return False


async def _record_direct_failure(
    session: AsyncSession,
    dl: Download,
    series: Series,
    issue: Issue | None,
    exc: Exception,
    values: dict[str, str],
) -> None:
    code = _download_error_code(exc)
    message = str(exc)[:500] or exc.__class__.__name__
    max_attempts = int(values.get("download_retry_attempts", "4")) + 1
    retry = _retryable_download_error(code) and dl.attempt_count < max_attempts
    dl.error = message
    dl.error_code = code
    if retry:
        delay_minutes = min(2 ** max(dl.attempt_count - 1, 0), 60)
        dl.status = DownloadStatus.QUEUED
        dl.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        event = "retrying"
        detail = f"{message} — retry {dl.attempt_count}/{max_attempts - 1} in {delay_minutes}m"
    else:
        dl.status = DownloadStatus.FAILED
        dl.next_retry_at = None
        event = "failed"
        detail = message
    session.add(HistoryEvent(
        series_id=series.id,
        issue_id=issue.id if issue else None,
        event=event,
        source_name=dl.source_name,
        detail=detail,
    ))
    await session.commit()


async def process_direct_queue() -> None:
    """Processes all queued DDL downloads, one release at a time."""
    while True:
        async with session_scope() as session:
            result = await session.execute(
                select(Download)
                .where(Download.kind == DownloadKind.DIRECT,
                       Download.status == DownloadStatus.QUEUED,
                       or_(Download.next_retry_at.is_(None),
                           Download.next_retry_at <= datetime.now(timezone.utc)))
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
        dl.error_code = "missing_series_or_source"
        await session.commit()
        return

    root = series.root_folder.path if series.root_folder else None
    if not root:
        dl.status = DownloadStatus.FAILED
        dl.error = "series has no root folder configured"
        dl.error_code = "missing_root_folder"
        await session.commit()
        return

    dl.status = DownloadStatus.DOWNLOADING
    dl.attempt_count += 1
    dl.progress = 0.0
    dl.next_retry_at = None
    dl.error = ""
    dl.error_code = ""
    await session.commit()

    last_progress_commit = 0.0

    async def on_progress(done: int, total: int) -> None:
        nonlocal last_progress_commit
        if total > 0:
            dl.progress = min(done / total, 1.0)
        now = time.monotonic()
        if now - last_progress_commit >= 1.0 or dl.progress >= 1.0:
            last_progress_commit = now
            await session.commit()

    def should_cancel() -> bool:
        return _is_cancelled(dl.id)

    payload_dir: Path | None = None
    try:
        payload_dir = await download_release(
            source, dl.payload, _staging_dir(values),
            progress_cb=on_progress, cancel_cb=should_cancel,
        )
        await session.refresh(dl)
        if dl.status == DownloadStatus.FAILED or _is_cancelled(dl.id):
            dl.status = DownloadStatus.FAILED
            dl.error = dl.error or "removed by user"
            await session.commit()
            return
        dl.status = DownloadStatus.IMPORTING
        await session.commit()
        if issue is not None and values.get("metron_enabled") == "true" and metron.enabled:
            try:
                if await metron.enrich_issue(issue):
                    await session.commit()
            except Exception as exc:
                log.warning("Metron issue enrichment failed for %s: %s", issue.id, exc)
        imported = import_payload(
            payload_dir, series, list(series.issues), Path(root),
            values["naming_template"], force_issue=issue, move=True,
        )
    except DownloadCancelled as exc:
        await _record_direct_failure(session, dl, series, issue, exc, values)
        return
    except Exception as exc:
        log.exception("DDL download %d failed", dl.id)
        code = _download_error_code(exc)
        if payload_dir is not None and code in {"import_collision", "invalid_archive"}:
            try:
                kept = _quarantine_payload(payload_dir, values, dl.id)
                payload_dir = None
                exc = RuntimeError(f"{exc}; downloaded payload kept at {kept}")
            except OSError as quarantine_error:
                log.warning("could not quarantine failed payload: %s", quarantine_error)
        await _record_direct_failure(session, dl, series, issue, exc, values)
        return
    finally:
        _cancelled_downloads.discard(dl.id)
        if payload_dir is not None:
            shutil.rmtree(payload_dir, ignore_errors=True)

    covered_count = _mark_imported(series, imported)
    needs_attention = covered_count == 0
    dl.status = DownloadStatus.NEEDS_ATTENTION if needs_attention else DownloadStatus.DONE
    dl.progress = 1.0
    dl.next_retry_at = None
    dl.error = ""
    dl.error_code = ""
    session.add(HistoryEvent(
        series_id=series.id, issue_id=issue.id if issue else None,
        event="needs_attention" if needs_attention else "imported",
        source_name=dl.source_name,
        detail=(f"{len(imported)} file(s) need manual matching from {dl.title}"
                if needs_attention else f"{len(imported)} file(s) from {dl.title}"),
    ))
    await session.commit()


def _mark_imported(series: Series, imported: list) -> int:
    marked: set[int] = set()
    for item in imported:
        covered = list(item.covered)
        if not covered and item.volume is not None:
            # a volume archive covers every issue assigned to that volume
            covered = [i for i in series.issues if i.volume == item.volume]
        for issue in covered:
            issue.downloaded = True
            issue.file_path = str(item.dest)
            marked.add(issue.id)
    return len(marked)


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
    covered_count = _mark_imported(series, imported)
    dl.status = DownloadStatus.DONE if covered_count else DownloadStatus.NEEDS_ATTENTION
    dl.progress = 1.0
    session.add(HistoryEvent(
        series_id=series.id,
        event="imported" if covered_count else "needs_attention",
        source_name=dl.source_name or "torrent",
        detail=(f"{len(imported)} file(s) from {dl.title}"
                if covered_count else f"{len(imported)} file(s) need manual matching from {dl.title}"),
    ))
    await session.commit()


# ------------------------------------------------------------ monitor loop

def _releasable(issue: Issue) -> bool:
    """Only hunt issues that are actually out (or undated)."""
    return is_released(issue.released_at)


def _download_covered_issue_ids(download: Download, issues: list[Issue]) -> set[int] | None:
    """Issue ids reserved by an active download.

    None means a series-level pack is in flight and coverage is unknown.
    """
    if download.issue_id is None:
        return None
    covered = {download.issue_id}
    issue_range = parse_issue_range(download.title)
    if issue_range is not None:
        lo, hi = issue_range
        covered.update(issue.id for issue in issues if lo <= issue.number <= hi)
    return covered


async def grab_missing_issues(
    session: AsyncSession, series: Series, values: dict[str, str],
    only_monitored: bool = True,
    straggler_cap: int | None = ISSUE_SEARCH_CAP,
) -> int:
    """Queue missing released issues from linked DDL sources.

    Shared by the scheduled monitor and the add-time refresh path, so a newly
    added monitored series starts pulling available issues as soon as its
    source links and issue list exist when the user requests a one-time
    search. The scheduled monitor keeps `only_monitored=True`; add-time
    search uses `False` because it is an explicit user action.

    `straggler_cap` bounds the targeted per-issue searches in one pass; the
    recurring monitor keeps the default so its hourly footprint stays small,
    while explicit one-time searches pass None to hunt every missing issue
    (still paced by the source's rate limiter)."""
    # active downloads for this series → don't double-grab
    result = await session.execute(
        select(Download).where(
            Download.series_id == series.id,
            Download.status.in_([DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING,
                                 DownloadStatus.IMPORTING]),
        )
    )
    active: set[int] = set()
    for dl in result.scalars().all():
        covered = _download_covered_issue_ids(dl, list(series.issues))
        if covered is None:
            # a series-level download (a pack/TPB) is in flight — its issue
            # coverage is unknown until it imports, so grabbing per-issue now
            # would duplicate everything
            log.info("monitor: %r has a series-level download in flight; skipping grabs",
                     series.title)
            return 0
        active.update(covered)
    wanted = [
        i for i in series.issues
        if (i.monitored or not only_monitored) and not i.downloaded and i.id not in active
        and _releasable(i)
    ]
    if not wanted:
        return 0

    # Explicit blocklisting suppresses a release indefinitely. Exhausted
    # failures get a 24-hour cooldown so an hourly monitor does not thrash a
    # dead host, but can recover later without database surgery.
    result = await session.execute(
        select(Download).where(
            Download.series_id == series.id,
            Download.status == DownloadStatus.FAILED,
            Download.issue_id.isnot(None),
        )
    )
    failed_downloads = result.scalars().all()
    cooldown = datetime.now(timezone.utc) - timedelta(hours=24)
    suppressed = [
        dl for dl in failed_downloads
        if dl.blocked or (dl.updated_at is not None and dl.updated_at >= cooldown)
    ]
    failed_pairs = {(dl.issue_id, dl.source_name) for dl in suppressed if dl.issue_id is not None}
    failed_releases = {(dl.source_name, dl.payload) for dl in suppressed if dl.payload}

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
                                      remaining, wanted_titles, failed_pairs,
                                      failed_releases)

        # targeted per-issue searches for stragglers (older issues that fell
        # off the recent-posts pages), capped per pass
        for issue in list(remaining.values())[:straggler_cap]:
            if (issue.id, src.name) in failed_pairs:
                continue
            query = f"{link.external_id} {issue.display_number or f'{issue.number:g}'}"
            try:
                results = await src.search_releases(query)
            except Exception as exc:
                log.warning("monitor: %s search %r failed: %s", src.name, query, exc)
                continue
            queued += await _grab_matches(session, series, src.name, results,
                                          remaining, wanted_titles, failed_pairs,
                                          failed_releases)
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
            if metadata_refresh_due(series):
                try:
                    await refresh_series_metadata(session, series)
                    await update_issues(session, series)
                    series.metadata_refreshed_at = datetime.now(timezone.utc)
                    await session.commit()
                    if values.get("metron_enabled") == "true" and metron.enabled:
                        await metron.enrich_series(series)
                        await session.commit()
                except Exception as exc:
                    log.warning("monitor: metadata refresh failed for %r: %s", series.title, exc)
            await link_sources(session, series, values)
            try:
                await scan_series_folder(session, series)
            except Exception as exc:
                log.warning("library scan failed for series %d: %s", series_id, exc)
            await grab_missing_issues(session, series, values)


async def _grab_matches(
    session: AsyncSession,
    series: Series,
    source_name: str,
    releases: list[SourceRelease],
    remaining: dict[float, Issue],
    wanted_titles: set[str],
    failed_pairs: set[tuple[int, str]],
    failed_releases: set[tuple[str, str]] | None = None,
) -> int:
    """Enqueue every release that matches a still-wanted issue. A multi-issue
    bundle ("#1-3") is grabbed once and covers all wanted issues in its span.
    Returns the number of downloads queued."""
    queued = 0
    failed_releases = failed_releases or set()
    for r in releases:
        if (source_name, r.external_id) in failed_releases:
            continue
        if not _release_matches_series(r, wanted_titles):
            continue
        # issues this release would satisfy: a single number, a whole span,
        # or a variant point issue matched by display number
        covered = [
            i for i in remaining.values()
            if (i.id, source_name) not in failed_pairs and release_covers_issue(r, i)
        ]
        if not covered:
            continue
        anchor = min(covered, key=lambda i: i.number)
        for issue in covered:
            remaining.pop(issue.number, None)
        # one download, tied to the lowest covered issue; the importer marks
        # every issue the downloaded file actually spans
        await enqueue_direct(session, series, anchor, source_name, r.external_id, r.title)
        queued += 1
    return queued
