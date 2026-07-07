"""Direct-download worker: stream release files (CBR/CBZ/ZIP packs) from a
DDL source into a staging directory, ready for the shared archive importer."""

import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from ..sources.base import DDLSource
from ..util import sanitize_filename

log = logging.getLogger(__name__)

FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.I)


def filename_from_response(resp: httpx.Response, fallback: str) -> str:
    """File name from Content-Disposition, else the final URL path."""
    cd = resp.headers.get("Content-Disposition", "")
    m = FILENAME_RE.search(cd)
    if m:
        name = unquote(m.group(1)).strip()
        if name:
            return sanitize_filename(name)
    path_name = unquote(Path(urlparse(str(resp.url)).path).name)
    return sanitize_filename(path_name or fallback)


async def download_release(
    source: DDLSource,
    release_external_id: str,
    staging_dir: Path,
    progress_cb=None,
) -> Path:
    """Resolve a release's direct links and stream every file into a fresh
    subdirectory of staging_dir. Returns that directory (the import payload).
    progress_cb(done_bytes, total_bytes) is called as data arrives; total is
    0 when the server sends no Content-Length."""
    links = await source.resolve_downloads(release_external_id)
    payload_dir = _unique_dir(staging_dir, release_external_id)
    payload_dir.mkdir(parents=True, exist_ok=True)

    done_total = 0
    try:
        for i, url in enumerate(links, start=1):
            done_total = await _fetch_file(
                source.client, url, payload_dir, f"part{i}",
                progress_cb, done_total,
            )
    except BaseException:
        # leave no partial payloads behind for the importer to trip on
        for p in payload_dir.glob("*.partial"):
            p.unlink(missing_ok=True)
        raise
    if not any(payload_dir.iterdir()):
        raise RuntimeError("no files downloaded")
    return payload_dir


async def _fetch_file(
    client: httpx.AsyncClient,
    url: str,
    dest_dir: Path,
    fallback_name: str,
    progress_cb,
    done_before: int,
) -> int:
    async with client.stream("GET", url) as resp:
        resp.raise_for_status()
        name = filename_from_response(resp, fallback_name)
        total = int(resp.headers.get("Content-Length") or 0)
        dest = dest_dir / name
        tmp = dest.with_name(dest.name + ".partial")
        done = done_before
        with open(tmp, "wb") as fh:
            async for chunk in resp.aiter_bytes(chunk_size=1024 * 256):
                fh.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, done_before + total)
        tmp.replace(dest)
        log.info("Downloaded %s (%d bytes)", dest.name, dest.stat().st_size)
        return done


def _unique_dir(staging_dir: Path, seed: str) -> Path:
    slug = sanitize_filename(Path(urlparse(seed).path).name or "release")[:80]
    candidate = staging_dir / slug
    n = 1
    while candidate.exists() and any(candidate.iterdir()):
        n += 1
        candidate = staging_dir / f"{slug}-{n}"
    return candidate
