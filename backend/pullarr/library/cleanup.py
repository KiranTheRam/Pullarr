"""Find and remove duplicate / orphaned files in a series' folders.

Common after grabbing a pack onto a library that already has the same
issues: two files end up representing the same issue/volume. This groups
files by what they represent, recommends which copy to keep (the one
matching pullarr's naming, else the referenced one, else the largest), and —
on apply — deletes the rest and re-points any issues at the survivor. It
never deletes the last copy backing a downloaded issue."""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Issue, Series
from .matcher import MediaFile, find_media_files
from .naming import issue_filename, volume_filename

log = logging.getLogger(__name__)


@dataclass
class CleanupFile:
    path: str
    size: int
    referenced: bool  # a downloaded issue points at this file
    keep: bool  # recommended default

    @property
    def name(self) -> str:
        return Path(self.path).name


@dataclass
class CleanupGroup:
    label: str  # "Volume 3" / "Issue 12"
    files: list[CleanupFile]


@dataclass
class CleanupPlan:
    groups: list[CleanupGroup] = field(default_factory=list)  # >1 file for one thing
    orphans: list[CleanupFile] = field(default_factory=list)  # standalone extras


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _identity(mf: MediaFile, tracked_issues: set[float], tracked_vol: set[int]):
    if mf.issue_number is not None and mf.issue_number in tracked_issues:
        return ("issue", mf.issue_number)
    if mf.volume_number is not None and mf.volume_number in tracked_vol:
        return ("vol", mf.volume_number)
    return ("unknown", str(mf.path))


def _all_media(folders: list[Path]) -> list[MediaFile]:
    media: list[MediaFile] = []
    for folder in folders:
        if Path(folder).exists():
            media.extend(find_media_files(folder))
    return media


def analyze(
    series: Series,
    issues: list[Issue],
    folders: list[Path],
    template: str,
) -> CleanupPlan:
    media = _all_media(folders)
    referenced = {i.file_path for i in issues if i.downloaded and i.file_path}
    tracked_issues = {i.number for i in issues}
    tracked_vol = {i.volume for i in issues if i.volume is not None}
    issue_by_num = {i.number: i for i in issues}
    vol_downloaded = {
        v: all(i.downloaded for i in issues if i.volume == v) for v in tracked_vol
    }

    by_identity: dict[tuple, list[MediaFile]] = {}
    for mf in media:
        by_identity.setdefault(_identity(mf, tracked_issues, tracked_vol), []).append(mf)

    plan = CleanupPlan()
    for identity, files in by_identity.items():
        kind, num = identity
        if len(files) > 1:
            plan.groups.append(_group(series, kind, num, files, referenced,
                                      issue_by_num, template))
            continue
        mf = files[0]
        path = str(mf.path)
        if path in referenced:
            continue  # single, in use — nothing to clean
        # a standalone unreferenced file: redundant if its content is already
        # covered elsewhere (a downloaded issue / a fully-downloaded volume)
        if kind == "issue":
            redundant = num in tracked_issues and issue_by_num[num].downloaded
        elif kind == "vol":
            redundant = vol_downloaded.get(num, False)
        else:
            redundant = False  # unknown extra — keep by default
        plan.orphans.append(CleanupFile(path, _size(path), False, keep=not redundant))

    plan.groups.sort(key=lambda g: g.label)
    plan.orphans.sort(key=lambda f: f.path)
    return plan


def _group(series, kind, num, files, referenced, issue_by_num, template):
    ext = Path(str(files[0].path)).suffix.lower()
    if kind == "issue":
        issue = issue_by_num[num]
        canonical = issue_filename(template, series.title, issue.number,
                                   issue.title, series.year, ext=ext)
        label = f"Issue {num:g}"
    else:
        canonical = volume_filename(series.title, num, ext)
        label = f"Volume {num}"

    cfiles = [CleanupFile(str(mf.path), _size(str(mf.path)), str(mf.path) in referenced, False)
              for mf in files]
    # default to keeping the file that's already in use (the established library
    # copy), so cleanup only removes the accidental duplicate and never has to
    # delete an in-use file. Fall back to the canonically-named one, then size.
    keeper = next((c for c in cfiles if c.referenced), None)
    if keeper is None:
        keeper = next((c for c in cfiles if Path(c.path).name == canonical), None)
    if keeper is None:
        keeper = max(cfiles, key=lambda c: c.size)
    keeper.keep = True
    return CleanupGroup(label, cfiles)


@dataclass
class CleanupResult:
    deleted: int = 0
    repointed: int = 0
    skipped: int = 0
    freed_bytes: int = 0


def apply_cleanup(
    series: Series, issues: list[Issue], folders: list[Path], delete_paths: list[str]
) -> CleanupResult:
    media = _all_media(folders)
    tracked_issues = {i.number for i in issues}
    tracked_vol = {i.volume for i in issues if i.volume is not None}
    identity_of = {str(mf.path): _identity(mf, tracked_issues, tracked_vol) for mf in media}
    delete_set = set(delete_paths)
    result = CleanupResult()

    for path in delete_paths:
        if not os.path.exists(path):
            continue
        referencing = [i for i in issues if i.file_path == path]
        if referencing:
            ident = identity_of.get(path)
            survivors = [
                str(mf.path) for mf in media
                if identity_of.get(str(mf.path)) == ident
                and str(mf.path) not in delete_set and os.path.exists(str(mf.path))
            ]
            if not survivors:
                result.skipped += 1  # would leave an issue with no file — refuse
                continue
            for i in referencing:
                i.file_path = survivors[0]
            result.repointed += len(referencing)
        size = _size(path)
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("cleanup: could not delete %s: %s", path, exc)
            result.skipped += 1
            continue
        result.deleted += 1
        result.freed_bytes += size
        log.info("cleanup: deleted %s", path)
    return result
