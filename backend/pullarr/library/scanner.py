"""Scan existing library folders and adopt files in place.

Read-only with respect to the filesystem: it only records which tracked
issues are already present on disk (setting Issue.downloaded/file_path).
It never copies, moves, or writes files — that's what lets pullarr sit on top
of a library the user already has without re-downloading anything.

A series can have several folders (a primary plus extras), e.g. a TPB
directory and a separate loose-issues directory; scanning looks across all
of them and, where an issue is available both as a loose file and inside a
whole-volume archive, the exact issue file wins."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Issue, Series
from ..util import normalize_title
from .matcher import MediaFile, find_media_files, match_files
from .naming import series_folder

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    matched_issues: int = 0  # issues newly marked owned
    volume_files: int = 0  # whole-volume archives found
    cleared: int = 0  # issues whose recorded file vanished
    unmatched: list[MediaFile] = field(default_factory=list)

    @property
    def unmatched_count(self) -> int:
        return len(self.unmatched)


def series_dir(root: Path, series: Series) -> Path:
    return root / (series.folder_name or series_folder(series.title, series.year))


def resolve_folders(root: Path, series: Series, extra_paths: list[str]) -> list[Path]:
    """All directories to scan for a series: the primary folder plus any extra
    folders. Extra paths may be relative to the root or absolute (pathlib joins
    an absolute right-hand side by replacing, so `root / abs` == abs)."""
    root = Path(root)
    values = [series.folder_name or series_folder(series.title, series.year), *extra_paths]
    folders: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        p = root / value
        if str(p) not in seen:
            seen.add(str(p))
            folders.append(p)
    return folders


def find_existing_folder(root: Path, series: Series) -> str | None:
    """Return the sub-directory name of `root` whose normalized name matches
    the series title or an alt title, so pullarr can adopt a pre-existing
    folder even when it isn't named exactly like the series."""
    root = Path(root)
    if not root.is_dir():
        return None
    wanted = {normalize_title(series.title)}
    if series.year:
        wanted.add(normalize_title(f"{series.title} {series.year}"))
    wanted.update(normalize_title(t) for t in series.alt_titles.split("\n") if t)
    wanted.discard("")
    best = None
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        nn = normalize_title(child.name)
        if nn in wanted:
            return child.name  # exact normalized match wins immediately
        if best is None and nn and any(nn in w or w in nn for w in wanted):
            best = child.name
    return best


def scan_series(series: Series, issues: list[Issue], folders: list[Path]) -> ScanResult:
    """Mark issues present across `folders` as downloaded (in place)."""
    folders = [Path(f) for f in folders]
    result = ScanResult()
    existing = [f for f in folders if f.exists()]
    if not existing:
        result.cleared = _reconcile(issues, keep=set())
        return result

    media: list[MediaFile] = []
    for folder in existing:
        media.extend(find_media_files(folder))
    match = match_files(media, issues)

    owned_now: set[int] = set()

    # exact issue files first — they take precedence over volume coverage
    for mf in match.matched:
        if mf.issue is None:
            continue
        issue = mf.issue
        path_str = str(mf.media.path)
        if issue.id not in owned_now and (
            not issue.downloaded or issue.file_path != path_str
        ):
            if not issue.downloaded:
                result.matched_issues += 1
            issue.downloaded = True
            issue.file_path = path_str
        owned_now.add(issue.id)

    # whole-volume archives fill in any issues not already covered exactly
    for mf in match.matched:
        if mf.issue is not None:
            continue
        if mf.volume is not None:
            result.volume_files += 1
        path_str = str(mf.media.path)
        for issue in mf.covered_issues:
            if issue.id in owned_now:
                continue
            if not issue.downloaded or not issue.file_path:
                issue.downloaded = True
                issue.file_path = path_str
                result.matched_issues += 1
            owned_now.add(issue.id)

    result.unmatched = match.unmatched
    result.cleared = _reconcile(issues, keep=owned_now)
    log.info("Scanned %r across %d folder(s): +%d issues, %d volume files, "
             "%d unmatched, -%d cleared", series.title, len(existing),
             result.matched_issues, result.volume_files, result.unmatched_count,
             result.cleared)
    return result


def _reconcile(issues: list[Issue], keep: set[int]) -> int:
    """Clear downloaded state for issues whose recorded file is gone."""
    cleared = 0
    for issue in issues:
        if not issue.downloaded or issue.id in keep:
            continue
        if not issue.file_path or not Path(issue.file_path).exists():
            issue.downloaded = False
            issue.file_path = ""
            cleared += 1
    return cleared
