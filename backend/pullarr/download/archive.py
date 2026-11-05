"""Bound untrusted archive directories, decompression and nested comic packs."""

import os
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
ARCHIVE_EXTS = {".cbz", ".zip", ".cbr", ".rar", ".cb7", ".7z"}
# Enough for large collected editions and 1,000-issue transport packs, while
# bounding directory allocation and work even for tiny highly compressed input.
MAX_EXPANDED_BYTES = 10 * 1024**3
MAX_CONTAINER_BYTES = 12 * 1024**3
MAX_MEMBER_BYTES = 2 * 1024**3
MAX_ARCHIVE_ENTRIES = 20_000
MAX_DIRECTORY_BYTES = 16 * 1024**2
MAX_PACK_ARCHIVES = 1000
ARCHIVE_TIMEOUT = 120
COPY_CHUNK = 1024 * 1024
ZIP_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA}


class ImportValidationError(RuntimeError):
    pass


def check_container_size(path: Path) -> None:
    if path.stat().st_size > MAX_CONTAINER_BYTES:
        raise ImportValidationError("invalid archive: container exceeds the 12 GiB safety limit")


def expansion_limit(path: Path) -> int:
    return min(MAX_EXPANDED_BYTES, max(path.stat().st_size * 100, 512 * 1024**2))


@dataclass
class ArchiveBudget:
    """One cumulative budget shared by every level of a nested pack."""
    limit: int = MAX_EXPANDED_BYTES
    expanded: int = 0
    entries: int = 0
    deadline: float = field(default_factory=lambda: time.monotonic() + ARCHIVE_TIMEOUT)

    def check(self, size: int = 0, entries: int = 0) -> None:
        self.expanded += size
        self.entries += entries
        if self.expanded > self.limit:
            raise ImportValidationError("invalid archive: expanded size exceeds safety limit")
        if self.entries > MAX_ARCHIVE_ENTRIES:
            raise ImportValidationError("invalid archive: too many entries")
        if time.monotonic() > self.deadline:
            raise ImportValidationError("invalid archive: decoding exceeded time limit")


def safe_member_name(name: str) -> bool:
    # Paths are never extracted verbatim, but also must be safe for comic
    # readers that may later extract an imported container.
    if len(name) > 65535:
        return False
    path = PurePosixPath(name.replace("\\", "/"))
    return bool(path.parts) and "\x00" not in name and not path.is_absolute() and ".." not in path.parts and ":" not in path.parts[0]


@contextmanager
def open_zip(path: Path) -> Iterator[zipfile.ZipFile]:
    """Check EOCD/ZIP64 before ZipFile allocates the central directory.

    CPython's EOCD reader reads at most 64 KiB plus fixed ZIP64 headers. Its
    offsets/constants are shared with ZipFile, including concatenated ZIPs.
    """
    check_container_size(Path(path))
    with Path(path).open("rb") as stream:
        end = zipfile._EndRecData(stream)
        if end is None:
            raise zipfile.BadZipFile("missing ZIP directory")
        if (end[zipfile._ECD_SIZE] > MAX_DIRECTORY_BYTES
                or end[zipfile._ECD_ENTRIES_TOTAL] > MAX_ARCHIVE_ENTRIES):
            raise ImportValidationError("invalid archive: ZIP directory exceeds safety limit")
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            # Never trust the EOCD count to describe the directory contents.
            if len(archive.infolist()) > MAX_ARCHIVE_ENTRIES:
                raise ImportValidationError("invalid archive: too many entries")
            yield archive


def checked_zip_infos(archive: zipfile.ZipFile, path: Path, *, skip_metadata: bool = False) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    expanded = 0
    for info in entries:
        if not safe_member_name(info.filename):
            raise ImportValidationError("invalid archive: unsafe member path")
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ImportValidationError("invalid archive: links and special files are not supported")
        # Replacement drops old metadata without decoding it, even when it is
        # oversized or uses a codec unsuitable for the metadata reader.
        if skip_metadata and info.filename.lower() == "comicinfo.xml":
            continue
        if info.flag_bits & 1:
            raise ImportValidationError("invalid archive: encrypted entries are not supported")
        if info.compress_type not in ZIP_METHODS:
            raise ImportValidationError("invalid archive: unsupported ZIP compression")
        if info.file_size > MAX_MEMBER_BYTES or info.compress_size > MAX_MEMBER_BYTES + MAX_DIRECTORY_BYTES:
            raise ImportValidationError("invalid archive: member exceeds safety limit")
        expanded += info.file_size
        if expanded > expansion_limit(path):
            raise ImportValidationError("invalid archive: expanded size exceeds safety limit")
    return entries


def _decoder_blocks(action: str, path: Path, argument: str, budget: ArchiveBudget, limit: int) -> Iterator[bytes]:
    """Stream a codec in a memory/CPU-limited subprocess, with bounded output.

    select+os.read applies the deadline even if a malicious codec emits nothing.
    The process is killed and reaped on every early exit, including reader errors.
    """
    command = [sys.executable, str(Path(__file__).with_name("_archive_worker.py")), action, str(path.resolve()), argument]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    received = 0
    try:
        assert proc.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                budget.check()
                if sys.platform == "darwin":
                    # macOS does not implement RLIMIT_AS. Keep the codec out of
                    # the server and stop it when its resident heap exceeds 1 GiB.
                    usage = subprocess.run(["ps", "-o", "rss=", "-p", str(proc.pid)],
                                           capture_output=True, text=True, timeout=1)
                    if usage.stdout.strip() and int(usage.stdout.strip()) > 1024**2:
                        raise ImportValidationError("invalid archive: decoder memory exceeds safety limit")
                if not selector.select(min(0.25, max(0, budget.deadline - time.monotonic()))):
                    continue
                block = os.read(proc.stdout.fileno(), COPY_CHUNK)
                if not block:
                    break
                received += len(block)
                if received > limit:
                    raise ImportValidationError("invalid archive: decoded output exceeds safety limit")
                yield block
        if proc.wait(timeout=max(0.001, budget.deadline - time.monotonic())):
            raise ImportValidationError("invalid archive: could not be decoded within safety limits")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()


def zip_member_blocks(archive: zipfile.ZipFile, info: zipfile.ZipInfo, path: Path, budget: ArchiveBudget) -> Iterator[bytes]:
    """Use the checked ZipInfo, including duplicate names; never read a page whole."""
    size = 0
    if info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        with archive.open(info) as stream:
            while block := stream.read(COPY_CHUNK):
                size += len(block)
                budget.check(len(block))
                if size > MAX_MEMBER_BYTES or size > info.file_size:
                    raise ImportValidationError("invalid archive: member exceeds safety limit")
                yield block
    else:
        # ZipExtFile's BZIP2/LZMA read(n) can allocate unlimited decoded data
        # internally. Preserve those formats while containing the decoder heap.
        index = archive.infolist().index(info)
        for block in _decoder_blocks("zip", path, str(index), budget, min(MAX_MEMBER_BYTES, info.file_size)):
            size += len(block)
            budget.check(len(block))
            yield block
    if size != info.file_size:
        raise ImportValidationError("invalid archive: incorrect decoded member size")


def copy_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, path: Path, output: BinaryIO, budget: ArchiveBudget) -> None:
    for block in zip_member_blocks(archive, info, path, budget):
        output.write(block)


def validate_archive(path: Path, *, allow_pack: bool = False, _depth: int = 0, _budget: ArchiveBudget | None = None) -> None:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise ImportValidationError(f"invalid archive: {path.name} is empty")
    check_container_size(path)
    budget = _budget or ArchiveBudget()
    try:
        if zipfile.is_zipfile(path):
            with open_zip(path) as archive:
                entries = checked_zip_infos(archive, path)
                budget.check(entries=len(entries))
                images = any(Path(entry.filename).suffix.lower() in IMAGE_EXTS for entry in entries if not entry.is_dir())
                members = [entry for entry in entries if not entry.is_dir() and Path(entry.filename).suffix.lower() in ARCHIVE_EXTS]
                if not images and (not allow_pack or not members):
                    raise ImportValidationError(f"invalid archive: {path.name} contains no images")
                if not images and (_depth >= 2 or len(members) > MAX_PACK_ARCHIVES):
                    raise ImportValidationError("invalid archive pack: too many archives or nesting levels")
                with tempfile.TemporaryDirectory(prefix="pullarr-validate-") as temp:
                    for index, entry in enumerate(entries):
                        if entry.is_dir():
                            continue
                        if not images and entry in members:
                            target = Path(temp) / f"{index}{Path(entry.filename).suffix}"
                            with target.open("wb") as output:
                                copy_zip_member(archive, entry, path, output, budget)
                            validate_archive(target, allow_pack=True, _depth=_depth + 1, _budget=budget)
                            target.unlink()
                        else:
                            for _ in zip_member_blocks(archive, entry, path, budget):
                                pass
            return

        with path.open("rb") as stream:
            magic = stream.read(8)
        if not magic.startswith((b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c")):
            raise ImportValidationError(f"invalid archive: {path.name} is not ZIP, RAR, or 7z")
        decoder = shutil.which("bsdtar")
        if not decoder:
            raise ImportValidationError("archive validation unavailable: install libarchive-tools (bsdtar)")
        listing = b"".join(_decoder_blocks("list", path, decoder, budget, MAX_DIRECTORY_BYTES))
        names = listing.rstrip(b"\n").decode("utf-8", errors="replace").split("\n", MAX_ARCHIVE_ENTRIES)
        budget.check(entries=len(names))
        if not all(safe_member_name(name) for name in names):
            raise ImportValidationError("invalid archive: unsafe member path")
        if not any(Path(name).suffix.lower() in IMAGE_EXTS for name in names):
            raise ImportValidationError(f"invalid archive: {path.name} contains no images")
        for block in _decoder_blocks("decode", path, decoder, budget, expansion_limit(path)):
            budget.check(len(block))
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError, EOFError, zlib.error, subprocess.SubprocessError) as exc:
        if isinstance(exc, ImportValidationError):
            raise
        raise ImportValidationError(f"invalid archive: {path.name} could not be decoded") from exc
