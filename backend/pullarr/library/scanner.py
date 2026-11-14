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
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Issue, Series
from ..util import normalize_title
from .matcher import MediaFile, find_media_files, match_files
from .naming import series_folder
from .work import apply_file_state, copy_issues, run_library_work

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    matched_issues: int = 0  # issues newly marked owned
    volume_files: int = 0  # whole-volume archives found
    cleared: int = 0  # issues whose recorded file vanished
    unmatched: list[MediaFile] = field(default_factory=list)
    root_unavailable: str = ""

    @property
    def unmatched_count(self) -> int:
        return len(self.unmatched)


def series_dir(root: Path, series: Series) -> Path:
    return root / (series.folder_name or series_folder(series.title, series.year))


class LibraryUnavailableError(RuntimeError):
    """The configured library cannot currently be trusted for reads or writes."""


def recorded_files_expect_content(root: Path, paths: Iterable[str]) -> bool:
    """Only ownership inside this root proves its mount should be populated.

    Absolute extra folders can hold all of a series' files while its primary
    root is legitimately empty. Compare recorded paths without touching a
    potentially unavailable share. Unknown legacy paths stay conservative.
    """
    try:
        recorded_root = Path(os.path.abspath(root))
        return any(not path or Path(os.path.abspath(path)).is_relative_to(recorded_root) for path in paths)
    except OSError:
        return True


def root_unavailable(root: Path, expect_content: bool = True) -> str:
    """Missing/unreadable roots and unexpectedly empty mount points are unsafe.

    Empty roots are valid for a new library. Once any series owns files in the
    root, an empty directory is more likely an unmounted share than deletion.
    """
    root = Path(root)
    try:
        if not root.is_dir():
            return f"library root folder {root} is missing — is it mounted?"
        # List even brand-new roots to detect unreadable shares.
        first = next(root.iterdir(), None)
        if expect_content and first is None:
            return f"library root folder {root} is empty — is it mounted?"
    except OSError as exc:
        return f"library root folder {root} can't be read: {exc}"
    return ""


def require_root_available(root: Path, expect_content: bool = True) -> None:
    reason = root_unavailable(root, expect_content)
    if reason:
        raise LibraryUnavailableError(reason)


def library_unavailable(
    root: Path, folders: Iterable[Path], recorded_paths: Iterable[str],
    expect_content: bool | None = None,
) -> str:
    """Check the root and each configured folder outside it as mount boundaries.

    A relative series folder may legitimately be deleted beneath a populated
    root. An external primary/extra folder is its own availability boundary;
    never interpret an offline or unexpectedly empty share as deleted books.
    Compare paths lexically, without resolving a potentially missing mount.
    """
    paths = list(recorded_paths)
    if expect_content is None:
        expect_content = recorded_files_expect_content(root, paths)
    reason = root_unavailable(root, expect_content)
    if reason:
        return reason
    absolute_root = Path(os.path.abspath(root))
    checked = {absolute_root}
    for folder in folders:
        absolute = Path(os.path.abspath(folder))
        if absolute in checked or absolute.is_relative_to(absolute_root):
            continue
        checked.add(absolute)
        reason = root_unavailable(folder, recorded_files_expect_content(folder, paths))
        if reason:
            return reason
    return ""


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


def find_existing_folder(root: Path, series: Series, max_depth: int = 3) -> str | None:
    """Return the relative directory path under `root` whose normalized name
    matches the series title or an alt title.

    Existing comic libraries are often grouped by publisher, imprint, or era
    (`Marvel/Ghost Spider`), so this walks a few levels below the root instead
    of checking direct children only.
    """
    root = Path(root)
    if not root.is_dir():
        return None
    wanted = {normalize_title(series.title)}
    if series.year:
        wanted.add(normalize_title(f"{series.title} {series.year}"))
    wanted.update(normalize_title(t) for t in series.alt_titles.split("\n") if t)
    wanted.discard("")
    best: str | None = None
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        parent, depth = stack.pop()
        try:
            children = sorted((c for c in parent.iterdir() if c.is_dir()), key=lambda p: str(p).lower())
        except OSError:
            continue
        for child in children:
            if child.name.startswith("."):
                continue
            rel = str(child.relative_to(root))
            nn = normalize_title(child.name)
            if nn in wanted:
                return rel  # exact normalized match wins immediately
            if best is None and len(nn) >= 4 and any(nn in w or w in nn for w in wanted):
                best = rel
            if depth + 1 < max_depth:
                stack.append((child, depth + 1))
    return best


def scan_series(
    series: Series, issues: list[Issue], folders: list[Path],
    root: Path | None = None, expect_content: bool | None = None,
) -> ScanResult:
    """Apply a scan only while its root remains available, including at the end."""
    if root is None:
        return _scan_series(series, issues, folders)
    recorded_paths = [issue.file_path for issue in issues if issue.downloaded]
    reason = library_unavailable(root, folders, recorded_paths, expect_content)
    if reason:
        return ScanResult(root_unavailable=reason)
    copies = copy_issues(issues)
    result = _scan_series(series, copies, folders)
    reason = library_unavailable(root, folders, recorded_paths, expect_content)
    if reason:
        return ScanResult(root_unavailable=reason)
    apply_file_state(issues, copies)
    return result


def _scan_series(series: Series, issues: list[Issue], folders: list[Path]) -> ScanResult:
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

    # Exact issue files take precedence over volume coverage. Prefer a
    # still-matching recorded file to preserve its ownership and provenance.
    exact = [mf for mf in match.matched if mf.issue is not None]
    exact.sort(key=lambda mf: mf.issue.file_path != str(mf.media.path))
    for mf in exact:
        issue = mf.issue
        if issue.id in owned_now:
            continue
        path_str = str(mf.media.path)
        if not issue.downloaded or issue.file_path != path_str:
            if not issue.downloaded:
                result.matched_issues += 1
            if issue.file_path != path_str:
                issue.file_source = ""
                issue.file_download_id = None
            issue.downloaded = True
            issue.file_path = path_str
        owned_now.add(issue.id)

    # Explicit issue spans take precedence over edition-dependent volume maps.
    # When the previous file vanished, adopt the surviving bundle immediately.
    bundles = sorted(match.matched, key=lambda m: m.media.issue_range is None)
    for mf in bundles:
        if mf.issue is not None:
            continue
        if mf.volume is not None:
            result.volume_files += 1
        path_str = str(mf.media.path)
        for issue in mf.covered_issues:
            if issue.id in owned_now:
                continue
            if not issue.downloaded or issue.file_path != path_str:
                newly_owned = not issue.downloaded
                issue.downloaded = True
                issue.file_path = path_str
                issue.file_source = ""
                issue.file_download_id = None
                result.matched_issues += int(newly_owned)
            owned_now.add(issue.id)

    result.unmatched = match.unmatched
    result.cleared = _reconcile(issues, keep=owned_now)
    log.info("Scanned %r across %d folder(s): +%d issues, %d volume files, "
             "%d unmatched, -%d cleared", series.title, len(existing),
             result.matched_issues, result.volume_files, result.unmatched_count,
             result.cleared)
    return result


async def scan_series_async(
    series: Series, issues: list[Issue], folders: list[Path],
    root: Path | None = None, expect_content: bool | None = None,
) -> ScanResult:
    """Scan detached values in a worker; update session-owned rows on its loop."""
    copies = copy_issues(issues)
    result = await run_library_work(scan_series, Series(title=series.title), copies, folders, root, expect_content)
    apply_file_state(issues, copies)
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
            issue.file_source = ""
            issue.file_download_id = None
            cleared += 1
    return cleared
