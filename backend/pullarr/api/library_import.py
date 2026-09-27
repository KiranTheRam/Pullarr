"""Confirm-before-add import for existing comics folders."""

import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..jobs.service import create_job
from ..jobs.tasks import refresh_series_full
from ..library.matcher import ARCHIVE_EXTS, IMAGE_EXTS, find_media_files
from ..library.scanner import resolve_folders
from ..models import JobKind, RootFolder, Series
from ..schemas import AddSeriesIn, ImportFolderOut, LibraryImportIn, LibraryImportResultOut
from .series import create_series_record

router = APIRouter(prefix="/library/import", tags=["library"])


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


async def _claimed(session: AsyncSession) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    rows = (await session.execute(select(Series).options(
        selectinload(Series.root_folder), selectinload(Series.extra_folders)
    ))).scalars().all()
    claimed: set[tuple[int, int]] = set()
    ancestors: set[tuple[int, int]] = set()
    for series in rows:
        if series.root_folder is None:
            continue
        folders = resolve_folders(Path(series.root_folder.path), series,
                                  [extra.path for extra in series.extra_folders])
        for folder in folders:
            if (identity := _identity(folder)) is not None:
                claimed.add(identity)
            for parent in folder.parents:
                if (identity := _identity(parent)) is not None:
                    ancestors.add(identity)
    return claimed, ancestors


def _conflicts(folder: Path, claimed: set[tuple[int, int]],
               ancestors: set[tuple[int, int]]) -> bool:
    identity = _identity(folder)
    return bool(
        identity in claimed or identity in ancestors
        or any(_identity(parent) in claimed for parent in folder.parents)
    )


def _candidate_dirs(root: Path, claimed: set[tuple[int, int]],
                    ancestors: set[tuple[int, int]]) -> list[ImportFolderOut]:
    """Show likely series folders at root or one publisher level below it."""
    out: list[ImportFolderOut] = []
    def add(folder: Path) -> None:
        if _conflicts(folder, claimed, ancestors):
            return
        try:
            media = find_media_files(folder)
        except OSError:
            return
        if media:
            out.append(ImportFolderOut(
                folder_name=str(folder.relative_to(root)), path=str(folder),
                file_count=len(media), query=folder.name,
            ))

    for folder in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        # A publisher folder can be an ancestor of a tracked series while
        # still containing untracked sibling series. Check each candidate
        # below instead of pruning the publisher folder here.
        try:
            children = [p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")]
            own_media = any(p.is_file() and p.suffix.lower() in ARCHIVE_EXTS | IMAGE_EXTS
                            for p in folder.iterdir())
        except OSError:
            continue
        # Issue/volume folders belong to the series above them. Otherwise,
        # treat this as a publisher container and offer its series children.
        issue_children = any(re.search(r"(?:^|\s)(?:#\s*\d+|(?:issue|chapter|volume|vol\.?|v)\s*\d+|\d{1,3})(?:\b|$)",
                                       child.name, re.IGNORECASE) for child in children)
        if own_media or issue_children or not children:
            add(folder)
        else:
            for child in sorted(children, key=lambda path: path.name.casefold()):
                add(child)
    return out


@router.get("/folders", response_model=list[ImportFolderOut])
async def import_folders(root_folder_id: int, session: AsyncSession = Depends(get_session)):
    root = await session.get(RootFolder, root_folder_id)
    if root is None:
        raise HTTPException(404, "Root folder not found")
    path = Path(root.path)
    if not path.is_dir():
        raise HTTPException(400, "Root folder is unavailable")
    claimed, ancestors = await _claimed(session)
    return await asyncio.to_thread(_candidate_dirs, path, claimed, ancestors)


def _safe_folder(root: Path, relative: str) -> Path:
    name = Path(relative)
    if name.is_absolute() or not relative or any(part in ("..", ".") for part in name.parts):
        raise HTTPException(422, "Folder must be inside the selected root")
    folder = root / name
    if not folder.is_dir() or not folder.resolve().is_relative_to(root.resolve()):
        raise HTTPException(422, "Folder is unavailable or outside the selected root")
    return folder


async def _refresh_in_order(jobs: list[tuple[int, int, bool]]) -> None:
    for series_id, job_id, search_now in jobs:
        await refresh_series_full(series_id, grab_missing=search_now,
                                  job_id=job_id, force_scan=True)


@router.post("", response_model=list[LibraryImportResultOut])
async def import_library(body: LibraryImportIn, session: AsyncSession = Depends(get_session)):
    root = await session.get(RootFolder, body.root_folder_id)
    if root is None:
        raise HTTPException(404, "Root folder not found")
    root_path = Path(root.path)
    if not root_path.is_dir():
        raise HTTPException(400, "Root folder is unavailable")
    if len(body.items) > 200:
        raise HTTPException(422, "Import at most 200 folders at once")
    claimed, ancestors = await _claimed(session)
    selected: set[tuple[int, int]] = set()
    jobs: list[tuple[int, int, bool]] = []
    results: list[LibraryImportResultOut] = []
    for item in body.items:
        try:
            folder = _safe_folder(root_path, item.folder_name)
        except HTTPException as exc:
            results.append(LibraryImportResultOut(folder_name=item.folder_name,
                                                  status="failed", detail=str(exc.detail)))
            continue
        identity = _identity(folder)
        if identity is None:
            results.append(LibraryImportResultOut(folder_name=item.folder_name, status="failed",
                                                  detail="Folder is unavailable"))
            continue
        if _conflicts(folder, claimed, ancestors) or identity in selected:
            results.append(LibraryImportResultOut(folder_name=item.folder_name, status="exists",
                                                  detail="Folder is already in the library or batch"))
            continue
        selected.add(identity)
        try:
            series = await create_series_record(AddSeriesIn(
                comicvine_id=item.comicvine_id, root_folder_id=body.root_folder_id,
                monitored=body.monitored, monitor_mode=body.monitor_mode,
                monitor_from=body.monitor_from, search_now=body.search_now,
                folder_name=item.folder_name,
            ), session)
            job = await create_job(session,
                JobKind.SEARCH_MISSING if body.search_now else JobKind.REFRESH_SERIES,
                series_id=series.id, detail="Imported library series sync")
        except HTTPException as exc:
            await session.rollback()
            results.append(LibraryImportResultOut(folder_name=item.folder_name,
                status="exists" if exc.status_code == 409 else "failed", detail=str(exc.detail)))
            continue
        jobs.append((series.id, job.id, body.search_now))
        claimed.add(identity)
        ancestors.update(filter(None, (_identity(parent) for parent in folder.parents)))
        results.append(LibraryImportResultOut(folder_name=item.folder_name,
                                               status="added", series_id=series.id))
    if jobs:
        asyncio.get_running_loop().create_task(_refresh_in_order(jobs))
    return results
