"""Shared, read-only matching of on-disk files to tracked issues.

Used both by the importer (copying completed downloads into the library) and
the scanner (adopting an existing library in place). Uses filenames and
available ComicInfo metadata."""

from dataclasses import dataclass, field
from pathlib import Path
import re
import stat
from xml.etree.ElementTree import ParseError

from ..download.archive import ARCHIVE_EXTS, IMAGE_EXTS
from ..download.cbz import parse_comicinfo, read_comicinfo_bytes
from ..filename import FilenameCoverage, normalize_issue_label, parse_filename
from ..models import Issue, Series
from ..util import (
    COLLECTED_EDITION,
    additional_editions,
    declared_issue_editions,
    edition_markers,
    filename_edition_markers,
    issue_has_editions,
    normalize_title,
    parse_issue_number,
)

LIBRARY_FILE_EXTS = ARCHIVE_EXTS | {".pdf"}
SIDECAR_IMAGE_STEMS = {
    "backdrop", "background", "banner", "cover", "fanart", "folder", "logo", "poster",
    "series", "thumb", "thumbnail",
}


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
    editions: frozenset[str] = frozenset()

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
    conflicting_pdfs: set[Path] = field(default_factory=set)


def _name_source(path: Path, is_dir: bool) -> str:
    """The text we parse the issue/volume number from."""
    return path.name if is_dir else path.stem


def pdf_file_size(path: Path) -> int | None:
    """Return the size of a regular PDF candidate, including empty files."""
    if path.suffix.lower() != ".pdf":
        return None
    try:
        entry = path.stat()
    except OSError:
        return None
    return entry.st_size if stat.S_ISREG(entry.st_mode) else None


def find_media_files(
    content_path: Path, files: list[Path] | None = None, *,
    file_extensions: set[str] = LIBRARY_FILE_EXTS,
) -> list[MediaFile]:
    """Find comic files and directories that directly hold loose images.

    Existing libraries include PDFs, which are matched by filename. Callers
    can restrict standalone files by extension or pass an explicit file list.
    """
    content_path = Path(content_path)
    media: list[MediaFile] = []

    if files is None and content_path.is_file():
        suffix = content_path.suffix.lower()
        if suffix in file_extensions and (suffix != ".pdf" or pdf_file_size(content_path)):
            media.append(_media_of(content_path, is_dir=False))
        return media

    if files is None and not content_path.is_dir():
        return media

    image_dirs: set[Path] = set()
    media_extensions = file_extensions | IMAGE_EXTS
    for p in sorted(set(files) if files is not None else content_path.rglob("*")):
        suffix = p.suffix.lower()
        if files is not None and suffix in media_extensions and not p.is_file():
            raise FileNotFoundError(f"torrent media file not found: {p}")
        if not p.is_file():
            continue
        if suffix in file_extensions:
            if suffix != ".pdf" or pdf_file_size(p):
                media.append(_media_of(p, is_dir=False))
        elif suffix in IMAGE_EXTS and p.stem.casefold() not in SIDECAR_IMAGE_STEMS:
            image_dirs.add(p.parent)

    for d in sorted(image_dirs):
        media.append(_media_of(d, is_dir=True))
    return media


def _media_of(path: Path, is_dir: bool) -> MediaFile:
    text = _name_source(path, is_dir)
    metadata_series, metadata_number, metadata_volume, metadata_editions = _comicinfo_identity(path, is_dir)
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
        editions=frozenset(filename_edition_markers(text) | metadata_editions),
    )


def _comicinfo_identity(path: Path, is_dir: bool) -> tuple[str, str, int | None, set[str]]:
    if is_dir or path.suffix.lower() not in {".cbz", ".zip"}:
        return "", "", None, set()
    data = read_comicinfo_bytes(path)
    if data is None:
        return "", "", None, set()
    try:
        root = parse_comicinfo(data)
    except ParseError:
        return "", "", None, set()
    series = (root.findtext("Series") or "").strip()
    number = (root.findtext("Number") or "").strip()
    try:
        volume = int(root.findtext("Volume") or "")
    except ValueError:
        volume = None
    editions = edition_markers(series) | declared_issue_editions(
        number, root.findtext("Title") or "", root.findtext("Format") or "",
    )
    return series, number, volume, editions


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


def _pdf_title_matches(prefix: str, series_titles: set[str]) -> bool:
    title = normalize_title(prefix)
    # Filenames such as #001 and Issue #001 rely on their series folder.
    if (title and title in series_titles) or title in {"issue", "no"} or not any(c.isalnum() for c in prefix):
        return True
    # Keep edition labels separate from identity. The matcher still requires
    # the tracked issue to carry the corresponding edition.
    return any(title.startswith(f"{series_title} ")
               and declared_issue_editions("", title[len(series_title) + 1:], "")
               for series_title in series_titles if series_title)


def match_files(media: list[MediaFile], issues: list[Issue], series: Series | None = None) -> MatchResult:
    """Match single issues and collected entries, or use known bundle coverage."""
    series_titles = set()
    if series is not None:
        series_titles = {normalize_title(title) for title in
                         (series.title, *(series.alt_titles or "").splitlines()) if title}
    by_number: dict[float, list[Issue]] = {}
    by_label: dict[str, list[Issue]] = {}
    for c in issues:
        by_number.setdefault(c.number, []).append(c)
        by_label.setdefault(normalize_issue_label(c.display_number or f"{c.number:g}"), []).append(c)
    issues_in_volume: dict[int, list[Issue]] = {}
    for c in issues:
        if c.volume is not None:
            issues_in_volume.setdefault(c.volume, []).append(c)

    matched: list[MatchedFile] = []
    unmatched: list[MediaFile] = []
    conflicting_pdfs: set[Path] = set()
    for mf in media:
        if mf.coverage and mf.coverage.ambiguous:
            unmatched.append(mf)
            continue
        if (series is not None and mf.coverage and not mf.is_dir and mf.path.suffix.lower() == ".pdf"
                and not _pdf_title_matches(mf.coverage.title_prefix, series_titles)):
            unmatched.append(mf)
            if mf.coverage.issue or mf.coverage.volume:
                conflicting_pdfs.add(mf.path)
            continue
        editions = additional_editions(mf.editions, series, len(issues))
        whole_volume = bool(mf.coverage and mf.coverage.volume and not mf.coverage.issue)
        if mf.issue_range or whole_volume:
            # A collected archive with explicit coverage can provide regular issues.
            editions.discard(COLLECTED_EDITION)
        # Display labels are identities, not numeric sort keys. ComicVine can
        # assign 1A a synthetic number when plain #1 also exists. Resolve the
        # exact label across all rows before considering numeric candidates,
        # while keeping a collection's first-page metadata out of span matching.
        label = ""
        if whole_volume and (volume := mf.volume_number) is not None and volume not in issues_in_volume:
            # Some series track trades as individual entries. Require a
            # matching ComicInfo number and an explicitly collected entry.
            if (normalize_issue_label(mf.metadata_number) == str(volume)
                    and (not mf.metadata_series or normalize_title(mf.metadata_series) in series_titles)):
                collected = [i for i in by_label.get(str(volume), [])
                             if COLLECTED_EDITION in declared_issue_editions(
                                 i.display_number or "", i.title or "", i.format or "",
                             ) and issue_has_editions(i, editions)]
                if len(collected) == 1:
                    matched.append(MatchedFile(mf, collected[0], None, collected))
                    continue
        elif not mf.issue_range and not mf.volume_range and not whole_volume:
            filename_label = mf.coverage.issue_label if mf.coverage else ""
            if mf.metadata_number:
                label = normalize_issue_label(mf.metadata_number)
            elif filename_label and re.fullmatch(r"\d+(?:\.\d+)?", filename_label) is None:
                label = filename_label
        exact = [i for i in by_label.get(label, []) if issue_has_editions(i, editions)]
        if len(exact) == 1:
            issue = exact[0]
        elif exact or (label and re.fullmatch(r"\d+(?:\.\d+)?", label) is None):
            # Neither ambiguous identity nor an untracked variant can prove a
            # plain issue or an entire volume, including metadata-only labels.
            unmatched.append(mf)
            continue
        else:
            candidates = [i for i in by_number.get(mf.issue_number, []) if issue_has_editions(i, editions)]
            issue = _pick_issue(mf, candidates)
        if issue is not None:
            matched.append(MatchedFile(media=mf, issue=issue, volume=None,
                                       covered_issues=[issue]))
        elif editions:
            unmatched.append(mf)
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
    return MatchResult(matched=matched, unmatched=unmatched, conflicting_pdfs=conflicting_pdfs)
