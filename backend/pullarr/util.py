import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger(__name__)


class RateLimiter:
    """Simple async token-bucket limiter shared per source."""

    def __init__(self, rate: float, per_seconds: float = 1.0) -> None:
        self._interval = per_seconds / rate
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self._interval
        if wait > 0:
            await asyncio.sleep(wait)


# ---- rate-limited HTTP with reactive back-off ----

# statuses that mean "you're going too fast / try again shortly"
_RETRY_STATUS = {429, 503}


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Seconds to wait per the server, from Retry-After or X-RateLimit-Reset."""
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return max(0.0, float(ra))  # delta-seconds form
        except ValueError:
            try:  # HTTP-date form
                when = parsedate_to_datetime(ra)
                return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                pass
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(0.0, float(reset) - datetime.now(timezone.utc).timestamp())
        except ValueError:
            pass
    return None


async def rl_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    limiter: RateLimiter | None = None,
    retries: int = 4,
    max_wait: float = 60.0,
    **kwargs,
) -> httpx.Response:
    """HTTP request with proactive rate limiting plus reactive back-off.

    The limiter (if given) paces requests up front; on a 429/503 response the
    call waits (honoring Retry-After) and retries, and transient network
    errors are retried with exponential back-off. Returns the final response
    (the caller still checks its status)."""
    resp: httpx.Response | None = None
    for attempt in range(retries + 1):
        if limiter is not None:
            await limiter.acquire()
        try:
            resp = await client.request(method, url, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if attempt >= retries:
                raise
            wait = min(2.0 ** attempt, max_wait)
            log.warning("%s %s network error (%s); retrying in %.0fs",
                        method, url, exc.__class__.__name__, wait)
            await asyncio.sleep(wait)
            continue
        if resp.status_code in _RETRY_STATUS and attempt < retries:
            wait = _retry_after_seconds(resp)
            if wait is None:
                wait = min(2.0 ** attempt, max_wait)
            wait = min(wait, max_wait)
            log.warning("%s %s -> %d; backing off %.0fs (attempt %d/%d)",
                        method, url, resp.status_code, wait, attempt + 1, retries)
            await asyncio.sleep(wait)
            continue
        return resp
    return resp  # type: ignore[return-value]


ILLEGAL_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    cleaned = ILLEGAL_PATH_CHARS.sub("", name).strip().rstrip(".")
    return re.sub(r"\s+", " ", cleaned) or "Unknown"


# Comic issue markers: "#12", "# 12.5", "No. 12", "Issue 12". The "#" form is
# the canonical one on GetComics and in scene release names; the word forms
# use a lookbehind (no preceding letter) so they also match after "_"/"["
ISSUE_PREFIX_PATTERN = re.compile(
    r"(?:#[ ]?|(?<![a-z])(?:no|issue)[ ._]{0,2})(\d+(?:\.\d+)?)", re.I
)
TRAILING_NUMBER_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)\s*$")
BRACKET_GROUPS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
# "v2"/"Vol. 3"/"Volume 04" — for comics this marks a TPB/collected volume
# when no issue marker sits next to it; in "Batman v2 #12" it's the series
# iteration and the file is still issue 12
VOLUME_PATTERN = re.compile(r"(?<![a-z0-9])v(?:ol(?:ume)?)?[ ._]{0,2}(\d+)", re.I)
YEAR_PATTERN = re.compile(r"\(((?:19|20)\d{2})\)")


def has_issue_marker(text: str) -> bool:
    """True when text has an explicit issue token (#/no./issue + number),
    as opposed to a bare trailing number that might actually be a volume."""
    return ISSUE_PREFIX_PATTERN.search(text) is not None


def parse_issue_number(text: str) -> float | None:
    m = ISSUE_PREFIX_PATTERN.search(text)
    if m:
        return float(m.group(1))
    # scene-style names bury the issue number before tag groups:
    # "Absolute_Batman_015_(2026)_(Webrip)_(DCP)" → strip (…)/[…] and
    # underscores, then the issue is the trailing number
    stripped = BRACKET_GROUPS.sub(" ", text)
    stripped = re.sub(r"[_\s]+", " ", stripped).strip()
    m = TRAILING_NUMBER_PATTERN.search(stripped)
    if m:
        return float(m.group(1))
    return None


def parse_volume_number(text: str) -> int | None:
    m = VOLUME_PATTERN.search(text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def parse_year(text: str) -> int | None:
    """Publication year from a "(2026)" group in a release/post title."""
    m = YEAR_PATTERN.search(text)
    return int(m.group(1)) if m else None


def strip_issue_suffix(title: str) -> str:
    """The series-name part of a release title: everything before the issue
    marker and bracket groups. "Absolute Batman #15 (2025)" → "Absolute Batman"."""
    t = BRACKET_GROUPS.sub(" ", title)
    m = ISSUE_PREFIX_PATTERN.search(t)
    if m:
        t = t[: m.start()]
    else:
        t = TRAILING_NUMBER_PATTERN.sub("", t.strip())
    t = re.sub(r"[-–—:|,+]+\s*$", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


def normalize_title(title: str) -> str:
    """Loose normalization for cross-source title matching."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()
