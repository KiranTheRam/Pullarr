"""Fetch torrent metadata and hash the original bencoded info dictionary."""
import base64
import hashlib
import re
from urllib.parse import parse_qsl

import httpx

from ..util import raise_for_status_redacted

MAX_TORRENT_SIZE = 10_000_000


def normalize_btih(value: str) -> str:
    """Normalize a v1 info hash from hexadecimal or legacy base32."""
    if re.fullmatch(r"[0-9a-fA-F]{40}", value):
        return value.lower()
    if re.fullmatch(r"[A-Za-z2-7]{32}", value):
        return base64.b32decode(value.upper()).hex()
    return ""


def magnet_btih_hex(magnet: str) -> str:
    if not magnet.lower().startswith("magnet:?"):
        return ""
    for key, value in parse_qsl(magnet[8:].partition("#")[0]):
        if key == "xt" and value.lower().startswith("urn:btih:"):
            if info_hash := normalize_btih(value[9:]):
                return info_hash
    return ""


class UnsupportedTorrentVersion(ValueError):
    """Torrent metadata does not provide a v1 identity for tracking."""


def torrent_info_hash(data: bytes) -> str:
    def parse(pos: int, depth: int = 0):
        if depth > 64 or pos >= len(data):
            raise ValueError("Invalid torrent metadata")
        token = data[pos:pos + 1]
        if token == b'i':
            end = data.index(b'e', pos + 1)
            return end + 1, int(data[pos + 1:end])
        if token in (b'd', b'l'):
            cursor = pos + 1
            while cursor < len(data) and data[cursor:cursor + 1] != b'e':
                cursor, _ = parse(cursor, depth + 1)
                if token == b'd':
                    cursor, _ = parse(cursor, depth + 1)
            if cursor >= len(data):
                raise ValueError("Invalid torrent metadata")
            return cursor + 1, None
        if not token.isdigit():
            raise ValueError("Invalid torrent metadata")
        colon = data.index(b':', pos)
        length = int(data[pos:colon])
        end = colon + 1 + length
        if length < 0 or end > len(data):
            raise ValueError("Invalid torrent metadata")
        return end, data[colon + 1:end]

    if not data.startswith(b'd'):
        raise ValueError("Expected a torrent dictionary")
    pos, info = 1, None
    while pos < len(data) and data[pos:pos + 1] != b'e':
        pos, key = parse(pos)
        if key != b'info':
            pos, _ = parse(pos)
            continue
        if info is not None or data[pos:pos + 1] != b'd':
            raise ValueError("Invalid torrent info dictionary")
        start = pos
        pos += 1
        version = pieces = None
        while data[pos:pos + 1] != b'e':
            pos, field = parse(pos, 1)
            pos, value = parse(pos, 1)
            if field == b'meta version':
                version = value
            elif field == b'pieces':
                pieces = value
        pos += 1
        # Pullarr stores the v1 info hash for release identity. qBittorrent
        # can expose a different API ID for hybrids, resolved by its client.
        if version == 2 and (not isinstance(pieces, bytes) or not pieces or len(pieces) % 20):
            raise UnsupportedTorrentVersion("BitTorrent v2-only metadata is unsupported; choose a v1 or hybrid torrent")
        info = data[start:pos]
    if pos != len(data) - 1 or data[pos:] != b'e' or info is None:
        raise ValueError("Missing torrent info dictionary")
    return hashlib.sha1(info).hexdigest()


async def fetch_torrent(url: str) -> tuple[str, str, bytes | None]:
    # Follow HTTP redirects explicitly: Prowlarr can redirect to a magnet URI.
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(6):
            if url.startswith("magnet:"):
                info_hash = magnet_btih_hex(url)
                if not info_hash:
                    raise ValueError("Invalid magnet redirect")
                return url, info_hash, None
            if not url.startswith(("http://", "https://")):
                raise ValueError("Invalid torrent download URL")
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                raise_for_status_redacted(response, "Torrent indexer")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_TORRENT_SIZE:
                        raise ValueError("Torrent metadata is too large")
                content = bytes(data)
                return url, torrent_info_hash(content), content
    raise ValueError("Too many torrent redirects")
