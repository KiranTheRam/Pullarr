"""Existing-library endpoints: scan/adopt, preview+apply rename, per-series
file listing with manual mapping, and a filesystem folder browser."""

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .. import settings_service
from ..db import get_session
from ..jobs.service import create_job
from ..library.matcher import find_media_files, match_files
from ..library.rename import apply_renames, plan_renames
from ..library.scanner import (
    find_existing_folder,
    resolve_folders,
    scan_series,
    series_dir,
)
from ..models import JobKind, RootFolder, Series, SeriesFolder, SeriesSourceLink
from ..schemas import (
    CleanupApplyIn,
    CleanupFileOut,
    CleanupGroupOut,
    CleanupPlanOut,
    CleanupResultOut,
    FileMapIn,
    FileMapRangeIn,
    FileMapRangeOut,
    FilesystemEntryOut,
    FilesystemListOut,
    FolderPreviewIn,
    FolderPreviewOut,
    RenameApplyIn,
    RenameItemOut,
    RenameOutcomeOut,
    ResyncOut,
    ScanResultOut,
    SeriesFileOut,
    SeriesFolderIn,
    SeriesFolderOut,
    SourceCandidateOut,
    SourceLinkIn,
    SourceLinkOut,
)
from ..sources import registry

router = APIRouter(tags=["library"])


async def _load(session: AsyncSession, series_id: int) -> Series:
    result = await session.execute(
        select(Series)
        .options(
            selectinload(Series.issues),
            selectinload(Series.source_links),
            selectinload(Series.root_folder),
            selectinload(Series.extra_folders),
        )
        .where(Series.id == series_id)
    )
    series = result.scalar_one_or_none()
    if series is None:
        raise HTTPException(404, "Series not found")
    return series


def _root_of(series: Series) -> Path:
    if series.root_folder is None:
        raise HTTPException(400, "Series has no root folder configured")
    return Path(series.root_folder.path)


def _folders_of(series: Series) -> list[Path]:
    """Primary folder plus any extra folders configured for the series."""
    return resolve_folders(_root_of(series), series, [f.path for f in series.extra_folders])


# ------------------------------------------------------------------ scan

@router.post("/series/{series_id}/scan", response_model=ScanResultOut)
async def scan(series_id: int, session: AsyncSession = Depends(get_session)):
    series = await _load(session, series_id)
    root = _root_of(series)
    folders = _folders_of(series)
    # adopt a matching folder if the primary one doesn't exist yet
    if not folders[0].exists() and not series.extra_folders:
        found = find_existing_folder(root, series)
        if found:
            series.folder_name = found
            folders = _folders_of(series)
    result = scan_series(series, list(series.issues), folders)
    await session.commit()
    return ScanResultOut(
        folder=", ".join(str(f) for f in folders),
        folder_exists=any(f.exists() for f in folders),
        matched_issues=result.matched_issues,
        volume_files=result.volume_files,
        cleared=result.cleared,
        unmatched=[m.path.name for m in result.unmatched],
    )


@router.post("/library/scan", status_code=202)
async def scan_all(session: AsyncSession = Depends(get_session)):
    from ..jobs.tasks import scan_all_series

    job = await create_job(session, JobKind.SCAN_LIBRARY)
    asyncio.get_running_loop().create_task(scan_all_series(job.id))
    return {"status": "scanning", "job_id": job.id}


# ----------------------------------------------------------------- rename

async def _plan(session: AsyncSession, series: Series):
    values = await settings_service.get_all(session)
    return plan_renames(series, list(series.issues), values["naming_template"])


@router.get("/series/{series_id}/rename", response_model=list[RenameItemOut])
async def rename_preview(series_id: int, session: AsyncSession = Depends(get_session)):
    series = await _load(session, series_id)
    return [
        RenameItemOut(
            issue_ids=i.issue_ids, current_path=i.current_path,
            current_name=i.current_name, new_path=i.new_path, new_name=i.new_name,
            conflict=i.conflict,
        )
        for i in await _plan(session, series)
    ]


@router.post("/series/{series_id}/rename", response_model=list[RenameOutcomeOut])
async def rename_apply(
    series_id: int, body: RenameApplyIn, session: AsyncSession = Depends(get_session)
):
    series = await _load(session, series_id)
    items = await _plan(session, series)
    if body.issue_ids is not None:
        wanted = set(body.issue_ids)
        items = [i for i in items if wanted & set(i.issue_ids)]
    outcomes = apply_renames(items, {i.id: i for i in series.issues})
    await session.commit()
    return [
        RenameOutcomeOut(
            current_name=o.item.current_name, new_name=o.item.new_name,
            status=o.status, detail=o.detail,
        )
        for o in outcomes
    ]


# --------------------------------------------------------- files + mapping

@router.get("/series/{series_id}/files", response_model=list[SeriesFileOut])
async def series_files(series_id: int, session: AsyncSession = Depends(get_session)):
    series = await _load(session, series_id)
    media = []
    for folder in _folders_of(series):
        if folder.exists():
            media.extend(find_media_files(folder))
    result = match_files(media, list(series.issues))
    out: list[SeriesFileOut] = []
    for mf in result.matched:
        out.append(SeriesFileOut(
            path=str(mf.media.path), name=mf.media.path.name, is_dir=mf.media.is_dir,
            issue_number=mf.media.issue_number, volume_number=mf.media.volume_number,
            matched_issue_id=mf.issue.id if mf.issue else None,
            covered_count=len(mf.covered_issues),
        ))
    for m in result.unmatched:
        out.append(SeriesFileOut(
            path=str(m.path), name=m.path.name, is_dir=m.is_dir,
            issue_number=m.issue_number, volume_number=m.volume_number,
            matched_issue_id=None,
        ))
    return out


@router.post("/series/{series_id}/files/map", status_code=204)
async def map_file(
    series_id: int, body: FileMapIn, session: AsyncSession = Depends(get_session)
):
    series = await _load(session, series_id)
    issue = next((i for i in series.issues if i.id == body.issue_id), None)
    if issue is None:
        raise HTTPException(404, "Issue not found")
    if not Path(body.file_path).exists():
        raise HTTPException(400, "File not found on disk")
    issue.downloaded = True
    issue.file_path = body.file_path
    await session.commit()


@router.post("/series/{series_id}/files/map-range", response_model=FileMapRangeOut)
async def map_file_range(
    series_id: int, body: FileMapRangeIn, session: AsyncSession = Depends(get_session)
):
    """Map a TPB/collected-volume archive to an issue range — ComicVine has no
    issue→TPB data, so coverage is declared by hand. Also stamps the parsed
    volume onto those issues so future scans/renames keep working."""
    from ..util import parse_volume_number

    series = await _load(session, series_id)
    if not Path(body.file_path).exists():
        raise HTTPException(400, "File not found on disk")
    lo, hi = sorted((body.from_number, body.to_number))
    volume = parse_volume_number(Path(body.file_path).stem)
    mapped = 0
    for issue in series.issues:
        if lo <= issue.number <= hi:
            issue.downloaded = True
            issue.file_path = body.file_path
            if volume is not None:
                issue.volume = volume
            mapped += 1
    if mapped == 0:
        raise HTTPException(400, "No tracked issues in that range")
    await session.commit()
    return FileMapRangeOut(mapped=mapped, volume=volume)


@router.get("/series/{series_id}/cleanup", response_model=CleanupPlanOut)
async def cleanup_plan(series_id: int, session: AsyncSession = Depends(get_session)):
    from ..library.cleanup import analyze

    series = await _load(session, series_id)
    values = await settings_service.get_all(session)
    plan = analyze(series, list(series.issues), _folders_of(series),
                   values["naming_template"])

    def out(f):
        return CleanupFileOut(path=f.path, name=Path(f.path).name, size=f.size,
                              referenced=f.referenced, keep=f.keep)

    return CleanupPlanOut(
        groups=[CleanupGroupOut(label=g.label, files=[out(f) for f in g.files])
                for g in plan.groups],
        orphans=[out(f) for f in plan.orphans],
    )


@router.post("/series/{series_id}/cleanup", response_model=CleanupResultOut)
async def cleanup_apply(
    series_id: int, body: CleanupApplyIn, session: AsyncSession = Depends(get_session)
):
    from ..library.cleanup import apply_cleanup

    series = await _load(session, series_id)
    result = apply_cleanup(series, list(series.issues), _folders_of(series), body.delete)
    await session.commit()
    return CleanupResultOut(
        deleted=result.deleted, repointed=result.repointed,
        skipped=result.skipped, freed_bytes=result.freed_bytes,
    )


# --------------------------------------------------------------- folders

def _relative_to_root(root: Path, raw: str) -> str:
    """Store a path relative to the root when it's under it, else as given."""
    raw = raw.strip()
    if raw.startswith("/"):
        try:
            return str(Path(raw).relative_to(root))
        except ValueError:
            return raw
    return raw.strip("/")


@router.get("/series/{series_id}/folders", response_model=list[SeriesFolderOut])
async def list_folders(series_id: int, session: AsyncSession = Depends(get_session)):
    series = await _load(session, series_id)
    root = _root_of(series)
    out = [SeriesFolderOut(
        id=None, path=series.folder_name, resolved=str(series_dir(root, series)),
        primary=True, exists=series_dir(root, series).exists(),
    )]
    for f in series.extra_folders:
        p = root / f.path
        out.append(SeriesFolderOut(
            id=f.id, path=f.path, resolved=str(p), primary=False, exists=p.exists(),
        ))
    return out


@router.post("/series/{series_id}/folders", response_model=SeriesFolderOut, status_code=201)
async def add_folder(
    series_id: int, body: SeriesFolderIn, session: AsyncSession = Depends(get_session)
):
    series = await _load(session, series_id)
    root = _root_of(series)
    path = _relative_to_root(root, body.path)
    if not path or path == series.folder_name or any(f.path == path for f in series.extra_folders):
        raise HTTPException(400, "Folder already configured for this series")
    folder = SeriesFolder(series_id=series.id, path=path)
    session.add(folder)
    await session.commit()
    await session.refresh(folder)
    resolved = root / path
    return SeriesFolderOut(
        id=folder.id, path=folder.path, resolved=str(resolved),
        primary=False, exists=resolved.exists(),
    )


@router.delete("/series/{series_id}/folders/{folder_id}", status_code=204)
async def remove_folder(
    series_id: int, folder_id: int, session: AsyncSession = Depends(get_session)
):
    folder = await session.get(SeriesFolder, folder_id)
    if folder is None or folder.series_id != series_id:
        raise HTTPException(404, "Folder not found")
    await session.delete(folder)
    await session.commit()


# ---------------------------------------------------------- source links

@router.get("/sources", response_model=list[str])
async def list_sources():
    """Names of the DDL sources that can be linked/searched."""
    return list(registry.DDL_SOURCES.keys())


@router.get("/series/{series_id}/sources/search", response_model=list[SourceCandidateOut])
async def source_search(
    series_id: int, source_name: str, query: str,
    session: AsyncSession = Depends(get_session),
):
    await _load(session, series_id)  # 404 if series missing
    await registry.apply_settings(session)  # configures sources
    src = registry.DDL_SOURCES.get(source_name)
    if src is None:
        raise HTTPException(404, f"Unknown source {source_name!r}")
    try:
        candidates = await src.search_series(query)
    except Exception as exc:
        raise HTTPException(502, f"{source_name} search failed: {exc}") from exc
    return [
        SourceCandidateOut(source_name=source_name, external_id=c.external_id,
                           title=c.title, url=c.url, alt_titles=c.alt_titles)
        for c in candidates[:20]
    ]


@router.post("/series/{series_id}/sources", response_model=SourceLinkOut, status_code=201)
async def set_source_link(
    series_id: int, body: SourceLinkIn, session: AsyncSession = Depends(get_session)
):
    series = await _load(session, series_id)
    link = next((l for l in series.source_links if l.source_name == body.source_name), None)
    if link is None:
        link = SeriesSourceLink(source_name=body.source_name)
        series.source_links.append(link)
    link.external_id = body.external_id
    link.external_title = body.external_title
    link.external_url = body.external_url
    await session.commit()
    await session.refresh(link)
    return link


@router.delete("/series/{series_id}/sources/{link_id}", status_code=204)
async def delete_source_link(
    series_id: int, link_id: int, session: AsyncSession = Depends(get_session)
):
    link = await session.get(SeriesSourceLink, link_id)
    if link is None or link.series_id != series_id:
        raise HTTPException(404, "Source link not found")
    await session.delete(link)
    await session.commit()


@router.post("/series/{series_id}/resync", response_model=ResyncOut)
async def resync_issues(series_id: int, session: AsyncSession = Depends(get_session)):
    """Rebuild the issue list from ComicVine (use after fixing a wrong match).
    Clears existing issues + this series' download records, re-syncs, then
    re-adopts files from disk."""
    from sqlalchemy import delete as sa_delete

    from ..jobs.tasks import update_issues
    from ..models import Download, HistoryEvent

    series = await _load(session, series_id)
    await registry.apply_settings(session)
    await session.execute(sa_delete(Download).where(Download.series_id == series_id))
    await session.execute(sa_delete(HistoryEvent).where(HistoryEvent.series_id == series_id))
    for issue in list(series.issues):
        await session.delete(issue)
    await session.commit()

    series = await _load(session, series_id)
    await update_issues(session, series)
    scan = await _scan_now(session, series)
    return ResyncOut(issues=len(series.issues), matched_issues=scan)


async def _scan_now(session: AsyncSession, series: Series) -> int:
    result = scan_series(series, list(series.issues), _folders_of(series))
    await session.commit()
    return result.matched_issues


@router.post("/library/folder-preview", response_model=FolderPreviewOut)
async def folder_preview(body: FolderPreviewIn, session: AsyncSession = Depends(get_session)):
    """Show the folder a prospective ComicVine volume will use.

    Pullarr keeps the ComicVine start year in the default folder name so
    reboots like Batman (1940) and Batman (2016) remain distinct, while still
    adopting an existing matching folder when one is already under the root.
    """
    from ..library.naming import series_folder

    root = await session.get(RootFolder, body.root_folder_id)
    if root is None:
        raise HTTPException(404, "Root folder not found")
    probe = Series(
        title=body.title,
        year=body.year,
        alt_titles="\n".join(body.alt_titles),
    )
    found = find_existing_folder(Path(root.path), probe)
    name = found or series_folder(body.title, body.year)
    resolved = Path(root.path) / name
    return FolderPreviewOut(
        folder_name=name,
        path=str(resolved),
        exists=resolved.exists(),
        matched=found is not None,
    )


# ---------------------------------------------------------- filesystem browse

@router.get("/filesystem", response_model=FilesystemListOut)
async def browse(
    path: str = Query(default=""), session: AsyncSession = Depends(get_session)
):
    """Folder browser: root-folder shortcuts plus the whole container filesystem."""
    roots = [
        Path(r.path)
        for r in (await session.execute(select(RootFolder))).scalars().all()
    ]
    if not path:
        entries = [FilesystemEntryOut(name=str(r), path=str(r)) for r in roots]
        if not any(str(r) == "/" for r in roots):
            entries.append(FilesystemEntryOut(name="/", path="/"))
        return FilesystemListOut(path="", parent=None, entries=entries)
    target = Path(path)
    if not target.is_absolute():
        raise HTTPException(400, "Path must be absolute")
    if not target.is_dir():
        raise HTTPException(404, "Not a directory")
    try:
        children = [c for c in target.iterdir() if c.is_dir()]
    except OSError as exc:
        raise HTTPException(400, f"Cannot list {target}: {exc}") from exc
    entries = sorted(
        (FilesystemEntryOut(name=c.name, path=str(c)) for c in children),
        key=lambda e: e.name.lower(),
    )
    at_top = target == target.parent
    return FilesystemListOut(
        path=str(target),
        parent=None if at_top else str(target.parent),
        entries=entries,
    )
