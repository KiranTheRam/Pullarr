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
from pathlib import Path

from ..download.cbz import build_comicinfo, upsert_comicinfo
from ..models import Issue, Series
from ..util import normalize_title, strip_issue_suffix
from .matcher import ARCHIVE_EXTS, IMAGE_EXTS, MediaFile, find_media_files, match_files
from .naming import issue_filename, range_filename, series_folder, volume_filename

log = logging.getLogger(__name__)


@dataclass
class ImportedFile:
    dest: Path
    issue: Issue | None  # a single-issue file
    volume: int | None  # a whole-volume (TPB) archive
    covered: list[Issue] = field(default_factory=list)  # every issue this file backs
    status: str = "imported"  # imported | duplicate | unmatched


class ImportValidationError(RuntimeError):
    pass


MAX_PACK_ARCHIVES = 1000
MAX_PACK_EXPANDED_BYTES = 10 * 1024**3


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_hash(path: Path) -> str:
    """Compare archive payloads while ignoring generated ComicInfo metadata."""
    if not zipfile.is_zipfile(path):
        return _sha256(path)
    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as zf:
        for name in sorted(n for n in zf.namelist() if n.lower() != "comicinfo.xml"):
            digest.update(name.encode("utf-8", errors="surrogateescape"))
            digest.update(zf.read(name))
    return digest.hexdigest()


def validate_archive(path: Path) -> None:
    """Reject empty/corrupt ZIP payloads before they become library state."""
    if not path.is_file() or path.stat().st_size == 0:
        raise ImportValidationError(f"invalid archive: {path.name} is empty")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            corrupt = zf.testzip()
            if corrupt:
                raise ImportValidationError(f"corrupt ZIP member: {corrupt}")
            images = [n for n in zf.namelist() if Path(n).suffix.lower() in IMAGE_EXTS]
            if not images:
                raise ImportValidationError(f"invalid archive: {path.name} contains no images")


def _nested_archive_members(path: Path) -> list[zipfile.ZipInfo]:
    """Return comic archives inside an outer ZIP pack with no page images."""
    if not zipfile.is_zipfile(path):
        return []
    with zipfile.ZipFile(path) as zf:
        corrupt = zf.testzip()
        if corrupt:
            raise ImportValidationError(f"corrupt ZIP member: {corrupt}")
        infos = [info for info in zf.infolist() if not info.is_dir()]
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
    proportional_limit = max(path.stat().st_size * 4, 512 * 1024**2)
    if expanded > min(proportional_limit, MAX_PACK_EXPANDED_BYTES):
        raise ImportValidationError(
            f"invalid archive pack: {path.name} expands beyond the safety limit"
        )
    return nested


def _extract_nested_archives(
    pack: Path, members: list[zipfile.ZipInfo], destination: Path
) -> None:
    """Safely flatten nested comic archives without trusting ZIP paths."""
    destination.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    with zipfile.ZipFile(pack) as zf:
        for index, info in enumerate(members, start=1):
            name = Path(info.filename).name
            if not name:
                continue
            candidate = name
            if candidate.casefold() in used:
                candidate = f"{Path(name).stem}-{index}{Path(name).suffix}"
            used.add(candidate.casefold())
            target = destination / candidate
            with zf.open(info) as source, open(target, "xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
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


def import_payload(
    content_path: Path,
    series: Series,
    issues: list[Issue],
    library_root: Path,
    template: str,
    force_issue: Issue | None = None,
    move: bool = False,
    _pack_depth: int = 0,
) -> list[ImportedFile]:
    """Copies (or, with move=True, moves) payload files into the library,
    named by convention. Returns an ImportedFile per placed file, each with the
    issue(s) it backs — one for a single issue, several for a multi-issue
    bundle ("#1-3") or a whole-volume (TPB) archive.

    force_issue: when the payload is a single file that matches nothing by
    name (the release was grabbed for a specific issue), import it as that
    issue anyway."""
    folder = library_root / (
        series.folder_name or series_folder(series.title, series.year)
    )
    folder.mkdir(parents=True, exist_ok=True)
    imported: list[ImportedFile] = []
    media_files = find_media_files(content_path)

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
                _extract_nested_archives(outer.path, members, expanded)
                return import_payload(
                    expanded, series, issues, library_root, template,
                    force_issue=force_issue, move=move, _pack_depth=_pack_depth + 1,
                )

    result = match_files(media_files, issues)

    def place(media: MediaFile, issue: Issue | None, volume: int | None,
              covered: list[Issue]) -> None:
        ext = _dest_ext(media)
        if issue is not None:
            dest_name = issue_filename(
                template, series.title, issue.display_number or issue.number,
                issue.title, series.year, ext=ext
            )
        elif media.issue_range is not None:
            lo, hi = media.issue_range
            dest_name = range_filename(series.title, lo, hi, series.year, ext=ext)
        elif volume is not None:
            dest_name = volume_filename(series.title, volume, ext)
        else:
            dest_name = f"{series_folder(series.title, series.year)} - {media.path.stem}{ext}"
        dest = folder / dest_name
        tmp = dest.with_suffix(dest.suffix + ".importing")
        tmp.unlink(missing_ok=True)
        moved = False
        try:
            if media.is_dir:
                _pack_images(media.path, tmp)
            elif move:
                shutil.move(str(media.path), tmp)
                moved = True
            else:
                shutil.copy2(media.path, tmp)
            validate_archive(tmp)
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
                tmp.replace(dest)
                log.info("Imported %s -> %s", media.path.name, dest)
        except Exception:
            if moved and tmp.exists() and not media.path.exists():
                shutil.move(str(tmp), media.path)
            else:
                tmp.unlink(missing_ok=True)
            raise
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
    if (
        force_issue is not None
        and len(media_files) == 1
        # only when the single file matched nothing at all (no issue, no bundle
        # coverage) — a "#1-3" file that covers issues must NOT be forced to one
        and not any(mf.issue is not None or mf.covered_issues for mf in matched)
    ):
        only = media_files[0]
        matched = [m for m in matched if m.media is not only]
        unmatched = [m for m in unmatched if m is not only]
        place(only, force_issue, None, [force_issue])

    for mf in matched:
        place(mf.media, mf.issue, mf.volume, mf.covered_issues)
    for media in unmatched:
        place(media, None, None, [])
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
    except (OSError, zipfile.BadZipFile) as exc:
        log.warning("could not inject ComicInfo.xml into %s: %s", dest, exc)


def _pack_images(img_dir: Path, dest: Path) -> None:
    images = sorted(
        p for p in img_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    )
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
        for img in images:
            zf.write(img, img.name)
    log.info("Packed %s (%d images) -> %s", img_dir, len(images), dest)
