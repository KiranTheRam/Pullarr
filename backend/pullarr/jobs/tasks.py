"""Background tasks: series refresh (ComicVine metadata + issue list),
source linking, grabbing, DDL download processing, qBittorrent sync, and the
monitor loop."""

import base64
import binascii
import logging
import re
import shutil
import time
from threading import Event
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from .. import kavita, notifications
from ..config import config
from ..db import session_scope
from ..download.ddl import DownloadCancelled, download_release
from ..download.qbittorrent import QbtClient, QbtError
from ..library.importer import import_payload
from ..library.scanner import LibraryUnavailableError, require_root_available, root_unavailable
from ..library.work import run_library_work
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
from ..monitoring import issue_wanted, resolve_initial_future
from .service import update_job
from ..sources import registry
from ..sources.base import SourceRelease
from ..util import (
    error_chain,
    is_transient_error,
    is_released,
    normalize_title,
    parse_issue_range,
    release_covers_issue,
    strip_issue_suffix,
    standalone_issue_for_release,
)

log = logging.getLogger(__name__)

BTIH_RE = re.compile(r"btih:([0-9a-fA-F]{40}|[A-Z2-7]{32})")

# per-series cap on targeted per-issue searches in one monitor pass, so a
# freshly added long series doesn't hammer the source
ISSUE_SEARCH_CAP = 5

_active_downloads: dict[int, Event] = {}
_importing_downloads: set[int] = set()


FAILED_GRAB_RETRY_AFTER = timedelta(hours=24)
TRANSIENT_GRAB_RETRY_AFTER = timedelta(hours=2)
TRANSIENT_GRAB_ATTEMPTS = 3
SOURCE_COOLDOWN_SECONDS = 10 * 60
NOT_SOURCE_FAILURES = {
    "library_unavailable", "missing_root_folder", "missing_series_or_source",
    "source_disabled", "source_configuration", "filesystem_error", "import_collision", "import_error",
    "cancelled", "interrupted",
}
_source_cooldown_until: dict[str, float] = {}
_link_retry_at: dict[tuple[int, str], float] = {}


def source_cooldown_remaining(name: str) -> float:
    remaining = _source_cooldown_until.get(name, 0) - time.monotonic()
    if remaining <= 0:
        _source_cooldown_until.pop(name, None)
        return 0
    log.debug("source %s skipped during outage cooldown", name)
    return remaining


def _source_failed(name: str, exc: Exception) -> None:
    if not is_transient_error(exc):
        log.warning("source %s failed (%s); check its settings and response", name, type(exc).__name__)
        return
    if not source_cooldown_remaining(name):
        _source_cooldown_until[name] = time.monotonic() + SOURCE_COOLDOWN_SECONDS
        # Exception text can include authenticated indexer URLs.
        log.warning("source %s failed (%s); skipping it for 10 minutes", name, type(exc).__name__)


def failed_release_blocks(dl: Download, now: datetime) -> bool:
    """User blocks last indefinitely; local failures never penalize a source.

    Unclassified existing failures retain the previous 24-hour behavior.
    Failed entries remain available through the failed-queue API indefinitely.
    """
    if dl.blocked:
        return True
    if dl.error_code in NOT_SOURCE_FAILURES or dl.error == "removed by user":
        return False
    window = TRANSIENT_GRAB_RETRY_AFTER if dl.transient_error else FAILED_GRAB_RETRY_AFTER
    return dl.updated_at is not None and dl.updated_at >= now - window


def cancel_downloads(ids: list[int]) -> None:
    for download_id in ids:
        if token := _active_downloads.get(download_id):
            token.set()


def download_is_active(download_id: int) -> bool:
    return download_id in _active_downloads


def download_is_importing(download_id: int) -> bool:
    return download_id in _importing_downloads


async def _import_download_payload(download_id: int, *args, **kwargs):
    _importing_downloads.add(download_id)
    try:
        return await run_library_work(import_payload, *args, **kwargs)
    finally:
        _importing_downloads.discard(download_id)


def _is_cancelled(download_id: int) -> bool:
    token = _active_downloads.get(download_id)
    return token is not None and token.is_set()


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
                monitored=issue_wanted(series, im.number),
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
    resolve_initial_future(series, series.issues)
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
        key = (series.id, src.name)
        if (src.name in linked or source_cooldown_remaining(src.name)
                or _link_retry_at.get(key, 0) > time.monotonic()):
            continue
        match = None
        search_failed = False
        for query in titles[:4]:
            if not normalize_title(query):
                continue
            try:
                candidates = await src.search_series(query)
            except Exception as exc:
                _source_failed(src.name, exc)
                _link_retry_at[key] = time.monotonic() + (
                    SOURCE_COOLDOWN_SECONDS if is_transient_error(exc) else 24 * 60 * 60
                )
                log.debug("source %s linking failed (%s)", src.name, type(exc).__name__)
                search_failed = True
                break
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
        if search_failed:
            continue
        _link_retry_at.pop(key, None)
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

async def root_expects_content(session: AsyncSession, series: Series) -> bool:
    """A new series shares the mount assumptions of all its root's neighbours."""
    if any(issue.downloaded for issue in series.issues):
        return True
    if series.root_folder is None:
        return False
    owned = await session.execute(
        select(Issue.id).join(Series, Issue.series_id == Series.id).where(
            Series.root_folder_id == series.root_folder.id,
            Issue.downloaded == True,  # noqa: E712
        ).limit(1)
    )
    return owned.first() is not None


async def library_root_unavailable(session: AsyncSession, series: Series) -> str:
    if series.root_folder is None:
        return ""
    return await run_in_threadpool(
        root_unavailable, Path(series.root_folder.path),
        await root_expects_content(session, series),
    )


async def reconcile_downloaded_files(session: AsyncSession, series: Series) -> int:
    """Clear downloaded state for issues whose recorded media file is gone."""
    reason = await library_root_unavailable(session, series)
    if reason:
        log.warning("Not checking files of %r: %s", series.title, reason)
        return 0
    # Defer mutation until the final root check, so losing a share during the
    # file checks cannot clear only part of the series.
    missing_issues = [issue for issue in series.issues if issue.downloaded and
                      (not issue.file_path or not Path(issue.file_path).is_file())]
    if await library_root_unavailable(session, series):
        return 0
    missing = 0
    for issue in missing_issues:
        issue.downloaded = False
        issue.file_path = ""
        issue.file_source = ""
        issue.file_download_id = None
        missing += 1
    if missing:
        await session.commit()
        log.info("Marked %d missing file(s) for %r", missing, series.title)
    return missing


async def scan_series_folder(session: AsyncSession, series: Series) -> None:
    """Adopt existing library folders for the series and mark issues that are
    already on disk as owned (so they aren't re-downloaded)."""
    from ..library.scanner import find_existing_folder, resolve_folders, scan_series_async

    if series.root_folder is None:
        return
    root = Path(series.root_folder.path)
    extras = [f.path for f in series.extra_folders]
    folders = resolve_folders(root, series, extras)
    if not folders[0].exists() and not extras:
        found = await run_in_threadpool(find_existing_folder, root, series)
        if found:
            series.folder_name = found
            folders = resolve_folders(root, series, extras)
    result = await scan_series_async(
        series, list(series.issues), folders, root=root,
        expect_content=await root_expects_content(session, series),
    )
    if result.root_unavailable:
        raise LibraryUnavailableError(result.root_unavailable)
    await session.commit()


async def refresh_series_full(
    series_id: int, grab_missing: bool = False, only_monitored: bool = False,
    job_id: int | None = None, force_scan: bool = False,
) -> None:
    try:
        await _refresh_series_full_impl(
            series_id, grab_missing=grab_missing,
            only_monitored=only_monitored, job_id=job_id, force_scan=force_scan,
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
    job_id: int | None = None, force_scan: bool = False,
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
        if force_scan or values.get("library_scan_on_add", "true") == "true":
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


def _notify_kavita(values: dict[str, str], series: Series) -> None:
    """Ask Kavita to scan the library this series' files live in.

    Kavita names a series from what its parser reads on disk, usually the
    folder name. Comic titles repeat across reboots, so the year-qualified
    names go first — matching "Batman (2016)" avoids having to choose between
    every Batman in the library.
    """
    if series.root_folder is None:
        return
    from ..library.naming import series_folder

    titles = [
        series.folder_name,
        series_folder(series.title, series.year),
        series.title,
        *(t.strip() for t in (series.alt_titles or "").split("\n")),
    ]
    seen: set[str] = set()
    ordered = [t for t in titles if t and not (t in seen or seen.add(t))]
    kavita.notify_import(values, series.root_folder_id, series.root_folder.path, ordered)


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
        if source_cooldown_remaining(src.name):
            continue
        try:
            releases = await src.list_releases(link.external_id)
        except Exception as exc:
            _source_failed(src.name, exc)
            log.debug("list_releases failed on %s (%s)", src.name, type(exc).__name__)
            continue
        for r in releases:
            if release_covers_issue(r, issue, series) and _release_matches_series(r, wanted_titles):
                return src.name, r
    return None


async def enqueue_direct(
    session: AsyncSession, series: Series, issue: Issue | None,
    source_name: str, external_id: str, title: str = "",
) -> Download:
    if issue is None and title:
        issue = standalone_issue_for_release(series, title)
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


def magnet_btih_hex(magnet: str) -> str:
    """The magnet's info-hash in the lowercase hex form qBittorrent reports.
    Base32 hashes (older magnet links) are converted — matching against
    qBittorrent by their raw form would never succeed."""
    m = BTIH_RE.search(magnet)
    if not m:
        return ""
    raw = m.group(1)
    if len(raw) == 40:
        return raw.lower()
    try:
        return base64.b32decode(raw).hex()
    except (binascii.Error, ValueError):
        return ""


async def submit_torrent(
    magnet: str, values: dict[str, str], *, keep_existing: bool = False,
) -> str:
    """Submit a magnet to qBittorrent and return its info hash.

    Kept separate from the database row creation so a failed torrent can be
    submitted again without replacing its Activity entry. With
    ``keep_existing``, a torrent qBittorrent still has (e.g. one whose import
    failed) is tracked again instead of re-added, which qBittorrent rejects.
    """
    torrent_data = None
    if magnet.startswith(("http://", "https://")):
        from ..download.torrent import fetch_torrent
        magnet, torrent_hash, torrent_data = await fetch_torrent(magnet)
    else:
        torrent_hash = magnet_btih_hex(magnet)
    if not torrent_hash:
        raise ValueError("Torrent must include a valid btih info hash")
    client = QbtClient(
        values["qbittorrent_url"], values["qbittorrent_username"], values["qbittorrent_password"]
    )
    try:
        if keep_existing and await client.get_torrent(torrent_hash):
            return torrent_hash
        category = values["qbittorrent_category"]
        # put grabs in a category subfolder so they stay organized and separate
        # from other qBittorrent downloads, regardless of its auto-management
        base = await client.default_save_path()
        save_path = f"{base.rstrip('/')}/{category}" if base else None
        await client.ensure_category(category, save_path)
        if torrent_data is not None:
            await client.add_torrent(torrent_data, category=category, save_path=save_path)
        else:
            await client.add_magnet(magnet, category=category, save_path=save_path)
    finally:
        await client.close()
    return torrent_hash


async def enqueue_torrent(
    session: AsyncSession, series: Series | None, magnet: str, title: str, values: dict[str, str],
    source_name: str = "manual", issue: Issue | None = None,
) -> Download:
    torrent_hash = await submit_torrent(magnet, values)
    dl = Download(
        series_id=series.id if series else None,
        issue_id=issue.id if issue else None,
        kind=DownloadKind.TORRENT,
        status=DownloadStatus.DOWNLOADING,
        title=title,
        source_name=source_name,
        payload=magnet,
        torrent_hash=torrent_hash,
    )
    session.add(dl)
    session.add(HistoryEvent(
        series_id=series.id if series else None, event="grabbed",
        source_name=source_name, detail=title,
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
    causes = list(error_chain(exc))
    # Local failures take precedence over the transfer they interrupted.
    if any(isinstance(c, LibraryUnavailableError) for c in causes):
        return "library_unavailable"
    if any(isinstance(c, DownloadCancelled) for c in causes):
        return "cancelled"
    if any(isinstance(c, (httpx.InvalidURL, httpx.UnsupportedProtocol, httpx.LocalProtocolError)) for c in causes):
        return "source_configuration"
    # An HTTP transport can wrap a raw socket/DNS OSError. Only filesystem
    # errors outside that transport wrapper belong to the local filesystem.
    for cause in causes:
        if isinstance(cause, (httpx.TransportError, ConnectionError, TimeoutError)):
            break
        if isinstance(cause, OSError):
            return "filesystem_error"
    if is_transient_error(exc):
        for cause in causes:
            status = getattr(getattr(cause, "response", None), "status_code", None)
            if status:
                return f"http_{status}"
            if isinstance(cause, TimeoutError) or "timeout" in type(cause).__name__.lower():
                return "timeout"
        return "incomplete_transfer" if any("protocol" in type(c).__name__.lower() for c in causes) else "network_error"
    for cause in causes:
        status = getattr(getattr(cause, "response", None), "status_code", None)
        if status:
            return f"http_{status}"
    message = str(exc).lower()
    if "peer closed" in message or "incomplete transfer" in message:
        return "incomplete_transfer"
    if "no usable download link" in message or "no files downloaded" in message:
        return "no_working_link"
    if "collision" in message:
        return "import_collision"
    if message.startswith("invalid series folder"):
        return "import_error"
    if "invalid" in message or "corrupt" in message:
        return "invalid_archive"
    return "download_error"


def _retryable_download_error(code: str) -> bool:
    if code in {"timeout", "incomplete_transfer", "network_error"}:
        return True
    if code.startswith("http_"):
        try:
            status = int(code.split("_", 1)[1])
        except ValueError:
            return False
        return status in {408, 425, 429} or 500 <= status < 600
    return False


async def _record_direct_failure(
    session: AsyncSession,
    dl: Download,
    series: Series,
    issue: Issue | None,
    exc: Exception,
    values: dict[str, str],
    *, code_override: str | None = None,
) -> None:
    code = code_override or _download_error_code(exc)
    if dl.status == DownloadStatus.IMPORTING and code == "download_error":
        code = "import_error"
    transient = _retryable_download_error(code)
    if transient:
        _source_failed(dl.source_name, exc)
    message = str(exc)[:500] or exc.__class__.__name__
    max_attempts = int(values.get("download_retry_attempts", "4")) + 1
    retry = transient and dl.attempt_count < max_attempts
    dl.transient_error = transient
    dl.error = message
    dl.error_code = code
    if retry:
        delay_minutes = min(2 ** max(dl.attempt_count - 1, 0), 60)
        dl.status = DownloadStatus.QUEUED
        delay_seconds = max(delay_minutes * 60, source_cooldown_remaining(dl.source_name))
        dl.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        event = "retrying"
        detail = f"{message} — retry {dl.attempt_count}/{max_attempts - 1} in {delay_seconds / 60:.0f}m"
    else:
        dl.status = DownloadStatus.FAILED
        dl.next_retry_at = None
        if transient:
            earlier = await session.scalar(select(func.count()).select_from(Download).where(
                Download.id != dl.id,
                Download.series_id == dl.series_id,
                Download.source_name == dl.source_name,
                Download.payload == dl.payload,
                Download.status == DownloadStatus.FAILED,
                Download.transient_error == True,  # noqa: E712
                Download.updated_at >= datetime.now(timezone.utc) - FAILED_GRAB_RETRY_AFTER,
            ))
            dl.transient_error = earlier + 1 < TRANSIENT_GRAB_ATTEMPTS
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
    # A cancellation belongs to one active attempt, never to a queued retry.
    if download_is_active(dl.id):
        return
    _active_downloads[dl.id] = Event()
    try:
        await _run_direct_attempt(session, dl)
    finally:
        _active_downloads.pop(dl.id, None)


async def _run_direct_attempt(session: AsyncSession, dl: Download) -> None:
    values = await registry.apply_settings(session)
    await session.refresh(dl)
    if dl.status != DownloadStatus.QUEUED or _is_cancelled(dl.id):
        return
    series = await _load_series(session, dl.series_id) if dl.series_id else None
    issue = await session.get(Issue, dl.issue_id) if dl.issue_id else None
    source = registry.DDL_SOURCES.get(dl.source_name)
    if series is None or source is None:
        dl.status = DownloadStatus.FAILED
        dl.error = "series/source no longer exists"
        dl.error_code = "missing_series_or_source"
        await session.commit()
        return

    if values.get(f"source_{dl.source_name}_enabled") != "true":
        await _record_direct_failure(session, dl, series, issue,
            ValueError("source is disabled; enable it before retrying"), values,
            code_override="source_disabled")
        return
    root = series.root_folder.path if series.root_folder else None
    if not root:
        await _record_direct_failure(session, dl, series, issue,
            ValueError("series has no root folder configured"), values,
            code_override="missing_root_folder")
        return

    unavailable = await library_root_unavailable(session, series)
    if unavailable:
        await _record_direct_failure(
            session, dl, series, issue, LibraryUnavailableError(unavailable), values,
        )
        return
    if cooldown := source_cooldown_remaining(dl.source_name):
        dl.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=cooldown)
        await session.commit()
        return
    expect_content = await root_expects_content(session, series)

    def check_library() -> None:
        require_root_available(Path(root), expect_content)

    dl.status = DownloadStatus.DOWNLOADING
    dl.attempt_count += 1
    dl.progress = 0.0
    dl.next_retry_at = None
    dl.error = ""
    dl.error_code = ""
    dl.transient_error = False
    await session.commit()

    last_progress_commit = 0.0

    async def on_progress(done: int, total: int) -> None:
        nonlocal last_progress_commit
        if _is_cancelled(dl.id):
            raise DownloadCancelled("removed by user")
        if total > 0:
            dl.progress = min(done / total, 1.0)
        now = time.monotonic()
        if now - last_progress_commit >= 1.0 or dl.progress >= 1.0:
            last_progress_commit = now
            await session.commit()

    def should_cancel() -> bool:
        check_library()
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
        check_library()
        dl.status = DownloadStatus.IMPORTING
        await session.commit()
        if issue is not None and values.get("metron_enabled") == "true" and metron.enabled:
            try:
                if await metron.enrich_issue(issue):
                    await session.commit()
            except Exception as exc:
                log.warning("Metron issue enrichment failed for %s: %s", issue.id, exc)
        if _is_cancelled(dl.id):
            raise DownloadCancelled("removed by user")
        imported = await _import_download_payload(dl.id,
            payload_dir, series, list(series.issues), Path(root),
            values["naming_template"], force_issue=issue, move=True,
            check_library=check_library,
        )
    except DownloadCancelled as exc:
        await _record_direct_failure(session, dl, series, issue, exc, values)
        return
    except Exception as exc:
        log.exception("DDL download %d failed", dl.id)
        if isinstance(exc, OSError):
            reason = root_unavailable(Path(root), expect_content)
            if reason:
                exc = LibraryUnavailableError(reason)
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
        if payload_dir is not None:
            await run_in_threadpool(shutil.rmtree, payload_dir, ignore_errors=True)

    covered_count = _mark_imported(series, imported, dl)
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
    if not needs_attention:
        notifications.notify_import(values, series.id, f"{covered_count} issue(s) from {dl.title}")
        _notify_kavita(values, series)


def _mark_imported(series: Series, imported: list, download: Download | None = None) -> int:
    marked: set[int] = set()
    for item in imported:
        covered = list(item.covered)
        if not covered and item.volume is not None:
            # a volume archive covers every issue assigned to that volume
            covered = [i for i in series.issues if i.volume == item.volume]
        for issue in covered:
            was_same_file = issue.file_path == str(item.dest)
            issue.downloaded = True
            issue.file_path = str(item.dest)
            if item.status != "duplicate":
                issue.file_source = download.source_name if download else ""
                issue.file_download_id = download.id if download else None
            elif not was_same_file:
                # The existing file was not written by this download.
                issue.file_source = ""
                issue.file_download_id = None
            marked.add(issue.id)
    return len(marked)


# --------------------------------------------------------------- qbt sync

SYNCED_TORRENT_STATUSES = (
    DownloadStatus.QUEUED, DownloadStatus.DOWNLOADING, DownloadStatus.IMPORTING,
)


async def _update_if_still_synced(session: AsyncSession, dl: Download, **values) -> bool:
    """Commit a status transition only if the torrent is still in the queue.

    qBittorrent calls yield while the API can remove this download in another
    session. Check and write in one UPDATE so the stale ORM row cannot undo a
    removal that committed first.
    """
    result = await session.execute(
        update(Download)
        .where(Download.id == dl.id, Download.status.in_(SYNCED_TORRENT_STATUSES))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if result.rowcount != 1:
        return False
    # Keep subsequent ORM writes diffed against the committed transition.
    await session.refresh(dl, list(values))
    return True


def _torrent_files(save_path: str, names: list[str]) -> list[Path]:
    """Resolve qBittorrent's relative file names without escaping its save path."""
    if not names:
        raise ValueError("torrent has no files selected for import")
    if not save_path or not Path(save_path).is_absolute():
        raise ValueError("torrent has no absolute save path; cannot scope its files safely")
    root = Path(save_path).resolve()
    files = []
    for name in names:
        relative = PurePosixPath(name)
        if (not name or relative.is_absolute() or ".." in relative.parts
                or "\\" in name or PureWindowsPath(name).drive):
            raise ValueError("torrent file list contains an unsafe path")
        path = root.joinpath(*relative.parts).resolve()
        if not path.is_relative_to(root):
            raise ValueError("torrent file list escapes its save path")
        files.append(path)
    return files


async def sync_qbittorrent() -> None:
    async with session_scope() as session:
        values = await registry.apply_settings(session)
        if values["qbittorrent_enabled"] != "true":
            return
        result = await session.execute(
            select(Download).where(
                Download.kind == DownloadKind.TORRENT,
                Download.status.in_(SYNCED_TORRENT_STATUSES),
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
                # rows saved before base32 hashes were converted hold a hash
                # qBittorrent never reports; recover it from the magnet
                torrent_hash = magnet_btih_hex(dl.payload)
                if torrent_hash and torrent_hash != dl.torrent_hash:
                    dl.torrent_hash = torrent_hash
                if not dl.torrent_hash:
                    continue
                torrent = await client.get_torrent(dl.torrent_hash)
                if torrent is None:
                    continue
                dl.progress = torrent.progress
                if torrent.is_complete and torrent.content_path:
                    try:
                        files = _torrent_files(
                            torrent.save_path, await client.torrent_files(dl.torrent_hash),
                        )
                    except QbtError as exc:
                        await _update_if_still_synced(
                            session, dl, status=DownloadStatus.DOWNLOADING,
                            error=f"waiting for torrent file list: {exc}",
                        )
                        continue
                    except ValueError as exc:
                        await _update_if_still_synced(
                            session, dl, status=DownloadStatus.FAILED,
                            error=str(exc)[:500], error_code="invalid_payload",
                        )
                        continue
                    if not await _update_if_still_synced(
                        session, dl, status=DownloadStatus.IMPORTING,
                    ):
                        log.info("torrent download %d left the queue during sync; "
                                 "not importing it", dl.id)
                        continue
                    await _import_torrent(
                        session, dl, Path(torrent.content_path), values, files=files,
                    )
                else:
                    await session.commit()
        finally:
            await client.close()


async def _import_torrent(
    session: AsyncSession, dl: Download, content_path: Path, values: dict[str, str],
    *, files: list[Path] | None = None,
) -> None:
    series = await _load_series(session, dl.series_id) if dl.series_id else None
    if series is None or not series.root_folder:
        dl.status = DownloadStatus.FAILED
        dl.error = "torrent has no linked series/root folder; import manually"
        await session.commit()
        return
    unavailable = await library_root_unavailable(session, series)
    if unavailable:
        dl.status = DownloadStatus.DOWNLOADING
        dl.error = f"waiting for the library: {unavailable}"[:500]
        dl.error_code = "library_unavailable"
        await session.commit()
        return
    expect_content = await root_expects_content(session, series)
    if not content_path.exists():
        # path as seen by qBittorrent may not be mounted here yet
        dl.error = f"content path not found: {content_path}"
        await session.commit()
        return
    try:
        imported = await _import_download_payload(dl.id,
            content_path, series, list(series.issues), Path(series.root_folder.path),
            values["naming_template"],
            force_issue=next((i for i in series.issues if i.id == dl.issue_id), None),
            files=files,
            check_library=lambda: require_root_available(
                Path(series.root_folder.path), expect_content,
            ),
        )
    except LibraryUnavailableError as exc:
        dl.status = DownloadStatus.DOWNLOADING
        dl.error = f"waiting for the library: {exc}"[:500]
        dl.error_code = "library_unavailable"
        await session.commit()
        return
    except Exception as exc:
        reason = (root_unavailable(Path(series.root_folder.path), expect_content)
                  if isinstance(exc, OSError) else "")
        if reason:
            dl.status = DownloadStatus.DOWNLOADING
            dl.error = f"waiting for the library: {reason}"[:500]
            dl.error_code = "library_unavailable"
            await session.commit()
            return
        if isinstance(exc, FileNotFoundError):
            dl.status = DownloadStatus.DOWNLOADING
            dl.error = f"torrent content moved during import; retrying: {exc}"[:500]
            dl.error_code = "content_unavailable"
            await session.commit()
            return
        log.exception("torrent import %d failed", dl.id)
        dl.status = DownloadStatus.FAILED
        dl.error = str(exc)[:500]
        dl.error_code = _download_error_code(exc)
        await session.commit()
        return
    covered_count = _mark_imported(series, imported, dl)
    dl.status = DownloadStatus.DONE if covered_count else DownloadStatus.NEEDS_ATTENTION
    dl.error = ""
    dl.error_code = ""
    dl.progress = 1.0
    session.add(HistoryEvent(
        series_id=series.id,
        event="imported" if covered_count else "needs_attention",
        source_name=dl.source_name or "torrent",
        detail=(f"{len(imported)} file(s) from {dl.title}"
                if covered_count else f"{len(imported)} file(s) need manual matching from {dl.title}"),
    ))
    await session.commit()
    if covered_count:
        notifications.notify_import(values, series.id, f"{covered_count} issue(s) from {dl.title}")
        _notify_kavita(values, series)


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
    if series.root_folder is None:
        log.warning("Not grabbing for %r: it has no root folder", series.title)
        return 0
    reason = await library_root_unavailable(session, series)
    if reason:
        log.warning("Not grabbing for %r: %s", series.title, reason)
        return 0
    if only_monitored and not series.monitored:
        return 0
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
    # permanent failures keep their 24-hour suppression. Transient failures
    # expire after two hours, escalating after repeated exhausted attempts.
    result = await session.execute(
        select(Download).where(
            Download.series_id == series.id,
            Download.status == DownloadStatus.FAILED,
        )
    )
    failed_downloads = result.scalars().all()
    now = datetime.now(timezone.utc)
    suppressed = [dl for dl in failed_downloads if failed_release_blocks(dl, now)]
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

        if source_cooldown_remaining(src.name):
            continue
        # one series-wide search, then match all wanted numbers
        try:
            releases = await src.list_releases(link.external_id)
        except Exception as exc:
            _source_failed(src.name, exc)
            log.debug("monitor: %s list failed (%s)", src.name, type(exc).__name__)
            continue
        queued += await _grab_matches(session, series, src.name, releases,
                                      remaining, wanted_titles, failed_releases)

        # targeted per-issue searches for stragglers (older issues that fell
        # off the recent-posts pages), capped per pass
        for issue in list(remaining.values())[:straggler_cap]:
            query = f"{link.external_id} {issue.display_number or f'{issue.number:g}'}"
            try:
                results = await src.search_releases(query)
            except Exception as exc:
                _source_failed(src.name, exc)
                log.debug("monitor: %s search failed (%s)", src.name, type(exc).__name__)
                # A transient failure must not be repeated for every issue.
                if source_cooldown_remaining(src.name):
                    break
                continue
            queued += await _grab_matches(session, series, src.name, results,
                                          remaining, wanted_titles, failed_releases)
    if values.get("qbittorrent_enabled") == "true":
        from ..sources.queries import search_queries, search_variants
        for indexer in registry.enabled_torrent_indexers(values):
            if source_cooldown_remaining(indexer.name):
                continue
            for issue in list(remaining.values())[:straggler_cap]:
                queries = search_queries(series, issue, values)
                try:
                    try:
                        torrents = await search_variants(indexer, queries)
                    except Exception as exc:
                        _source_failed(indexer.name, exc)
                        if source_cooldown_remaining(indexer.name):
                            break
                        raise
                    for release in sorted(torrents, key=lambda r: r.seeders, reverse=True):
                        if release.seeders < 1 or (release.source_name, release.magnet) in failed_releases:
                            continue
                        # Search feeds may ignore q. Require the complete rendered
                        # title/number query with word boundaries before auto-grabbing.
                        templates = series.search_templates or values.get("torrent_query_templates", "{series} {issue}")
                        if not all("{series}" in t and "{issue" in t for t in templates.splitlines() if t.strip()):
                            continue
                        title = " " + normalize_title(release.title) + " "
                        if not any(" " + normalize_title(q) + " " in title for q in queries):
                            continue
                        await enqueue_torrent(session, series, release.magnet, release.title, values,
                                              source_name=release.source_name, issue=issue)
                        remaining.pop(issue.number, None)
                        queued += 1
                        break
                except Exception as exc:
                    log.warning("Torznab search/grab failed for series %d: %s", series.id, type(exc).__name__)
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
            # Finished status from ComicVine is inferred from issue recency, so
            # slow complete series down without ever silently unmonitoring them.
            if (
                series.status in (SeriesStatus.FINISHED, SeriesStatus.CANCELLED)
                and series.issues and all(issue.downloaded for issue in series.issues)
                and series.last_monitored_at is not None
            ):
                days = int(values.get("finished_series_check_days", "7"))
                if datetime.now(timezone.utc) - series.last_monitored_at < timedelta(days=days):
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
            series.last_monitored_at = datetime.now(timezone.utc)
            await session.commit()


async def _grab_matches(
    session: AsyncSession,
    series: Series,
    source_name: str,
    releases: list[SourceRelease],
    remaining: dict[float, Issue],
    wanted_titles: set[str],
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
            if release_covers_issue(r, i, series)
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
