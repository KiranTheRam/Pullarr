"""Shared, read-only matching of on-disk files to tracked issues.

Used both by the importer (copying completed downloads into the library) and
the scanner (adopting an existing library in place). Filename-based only — it
never opens or writes files."""

from dataclasses import dataclass, field
from pathlib import Path
import re
from xml.etree.ElementTree import ParseError, fromstring

from ..download.cbz import read_comicinfo_bytes
from ..filename import FilenameCoverage, parse_filename
from ..models import Issue
from ..util import (
    normalize_title,
    parse_issue_number,
)

ARCHIVE_EXTS = {".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}


@dataclass
class MediaFile:
    path: Path
    is_dir: bool  # a directory of loose images (one issue/volume as pages)
    issue_number: float | None
    volume_number: int | None
    # a multi-issue bundle in one file ("#1-3" / "001-003"), inclusive
    issue_range: tuple[float, float] | None = None
    metadata_series: str = ""
    metadata_number: str = ""
    metadata_volume: int | None = None
    coverage: FilenameCoverage | None = None
    volume_range: tuple[int, int] | None = None

    @property
    def label(self) -> str:
        return self.path.name


@dataclass
class MatchedFile:
    media: MediaFile
    issue: Issue | None  # the single issue this file is, if any
    volume: int | None  # set when the file is a whole-volume archive
    covered_issues: list[Issue] = field(default_factory=list)


@dataclass
class MatchResult:
    matched: list[MatchedFile]
    unmatched: list[MediaFile]


def _name_source(path: Path, is_dir: bool) -> str:
    """The text we parse the issue/volume number from."""
    return path.name if is_dir else path.stem


def find_media_files(content_path: Path, files: list[Path] | None = None) -> list[MediaFile]:
    """Archives anywhere under content_path, plus directories that directly
    hold loose images. When files is supplied, inspect only those paths rather
    than walking the containing folder. Non-media sidecars are ignored."""
    content_path = Path(content_path)
    media: list[MediaFile] = []

    if files is None and content_path.is_file():
        if content_path.suffix.lower() in ARCHIVE_EXTS:
            media.append(_media_of(content_path, is_dir=False))
        return media

    if files is None and not content_path.is_dir():
        return media

    image_dirs: set[Path] = set()
    for p in sorted(set(files) if files is not None else content_path.rglob("*")):
        if files is not None and p.suffix.lower() in ARCHIVE_EXTS | IMAGE_EXTS and not p.is_file():
            raise FileNotFoundError(f"torrent media file not found: {p}")
        if not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix in ARCHIVE_EXTS:
            media.append(_media_of(p, is_dir=False))
        elif suffix in IMAGE_EXTS:
            image_dirs.add(p.parent)

    for d in sorted(image_dirs):
        media.append(_media_of(d, is_dir=True))
    return media


def _media_of(path: Path, is_dir: bool) -> MediaFile:
    text = _name_source(path, is_dir)
    metadata_series, metadata_number, metadata_volume = _comicinfo_identity(path, is_dir)
    coverage = parse_filename(text)
    volume = coverage.volume_number
    issue = coverage.issue_number
    issue_range = (coverage.issue if coverage.issue and coverage.issue[0] != coverage.issue[1]
                   and not coverage.ambiguous else None)
    volume_range = (coverage.volume if coverage.volume and coverage.volume[0] != coverage.volume[1]
                    and not coverage.ambiguous else None)
    # A single ComicInfo Number often describes only the first issue of a
    # collection. It cannot narrow an explicit span or collected-volume name.
    if not coverage.ambiguous and not coverage.is_range and not (coverage.volume and not coverage.issue):
        if metadata_volume is not None:
            volume = metadata_volume
        if metadata_number:
            try:
                issue = float(metadata_number)
            except ValueError:
                parsed_metadata = parse_issue_number(f"#{metadata_number}")
                if parsed_metadata is not None:
                    issue = parsed_metadata
    return MediaFile(
        path=path, is_dir=is_dir, issue_number=issue,
        volume_number=volume, issue_range=issue_range,
        metadata_series=metadata_series, metadata_number=metadata_number,
        metadata_volume=metadata_volume, coverage=coverage, volume_range=volume_range,
    )


def _comicinfo_identity(path: Path, is_dir: bool) -> tuple[str, str, int | None]:
    if is_dir or path.suffix.lower() not in {".cbz", ".zip"}:
        return "", "", None
    data = read_comicinfo_bytes(path)
    if data is None:
        return "", "", None
    try:
        root = fromstring(data)
    except ParseError:
        return "", "", None
    series = (root.findtext("Series") or "").strip()
    number = (root.findtext("Number") or "").strip()
    try:
        volume = int(root.findtext("Volume") or "")
    except ValueError:
        volume = None
    return series, number, volume


def _plain_display_number(issue: Issue) -> bool:
    raw = (issue.display_number or f"{issue.number:g}").strip()
    return raw == "½" or re.fullmatch(r"\d+(?:\.\d+)?", raw) is not None


def _pick_issue(mf: MediaFile, candidates: list[Issue]) -> Issue | None:
    if mf.metadata_number:
        exact = [
            issue for issue in candidates
            if (issue.display_number or f"{issue.number:g}").strip().casefold()
            == mf.metadata_number.strip().casefold()
        ]
        if len(exact) == 1:
            return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None
    stem = normalize_title(_name_source(mf.path, mf.is_dir))
    matched = [
        issue for issue in candidates
        if issue.display_number
        and not _plain_display_number(issue)
        and normalize_title(issue.display_number) in stem
    ]
    return matched[0] if len(matched) == 1 else None


def match_files(media: list[MediaFile], issues: list[Issue]) -> MatchResult:
    """Match each media file to a issue (by number) or, for whole-volume
    archives, to every issue assigned to that volume."""
    by_number: dict[float, list[Issue]] = {}
    for c in issues:
        by_number.setdefault(c.number, []).append(c)
    issues_in_volume: dict[int, list[Issue]] = {}
    for c in issues:
        if c.volume is not None:
            issues_in_volume.setdefault(c.volume, []).append(c)

    matched: list[MatchedFile] = []
    unmatched: list[MediaFile] = []
    for mf in media:
        if mf.coverage and mf.coverage.ambiguous:
            unmatched.append(mf)
            continue
        issue = (
            _pick_issue(mf, by_number.get(mf.issue_number, []))
            if mf.issue_number is not None
            else None
        )
        if issue is not None:
            matched.append(MatchedFile(media=mf, issue=issue, volume=None,
                                       covered_issues=[issue]))
        elif mf.issue_range is not None:
            # a multi-issue bundle covers every tracked issue in its span
            lo, hi = mf.issue_range
            covered = [c for c in issues if lo <= c.number <= hi and _plain_display_number(c)]
            matched.append(MatchedFile(media=mf, issue=None, volume=None,
                                       covered_issues=covered))
        elif mf.issue_number is not None:
            # An explicit issue that is not tracked is not a whole volume.
            unmatched.append(mf)
        elif mf.volume_range is not None:
            lo, hi = mf.volume_range
            covered = [c for c in issues if c.volume is not None and lo <= c.volume <= hi]
            matched.append(MatchedFile(media=mf, issue=None, volume=None,
                                       covered_issues=covered))
        elif mf.volume_number is not None and mf.volume_number in issues_in_volume:
            covered = issues_in_volume[mf.volume_number]
            matched.append(MatchedFile(media=mf, issue=None, volume=mf.volume_number,
                                       covered_issues=list(covered)))
        elif mf.volume_number is not None:
            # a volume archive for a volume we don't have issue rows for yet;
            # keep the volume tag so callers can still name it, but no coverage
            matched.append(MatchedFile(media=mf, issue=None, volume=mf.volume_number,
                                       covered_issues=[]))
        else:
            unmatched.append(mf)
    return MatchResult(matched=matched, unmatched=unmatched)
