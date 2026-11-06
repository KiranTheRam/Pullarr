"""Import completed download payloads (DDL staging dirs or torrent data)
into the library.

Handles: single .cbz/.zip/.cbr files, directories of archives, and
directories of loose images (zipped into one CBZ). File→issue matching is
shared with the library scanner via library.matcher. Imported CBZ files get
a ComicInfo.xml injected when they lack one (CBR is left untouched)."""

import logging
import hashlib
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from collections.abc import Callable
from pathlib import Path

from ..download.cbz import build_comicinfo, upsert_comicinfo
from ..download.archive import (ArchiveBudget, ImportValidationError, MAX_ARCHIVE_ENTRIES,
                                MAX_CONTAINER_BYTES, MAX_EXPANDED_BYTES, MAX_MEMBER_BYTES, MAX_PACK_ARCHIVES,
                                check_container_size, checked_zip_infos, copy_zip_member, expansion_limit,
                                open_zip, validate_archive, zip_member_blocks)
from ..models import Issue, Series
from ..util import normalize_title, strip_issue_suffix
from .matcher import ARCHIVE_EXTS, IMAGE_EXTS, MediaFile, find_media_files, match_files
from .naming import (issue_filename, range_filename, series_folder, unmatched_filename,
                     volume_filename, volume_range_filename)
from .scanner import recorded_files_expect_content, require_root_available

log = logging.getLogger(__name__)


@dataclass
class ImportedFile:
    dest: Path
    issue: Issue | None  # a single-issue file
    volume: int | None  # a whole-volume (TPB) archive
    covered: list[Issue] = field(default_factory=list)  # every issue this file backs
    status: str = "imported"  # imported | duplicate | unmatched


MAX_PACK_EXPANDED_BYTES = MAX_EXPANDED_BYTES


def _sha256(path: Path) -> str:
    check_container_size(path)
    budget = ArchiveBudget(limit=MAX_CONTAINER_BYTES)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            budget.check(len(block))
            digest.update(block)
    return digest.hexdigest()


def _content_hash(path: Path) -> str:
    """Compare archive payloads while ignoring generated ComicInfo metadata."""
    if not zipfile.is_zipfile(path):
        return _sha256(path)
    digest = hashlib.sha256()
    with open_zip(path) as zf:
        entries = checked_zip_infos(zf, path, skip_metadata=True)
        budget = ArchiveBudget(limit=expansion_limit(path))
        for info in sorted(entries, key=lambda entry: entry.filename):
            if info.filename.lower() == "comicinfo.xml":
                continue
            digest.update(info.filename.encode("utf-8", errors="surrogateescape"))
            for block in zip_member_blocks(zf, info, path, budget):
                digest.update(block)
    return digest.hexdigest()


def _nested_archive_members(path: Path) -> list[zipfile.ZipInfo]:
    """Return comic archives inside an outer ZIP pack with no page images."""
    if not zipfile.is_zipfile(path):
        return []
    with open_zip(path) as zf:
        infos = [info for info in checked_zip_infos(zf, path) if not info.is_dir()]
        if any(Path(info.filename).suffix.lower() in IMAGE_EXTS for info in infos):
            return []
        nested = [
            info for info in infos
            if Path(info.filename).suffix.lower() in ARCHIVE_EXTS
        ]
    if not nested:
        return []
    if len(nested) > MAX_PACK_ARCHIVES:
        raise ImportValidationError(
            f"invalid archive pack: {path.name} contains too many nested archives"
        )
    expanded = sum(info.file_size for info in nested)
    proportional_limit = expansion_limit(path)
    if expanded > min(proportional_limit, MAX_PACK_EXPANDED_BYTES):
        raise ImportValidationError(
            f"invalid archive pack: {path.name} expands beyond the safety limit"
        )
    return nested


def _extract_nested_archives(
    pack: Path, members: list[zipfile.ZipInfo], destination: Path, budget: ArchiveBudget | None = None
) -> None:
    """Safely flatten nested comic archives without trusting ZIP paths."""
    destination.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    budget = budget or ArchiveBudget(limit=expansion_limit(pack))
    with open_zip(pack) as zf:
        infos = checked_zip_infos(zf, pack)
        budget.check(entries=len(infos))
        # Check ignored sidecars too; a corrupt pack must fail before placing files.
        offsets = {info.header_offset for info in members}
        for info in infos:
            if info.header_offset not in offsets and not info.is_dir():
                for _ in zip_member_blocks(zf, info, pack, budget):
                    pass
        for index, original in enumerate(members, start=1):
            info = next(info for info in infos if info.header_offset == original.header_offset)
            name = Path(info.filename).name
            if not name:
                continue
            candidate = name
            if candidate.casefold() in used:
                candidate = f"{Path(name).stem}-{index}{Path(name).suffix}"
            used.add(candidate.casefold())
            target = destination / candidate
            with open(target, "xb") as output:
                copy_zip_member(zf, info, pack, output, budget)
    log.info("Expanded %d nested comic archives from %s", len(members), pack.name)


def _media_matches_series(media: MediaFile, series: Series) -> bool:
    titles = {
        normalize_title(title)
        for title in [series.title, *(series.alt_titles or "").split("\n")]
        if normalize_title(title)
    }
    embedded = normalize_title(media.metadata_series) if media.metadata_series else ""
    parsed = normalize_title(strip_issue_suffix(media.path.stem))
    return embedded in titles if embedded else parsed in titles


def _dest_ext(media: MediaFile) -> str:
    """Target extension: pack loose images to .cbz, else preserve the archive
    format (normalizing container synonyms)."""
    if media.is_dir:
        return ".cbz"
    return {".zip": ".cbz", ".rar": ".cbr", ".7z": ".cb7"}.get(
        media.path.suffix.lower(), media.path.suffix.lower()
    )


def _preflight_destinations(
    planned: list[tuple[MediaFile, Issue | None, int | None, list[Issue], Path]],
    issues: list[Issue],
    files: list[Path] | None,
) -> None:
    """Refuse an ambiguous destination before placing any part of the payload."""
    holders: dict[Path, list[Issue]] = {}
    for issue in issues:
        if issue.file_path:
            holders.setdefault(Path(issue.file_path).resolve(), []).append(issue)
    claimed: set[Path] = set()
    for media, issue, volume, covered, dest in planned:
        key = dest.resolve()
        if key in claimed:
            raise ImportValidationError(
                f"import collision: multiple payload files would both be saved as {dest.name}; "
                "use a naming template that keeps issue numbers distinct"
            )
        claimed.add(key)
        owners = covered or ([issue] if issue is not None else [])
        for holder in holders.get(key, []):
            if holder not in owners:
                number = holder.display_number or f"{holder.number:g}"
                raise ImportValidationError(
                    f"import collision: {dest.name} already holds issue {number}; "
                    "use a naming template that keeps issue numbers distinct"
                )
        if not dest.exists():
            continue
        if not dest.is_file():
            raise ImportValidationError(f"import collision: {dest.name} is not a file")
        if media.is_dir:
            # Compare only the selected torrent pages, including when another
            # torrent has pages in the same directory.
            with tempfile.TemporaryDirectory(prefix="pullarr-preflight-") as tmp:
                packed = Path(tmp) / "pages.cbz"
                _pack_images(media.path, packed, files)
                same = _content_hash(packed) == _content_hash(dest)
        else:
            same = _content_hash(media.path) == _content_hash(dest)
        if not same:
            raise ImportValidationError(
                f"import collision: {dest.name} already exists with different content"
            )


def import_payload(
    content_path: Path,
    series: Series,
    issues: list[Issue],
    library_root: Path,
    template: str,
    force_issue: Issue | None = None,
    move: bool = False,
    _pack_depth: int = 0,
    check_library: Callable[[], None] | None = None,
    files: list[Path] | None = None,
    _archive_budget: ArchiveBudget | None = None,
) -> list[ImportedFile]:
    """Copies (or, with move=True, moves) payload files into the library,
    named by convention. Returns an ImportedFile per placed file, each with the
    issue(s) it backs — one for a single issue, several for a multi-issue
    bundle ("#1-3") or a whole-volume (TPB) archive.

    force_issue: when the payload is a single file that matches nothing by
    name (the release was grabbed for a specific issue), import it as that
    issue anyway. files restricts a torrent import to its explicitly listed
    payload; loose-image directories are packed from those files only."""
    archive_budget = _archive_budget or ArchiveBudget()
    library_root = Path(library_root)
    if check_library is None:
        expect_content = recorded_files_expect_content(library_root, (issue.file_path for issue in issues if issue.downloaded))
        check_library = lambda: require_root_available(library_root, expect_content)
    check_library()
    folder = library_root / (
        series.folder_name or series_folder(series.title, series.year)
    )
    # Create only children, never the configured mount point or its parents.
    # A disappearance after the check must fail instead of rebuilding the root.
    if library_root not in folder.parents:
        raise ImportValidationError("invalid series folder outside library root")
    missing = []
    current = folder
    while current != library_root and not current.exists():
        missing.append(current)
        current = current.parent
    for child in reversed(missing):
        check_library()
        child.mkdir(exist_ok=True)
    imported: list[ImportedFile] = []
    media_files = find_media_files(content_path, files=files)

    # GetComics collection downloads are often an outer ZIP containing one
    # CBR/CBZ per issue. Expand that transport container before matching; it
    # is not itself a CBZ and therefore should never be forced onto issue #1.
    if len(media_files) == 1:
        outer = media_files[0]
        members = _nested_archive_members(outer.path)
        if members:
            if _pack_depth >= 2:
                raise ImportValidationError("invalid archive pack: nesting is too deep")
            temp_parent = content_path if content_path.is_dir() else content_path.parent
            with tempfile.TemporaryDirectory(prefix=".pullarr-pack-", dir=temp_parent) as tmp:
                expanded = Path(tmp)
                _extract_nested_archives(outer.path, members, expanded, archive_budget)
                return import_payload(
                    expanded, series, issues, library_root, template,
                    force_issue=force_issue, move=move, _pack_depth=_pack_depth + 1,
                    check_library=check_library, _archive_budget=archive_budget,
                )

    result = match_files(media_files, issues)

    def destination(media: MediaFile, issue: Issue | None, volume: int | None) -> Path:
        ext = _dest_ext(media)
        if issue is not None:
            dest_name = issue_filename(
                template, series.title, issue.display_number or issue.number,
                issue.title, series.year, ext=ext
            )
        elif media.issue_range is not None:
            lo, hi = media.issue_range
            dest_name = range_filename(series.title, lo, hi, series.year, ext=ext)
        elif media.volume_range is not None:
            lo, hi = media.volume_range
            dest_name = volume_range_filename(series.title, lo, hi, ext)
        elif volume is not None:
            dest_name = volume_filename(series.title, volume, ext)
        else:
            dest_name = unmatched_filename(series.title, media.path.stem, series.year, ext)
        return folder / dest_name

    def place(media: MediaFile, issue: Issue | None, volume: int | None,
              covered: list[Issue], dest: Path) -> None:
        check_library()
        tmp = dest.with_suffix(dest.suffix + ".importing")
        tmp.unlink(missing_ok=True)
        moved = False
        if not media.is_dir:
            check_container_size(media.path)
        try:
            if media.is_dir:
                _pack_images(media.path, tmp, files)
            elif move:
                shutil.move(str(media.path), tmp)
                moved = True
            else:
                shutil.copy2(media.path, tmp)
            check_library()
            validate_archive(tmp, _budget=archive_budget)
            status = "imported"
            if dest.exists():
                if _content_hash(tmp) == _content_hash(dest):
                    status = "duplicate"
                    tmp.unlink(missing_ok=True)
                else:
                    raise ImportValidationError(
                        f"import collision: {dest.name} already exists with different content"
                    )
            else:
                check_library()
                tmp.replace(dest)
                log.info("Imported %s -> %s", media.path.name, dest)
        except Exception:
            if moved and tmp.exists() and not media.path.exists():
                shutil.move(str(tmp), media.path)
            else:
                tmp.unlink(missing_ok=True)
            raise
        check_library()
        _stamp_comicinfo(dest, series, issue, volume)
        imported.append(ImportedFile(dest=dest, issue=issue,
                                     volume=volume if issue is None else None,
                                     covered=covered or ([issue] if issue else []),
                                     status=status if (covered or issue or volume is not None)
                                     else "unmatched"))

    unmatched = list(result.unmatched)
    matched = list(result.matched)

    # Packs sometimes include previews or extras whose trailing number is the
    # same as a real issue. Prefer the archive whose parsed series title
    # matches, and retain the other file as an unmatched extra rather than
    # failing the entire pack with a destination collision.
    claimed_issues: set[int] = set()
    unique_matched = []
    for mf in sorted(matched, key=lambda item: not _media_matches_series(item.media, series)):
        if mf.issue is not None and mf.issue.id in claimed_issues:
            unmatched.append(mf.media)
            continue
        if mf.issue is not None:
            claimed_issues.add(mf.issue.id)
        unique_matched.append(mf)
    matched = unique_matched
    planned = []
    if (
        force_issue is not None
        and len(media_files) == 1
        and not (media_files[0].coverage and (
            media_files[0].coverage.ambiguous or media_files[0].coverage.is_range
            or media_files[0].coverage.volume
        ))
        # only when the single file matched nothing at all (no issue, no bundle
        # coverage) — a "#1-3" file that covers issues must NOT be forced to one
        and not any(mf.issue is not None or mf.covered_issues for mf in matched)
    ):
        only = media_files[0]
        matched = [m for m in matched if m.media is not only]
        unmatched = [m for m in unmatched if m is not only]
        planned.append((only, force_issue, None, [force_issue], destination(only, force_issue, None)))

    for mf in matched:
        planned.append((mf.media, mf.issue, mf.volume, mf.covered_issues,
                        destination(mf.media, mf.issue, mf.volume)))
    for media in unmatched:
        planned.append((media, None, None, [], destination(media, None, None)))
    _preflight_destinations(planned, issues, files)
    for plan in planned:
        place(*plan)
    return imported


def _stamp_comicinfo(dest: Path, series: Series, issue: Issue | None, volume: int | None) -> None:
    try:
        released = issue.released_at if issue else None
        web = issue.web_url if issue else ""
        if issue and not web and issue.comicvine_id:
            web = f"https://comicvine.gamespot.com/issue/4000-{issue.comicvine_id}/"
        xml = build_comicinfo(
            series=series.title,
            number=(issue.display_number or issue.number) if issue else None,
            volume=volume,
            title=issue.title if issue else "",
            summary=issue.summary if issue else series.description,
            publisher=series.publisher,
            imprint=issue.imprint if issue else "",
            year=released.year if released else series.year,
            month=released.month if released else None,
            day=released.day if released else None,
            count=series.total_issues,
            web=web,
            page_count=issue.page_count if issue else None,
            writer=issue.writers if issue else "",
            penciller=issue.pencillers if issue else "",
            inker=issue.inkers if issue else "",
            colorist=issue.colorists if issue else "",
            letterer=issue.letterers if issue else "",
            cover_artist=issue.cover_artists if issue else "",
            editor=issue.editors if issue else "",
            translator=issue.translators if issue else "",
            genre=(issue.genres if issue and issue.genres else series.genres),
            story_arc=issue.story_arcs if issue else "",
            characters=issue.characters if issue else "",
            teams=issue.teams if issue else "",
            format=issue.format if issue else "",
            language=issue.language if issue else "",
        )
        if upsert_comicinfo(dest, xml):
            log.info("Updated ComicInfo.xml in %s", dest.name)
    except (OSError, zipfile.BadZipFile, ImportValidationError) as exc:
        log.warning("could not inject ComicInfo.xml into %s: %s", dest, exc)


def _pack_images(img_dir: Path, dest: Path, files: list[Path] | None = None) -> None:
    images = sorted(
        p for p in (files if files is not None else img_dir.iterdir())
        if p.parent == img_dir and p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    if len(images) >= MAX_ARCHIVE_ENTRIES:
        raise ImportValidationError("too many loose comic pages")
    total = 0
    for img in images:
        size = img.stat().st_size
        total += size
        if size > MAX_MEMBER_BYTES or total > MAX_EXPANDED_BYTES:
            raise ImportValidationError("loose comic pages exceed archive safety limits")
    budget = ArchiveBudget()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
        for img in images:
            size = 0
            with img.open("rb") as source, zf.open(img.name, "w", force_zip64=True) as output:
                while block := source.read(1024 * 1024):
                    size += len(block)
                    budget.check(len(block))
                    if size > MAX_MEMBER_BYTES:
                        raise ImportValidationError("loose comic page exceeds archive safety limit")
                    output.write(block)
    log.info("Packed %s (%d images) -> %s", img_dir, len(images), dest)
