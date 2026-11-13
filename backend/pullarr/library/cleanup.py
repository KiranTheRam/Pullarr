"""Find and remove duplicate / orphaned files in a series' folders.

Common after grabbing a pack onto a library that already has the same
issues: two files end up representing the same issue/volume. This groups
files by what they represent, recommends which copy to keep (the one
matching pullarr's naming, else the referenced one, else the largest), and —
on apply — deletes the rest and re-points any issues at the survivor. It
never deletes the last copy backing a downloaded issue."""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..filename import BRACKET_GROUPS, parse_filename
from ..models import Issue, Series
from ..util import parse_issue_label
from .matcher import MediaFile, find_media_files, match_files
from .naming import filename_for_issue, volume_filename

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


def _canonical(path: str | Path) -> str:
    return str(Path(path).resolve())


def _provable(mf: MediaFile) -> bool:
    """Ranges and uncertain names can never prove another archive redundant."""
    if mf.is_dir:
        return False
    stem = mf.path.stem
    coverage = parse_filename(stem)
    if coverage.ambiguous or coverage.is_range:
        return False
    label = parse_issue_label(stem)
    if label and re.fullmatch(r"\d+(?:\.\d+)?", label) is None:
        return False  # a variant suffix cannot prove a plain numeric issue duplicate
    for match in BRACKET_GROUPS.finditer(stem):
        tag = match.group()[1:-1].strip()
        # Ignore scene tags for matching, but keep issue-like tags out of
        # automatic deletion decisions, including partially written labels.
        if re.search(r"(?:#|(?:issue|no)[ ._]*|v(?:ol(?:ume)?)?[ ._]*)\d", tag, re.I):
            parsed = parse_filename(tag)
            if not parsed.explicit_issue and not parsed.volume:
                return False
    if coverage.issue_number is not None:
        return mf.issue_number == coverage.issue_number
    if coverage.volume_number is not None:
        return mf.issue_number is None and mf.volume_number == coverage.volume_number
    return False  # metadata-only or unknown identity cannot prove filename coverage


def _identity(mf: MediaFile, issues_by_number: dict[float, list[Issue]], tracked_vol: set[int]):
    if not _provable(mf):
        return ("unknown", str(mf.path))
    if mf.issue_number is not None and len(issues_by_number.get(mf.issue_number, [])) == 1:
        return ("issue", mf.issue_number)
    if mf.issue_number is None and mf.volume_number is not None and mf.volume_number in tracked_vol:
        return ("vol", mf.volume_number)
    return ("unknown", str(mf.path))


def _all_media(folders: list[Path]) -> list[MediaFile]:
    media: dict[str, MediaFile] = {}
    for folder in folders:
        if Path(folder).exists():
            for mf in find_media_files(folder):
                media.setdefault(_canonical(mf.path), mf)
    return list(media.values())


def _coverage(media: list[MediaFile], issues: list[Issue]):
    coverage = {_canonical(mf.path): set() for mf in media}
    for matched in match_files(media, issues).matched:
        coverage[_canonical(matched.media.path)].update(i.id for i in matched.covered_issues)
    required = {key: set(value) for key, value in coverage.items()}
    for issue in issues:
        if issue.file_path and (key := _canonical(issue.file_path)) in required:
            required[key].add(issue.id)
    return coverage, required


def _covered_elsewhere(key, coverage, survivors, required):
    proven = set().union(*(coverage[other] for other in survivors if other != key))
    return bool(required) and required <= proven


def analyze(
    series: Series,
    issues: list[Issue],
    folders: list[Path],
    template: str,
) -> CleanupPlan:
    media = _all_media(folders)
    referenced = {_canonical(i.file_path) for i in issues if i.file_path}
    tracked_vol = {i.volume for i in issues if i.volume is not None}
    issue_by_num = {i.number: i for i in issues}
    issues_by_number: dict[float, list[Issue]] = {}
    for issue in issues:
        issues_by_number.setdefault(issue.number, []).append(issue)
    coverage, required = _coverage(media, issues)
    retained = {_canonical(mf.path) for mf in media if _provable(mf)
                and required[_canonical(mf.path)] == coverage[_canonical(mf.path)]}
    by_identity: dict[tuple, list[MediaFile]] = {}
    for mf in media:
        key = _canonical(mf.path)
        if key in retained:
            by_identity.setdefault(_identity(mf, issues_by_number, tracked_vol), []).append(mf)

    plan = CleanupPlan()
    grouped = set()
    for (kind, num), files in by_identity.items():
        if kind == "unknown" or len(files) < 2:
            continue
        group = _group(series, kind, num, files, referenced, issue_by_num, template)
        for item in group.files:
            key = _canonical(item.path)
            if not item.keep:
                if _covered_elsewhere(key, coverage, retained, required[key]):
                    retained.remove(key)
                else:
                    item.keep = True
            grouped.add(key)
        plan.groups.append(group)

    for mf in media:
        path = str(mf.path)
        key = _canonical(path)
        if key in grouped or key in referenced:
            continue
        redundant = (key in retained and _covered_elsewhere(key, coverage, retained, required[key]))
        if redundant:
            retained.remove(key)
        plan.orphans.append(CleanupFile(path, _size(path), False, keep=not redundant))
    plan.groups.sort(key=lambda g: g.label)
    plan.orphans.sort(key=lambda f: f.path)
    return plan


def _group(series, kind, num, files, referenced, issue_by_num, template):
    ext = Path(str(files[0].path)).suffix.lower()
    if kind == "issue":
        canonical = filename_for_issue(template, series, issue_by_num[num], ext=ext)
        label = f"Issue {num:g}"
    else:
        canonical = volume_filename(series.title, num, ext)
        label = f"Volume {num}"

    cfiles = [CleanupFile(str(mf.path), _size(str(mf.path)), _canonical(mf.path) in referenced, False)
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
    media_by_resolved = {_canonical(mf.path): mf for mf in media}
    coverage, required = _coverage(media, issues)
    delete_set = {_canonical(p) for p in delete_paths}
    result = CleanupResult()
    seen = set()
    for requested in delete_paths:
        resolved = _canonical(requested)
        if resolved in seen:
            continue
        seen.add(resolved)
        mf = media_by_resolved.get(resolved)
        if mf is None or mf.is_dir:
            result.skipped += 1
            continue
        path = str(mf.path)
        if not mf.path.exists():
            continue
        survivors = {key for key, other in media_by_resolved.items()
                     if key not in delete_set and other.path.exists() and _provable(other)
                     and required[key] == coverage[key]}
        # Recheck the entire batch, including unreferenced known content and
        # stale downloaded flags. Ambiguous names cannot prove a surviving copy.
        if required[resolved] and not _covered_elsewhere(resolved, coverage, survivors, required[resolved]):
            result.skipped += 1
            continue
        referencing = [i for i in issues if i.file_path and _canonical(i.file_path) == resolved]
        replacements = {i.id: next((str(media_by_resolved[key].path) for key in sorted(survivors)
                                    if i.id in coverage[key]), None) for i in referencing}
        if any(value is None for value in replacements.values()):
            result.skipped += 1
            continue
        size = _size(path)
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("cleanup: could not delete %s: %s", path, exc)
            result.skipped += 1
            continue
        # Update ownership only after the filesystem deletion succeeds.
        for issue in referencing:
            issue.file_path = replacements[issue.id]
            issue.file_source = ""
            issue.file_download_id = None
        result.repointed += len(referencing)
        result.deleted += 1
        result.freed_bytes += size
        log.info("cleanup: deleted %s", path)
    return result
