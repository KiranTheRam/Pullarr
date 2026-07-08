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


# a hyphen-separated pair of issue-like numbers: "#1-3", "#1 – 3", "#1-#3",
# "001-003", "1 - 12". Applied after bracket groups are stripped so a "(2019)"
# year can't be read as a range.
ISSUE_RANGE_PATTERN = re.compile(r"#?\s*0*(\d{1,4})\s*[-–—]\s*#?\s*0*(\d{1,4})(?!\d)")
_MAX_RANGE_SPAN = 60  # sanity cap so garbage can't claim a huge span


def has_issue_marker(text: str) -> bool:
    """True when text has an explicit issue token (#/no./issue + number),
    as opposed to a bare trailing number that might actually be a volume."""
    return ISSUE_PREFIX_PATTERN.search(text) is not None


def parse_issue_range(text: str) -> tuple[float, float] | None:
    """A multi-issue span like "#1-3" / "001-003" → (1.0, 3.0). Returns None
    for single issues, TPBs, and volume ranges ("Vol. 1-3")."""
    stripped = BRACKET_GROUPS.sub(" ", text)
    for m in ISSUE_RANGE_PATTERN.finditer(stripped):
        pre = stripped[max(0, m.start() - 7):m.start()].lower().rstrip()
        if pre.endswith(("v", "vol", "vol.", "volume")):
            continue  # a collected-volume range, not an issue range
        start, end = int(m.group(1)), int(m.group(2))
        if end > start and end - start <= _MAX_RANGE_SPAN:
            return float(start), float(end)
    return None


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


# Same marker forms as ISSUE_PREFIX_PATTERN but keeping a variant suffix
# ("#78.BEY", "#16.HU") that the numeric pattern stops before.
ISSUE_LABEL_PATTERN = re.compile(
    r"(?:#[ ]?|(?<![a-z])(?:no|issue)[ ._]{0,2})(\d+(?:\.[a-z0-9]+)?)", re.I
)
_PLAIN_LABEL = re.compile(r"\d+(?:\.\d+)?$")


def parse_issue_label(text: str) -> str | None:
    """The full issue token after an explicit marker, lowercased and keeping
    variant suffixes: "#78.BEY" → "78.bey" (parse_issue_number reads 78)."""
    m = ISSUE_LABEL_PATTERN.search(text)
    return m.group(1).lower() if m else None


def release_covers_issue(release, issue) -> bool:
    """Whether a source release provides this specific issue.

    Beyond the numeric span check, two guards keep lookalikes apart:
    variant point issues ("#78.BEY") match only on the exact display number
    (their sort number is synthetic, and a plain "#78" is a different issue);
    and when both sides carry a year they must be within a year of each
    other, so a relaunch reusing the same numbers (a later series' "#73")
    can't satisfy an older series' #73.
    """
    if (
        release.year is not None
        and issue.released_at is not None
        and abs(release.year - issue.released_at.year) > 1
    ):
        return False
    display = (issue.display_number or "").strip().lower()
    label = parse_issue_label(release.title)
    issue_is_variant = bool(display) and _PLAIN_LABEL.fullmatch(display) is None
    release_is_variant = label is not None and _PLAIN_LABEL.fullmatch(label) is None
    if issue_is_variant or release_is_variant:
        return label == display
    if release.issue_number is None:
        return False
    hi = release.issue_end if release.issue_end is not None else release.issue_number
    return release.issue_number <= issue.number <= hi


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
    """Loose normalization for cross-source title matching. A leading "The"
    is dropped because sources disagree on it (ComicVine's "The Amazing
    Spider-Man" is posted on GetComics as "Amazing Spider-Man")."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"^the ", "", t) or t


def as_utc(value: datetime) -> datetime:
    """Return a timezone-aware UTC datetime.

    SQLite can round-trip aware datetimes as naive values; the app stores all
    persisted datetimes as UTC, so naive values are interpreted as UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_released(value: datetime | None, now: datetime | None = None) -> bool:
    """True when an issue date is due for grabbing/listing as wanted."""
    if value is None:
        return True
    current = as_utc(now or datetime.now(timezone.utc))
    return as_utc(value) <= current
