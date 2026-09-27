"""Validate comic containers before accepting a download or changing ownership."""

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
ARCHIVE_EXTS = {".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z"}
MAX_EXPANDED_BYTES = 10 * 1024**3


class ImportValidationError(RuntimeError):
    pass


def validate_archive(path: Path, *, allow_pack: bool = False, _depth: int = 0) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ImportValidationError(f"invalid archive: {path.name} is empty")
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                entries = [entry for entry in archive.infolist() if not entry.is_dir()]
                expanded = sum(entry.file_size for entry in entries)
                if expanded > min(MAX_EXPANDED_BYTES, max(path.stat().st_size * 100, 512 * 1024**2)):
                    raise ImportValidationError("invalid archive: expanded size exceeds safety limit")
                if archive.testzip():
                    raise ImportValidationError(f"corrupt archive: {path.name}")
                if any(Path(entry.filename).suffix.lower() in IMAGE_EXTS for entry in entries):
                    return
                members = [entry for entry in entries if Path(entry.filename).suffix.lower() in ARCHIVE_EXTS]
                if not allow_pack or not members:
                    raise ImportValidationError(f"invalid archive: {path.name} contains no images")
                if _depth >= 2 or len(members) > 1000:
                    raise ImportValidationError("invalid archive pack: too many archives or nesting levels")
                with tempfile.TemporaryDirectory(prefix="pullarr-validate-") as temp:
                    for index, entry in enumerate(members):
                        target = Path(temp) / f"{index}{Path(entry.filename).suffix}"
                        with archive.open(entry) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
                        validate_archive(target, allow_pack=True, _depth=_depth + 1)
                        target.unlink()
            return

        # RAR/7z must pass libarchive's decoder, not just a magic-byte check.
        # bsdtar writes decoded data to /dev/null; nothing is extracted on disk.
        with path.open("rb") as stream:
            magic = stream.read(8)
        if not magic.startswith((b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c")):
            raise ImportValidationError(f"invalid archive: {path.name} is not ZIP, RAR, or 7z")
        decoder = shutil.which("bsdtar")
        if not decoder:
            raise ImportValidationError("archive validation unavailable: install libarchive-tools (bsdtar)")
        names = subprocess.run(
            [decoder, "-tf", str(path.resolve())], check=True, capture_output=True,
            text=True, errors="replace", timeout=120, stdin=subprocess.DEVNULL,
        ).stdout.splitlines()
        if not any(Path(name).suffix.lower() in IMAGE_EXTS for name in names):
            raise ImportValidationError(f"invalid archive: {path.name} contains no images")
        subprocess.run(
            [decoder, "-xOf", str(path.resolve())], check=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, timeout=120, stdin=subprocess.DEVNULL,
        )
    except (zipfile.BadZipFile, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        if isinstance(exc, ImportValidationError):
            raise
        raise ImportValidationError(f"invalid archive: {path.name} could not be decoded") from exc
