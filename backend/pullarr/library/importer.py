"""Import completed download payloads (DDL staging dirs or torrent data)
into the library.

Handles: single .cbz/.zip/.cbr files, directories of archives, and
directories of loose images (zipped into one CBZ). File→issue matching is
shared with the library scanner via library.matcher. Imported CBZ files get
a ComicInfo.xml injected when they lack one (CBR is left untouched)."""

import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..download.cbz import build_comicinfo, inject_comicinfo
from ..models import Issue, Series
from .matcher import IMAGE_EXTS, MediaFile, find_media_files, match_files
from .naming import issue_filename, range_filename, series_folder, volume_filename

log = logging.getLogger(__name__)


@dataclass
class ImportedFile:
    dest: Path
    issue: Issue | None  # a single-issue file
    volume: int | None  # a whole-volume (TPB) archive
    covered: list[Issue] = field(default_factory=list)  # every issue this file backs


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
    result = match_files(media_files, issues)

    def place(media: MediaFile, issue: Issue | None, volume: int | None,
              covered: list[Issue]) -> None:
        ext = _dest_ext(media)
        if issue is not None:
            dest_name = issue_filename(
                template, series.title, issue.number, issue.title, series.year, ext=ext
            )
        elif media.issue_range is not None:
            lo, hi = media.issue_range
            dest_name = range_filename(series.title, lo, hi, series.year, ext=ext)
        elif volume is not None:
            dest_name = volume_filename(series.title, volume, ext)
        else:
            dest_name = f"{series_folder(series.title, series.year)} - {media.path.stem}{ext}"
        dest = folder / dest_name
        if not dest.exists():
            if media.is_dir:
                _pack_images(media.path, dest)
            elif move:
                shutil.move(str(media.path), dest)
                log.info("Imported (moved) %s -> %s", media.path.name, dest)
            else:
                shutil.copy2(media.path, dest)
                log.info("Imported %s -> %s", media.path.name, dest)
        _stamp_comicinfo(dest, series, issue, volume)
        imported.append(ImportedFile(dest=dest, issue=issue,
                                     volume=volume if issue is None else None,
                                     covered=covered or ([issue] if issue else [])))

    unmatched = list(result.unmatched)
    matched = list(result.matched)
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
        xml = build_comicinfo(
            series=series.title,
            number=issue.number if issue else None,
            volume=volume,
            title=issue.title if issue else "",
            publisher=series.publisher,
            year=series.year,
        )
        if inject_comicinfo(dest, xml):
            log.info("Injected ComicInfo.xml into %s", dest.name)
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
