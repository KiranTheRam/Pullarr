"""ComicVine metadata provider.

A ComicVine "volume" is what pullarr calls a series (e.g. Batman (2016));
its "issues" are the per-issue records. Requires a free API key
(comicvine.gamespot.com/api) set in Settings — searches fail with a clear
error until it's configured.
"""

import html
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from .. import USER_AGENT
from ..util import RateLimiter, rl_request
from .base import IssueMetadata, MetadataProvider, SeriesMetadata

log = logging.getLogger(__name__)

API_URL = "https://comicvine.gamespot.com/api"

VOLUME_FIELDS = "id,name,start_year,publisher,image,description,count_of_issues,aliases"
ISSUE_FIELDS = "id,issue_number,name,cover_date,store_date,image"

# ComicVine allows 200 requests/resource/hour — pace well under that and
# lean on its Retry-After handling for velocity blocks
_limiter = RateLimiter(rate=1, per_seconds=2)

TAG_RE = re.compile(r"<[^>]+>")


class ComicVineError(RuntimeError):
    pass


def _clean_html(text: str | None) -> str:
    if not text:
        return ""
    return TAG_RE.sub("", html.unescape(text)).strip()


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_issue_number(value: str | None) -> float | None:
    """ComicVine issue_number is a string: "1", "1.5", also "1.MU", "½",
    "Special". Numeric-prefixed forms are kept; the rest are skipped."""
    if not value:
        return None
    value = value.strip()
    if value == "½":
        return 0.5
    m = re.match(r"^(\d+(?:\.\d+)?)", value)
    return float(m.group(1)) if m else None


def _plain_numeric_label(value: str) -> bool:
    return value == "½" or re.fullmatch(r"\d+(?:\.\d+)?", value) is not None


def _next_available(base: float, used: set[float]) -> float:
    n = 1
    candidate = base
    while candidate in used:
        candidate = round(base + (n / 1000), 3)
        n += 1
    used.add(candidate)
    return candidate


def _assign_sort_numbers(raw_numbers: list[str]) -> list[float]:
    parsed = [parse_issue_number(raw) for raw in raw_numbers]
    plain_bases = {
        number
        for raw, number in zip(raw_numbers, parsed)
        if number is not None and _plain_numeric_label(raw)
    }
    max_numeric = max((n for n in parsed if n is not None), default=0.0)
    used: set[float] = set()
    special_base = max_numeric + 1.0
    sort_numbers: list[float] = []
    for raw, number in zip(raw_numbers, parsed):
        if number is None:
            sort_numbers.append(_next_available(special_base, used))
            special_base = sort_numbers[-1] + 0.001
            continue
        if number in used or (number in plain_bases and not _plain_numeric_label(raw)):
            sort_numbers.append(_next_available(number + 0.001, used))
            continue
        used.add(number)
        sort_numbers.append(number)
    return sort_numbers


class ComicVineProvider(MetadataProvider):
    name = "comicvine"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True
        )
        self._api_key = ""

    def configure(self, api_key: str) -> None:
        self._api_key = api_key.strip()

    async def _get(self, path: str, params: dict) -> dict:
        if not self._api_key:
            raise ComicVineError(
                "ComicVine API key not configured (Settings → Metadata)"
            )
        resp = await rl_request(
            self._client, "GET", f"{API_URL}/{path}/", limiter=_limiter,
            params={"api_key": self._api_key, "format": "json", **params},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status_code") != 1:
            raise ComicVineError(f"ComicVine error: {data.get('error', 'unknown')}")
        return data

    def _to_metadata(self, volume: dict) -> SeriesMetadata:
        image = volume.get("image") or {}
        aliases = [a for a in (volume.get("aliases") or "").splitlines() if a.strip()]
        year = None
        try:
            year = int(volume.get("start_year") or "")
        except (ValueError, TypeError):
            pass
        return SeriesMetadata(
            provider=self.name,
            provider_id=str(volume["id"]),
            title=volume.get("name") or "Unknown",
            alt_titles=aliases,
            description=_clean_html(volume.get("description")),
            status="unknown",
            publisher=(volume.get("publisher") or {}).get("name", "") or "",
            year=year,
            cover_url=image.get("medium_url") or image.get("original_url") or "",
            genres=[],
            total_issues=volume.get("count_of_issues"),
        )

    async def search(self, query: str, limit: int = 20) -> list[SeriesMetadata]:
        data = await self._get("search", {
            "query": query,
            "resources": "volume",
            "field_list": VOLUME_FIELDS,
            "limit": min(limit, 25),
        })
        return [self._to_metadata(v) for v in data.get("results") or []]

    async def get_series(self, provider_id: str) -> SeriesMetadata | None:
        try:
            data = await self._get(f"volume/4050-{int(provider_id)}", {
                "field_list": VOLUME_FIELDS,
            })
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        except ComicVineError as exc:
            if "Object Not Found" in str(exc):
                return None
            raise
        result = data.get("results")
        return self._to_metadata(result) if result else None

    async def issues_in_stores(
        self, start: str, end: str, issue_number: str | None = None,
        limit: int = 100, offset: int = 0,
    ) -> list[dict]:
        """Raw issues with a store date in [start, end] (ISO dates), newest
        first. issue_number="1" restricts to series launches."""
        filters = [f"store_date:{start}|{end}"]
        if issue_number is not None:
            filters.append(f"issue_number:{issue_number}")
        data = await self._get("issues", {
            "filter": ",".join(filters),
            "sort": "store_date:desc",
            "field_list": "id,name,issue_number,store_date,image,volume",
            "limit": min(limit, 100),
            "offset": offset,
        })
        return data.get("results") or []

    async def volumes_by_ids(self, volume_ids: list[int]) -> dict[int, SeriesMetadata]:
        """Look up many volumes at once. ComicVine's list filters take several
        values for one field separated by "|", so a whole discovery page costs
        a single request instead of one per volume."""
        out: dict[int, SeriesMetadata] = {}
        unique = list(dict.fromkeys(volume_ids))
        for start in range(0, len(unique), 100):
            chunk = unique[start:start + 100]
            data = await self._get("volumes", {
                "filter": "id:" + "|".join(str(v) for v in chunk),
                "field_list": VOLUME_FIELDS,
                "limit": 100,
            })
            for volume in data.get("results") or []:
                out[int(volume["id"])] = self._to_metadata(volume)
        return out

    async def list_issues(self, provider_id: str) -> list[IssueMetadata]:
        raw_items: list[dict] = []
        offset = 0
        while True:
            data = await self._get("issues", {
                "filter": f"volume:{int(provider_id)}",
                "field_list": ISSUE_FIELDS,
                "limit": 100,
                "offset": offset,
            })
            page = data.get("results") or []
            raw_items.extend(page)
            offset += len(page)
            if offset >= int(data.get("number_of_total_results") or 0) or not page:
                break
        raw_numbers = [str(item.get("issue_number") or "").strip() for item in raw_items]
        sort_numbers = _assign_sort_numbers(raw_numbers)
        issues: list[IssueMetadata] = []
        for item, number, display_number in zip(raw_items, sort_numbers, raw_numbers):
            image = item.get("image") or {}
            issues.append(IssueMetadata(
                provider_id=str(item.get("id") or ""),
                number=number,
                display_number=display_number or str(number),
                title=item.get("name") or "",
                released_at=_parse_date(item.get("store_date"))
                or _parse_date(item.get("cover_date")),
                cover_url=image.get("small_url") or "",
            ))
        issues.sort(key=lambda i: (i.number, i.display_number))
        return issues


def derive_status(issues: list[IssueMetadata], total_issues: int | None) -> str:
    """ComicVine volumes carry no status; call a series releasing when its
    newest issue is recent (a year covers annuals/delays), finished otherwise."""
    dates = [i.released_at for i in issues if i.released_at is not None]
    if not dates:
        return "unknown"
    newest = max(dates)
    if newest >= datetime.now(timezone.utc) - timedelta(days=365):
        return "releasing"
    return "finished"


provider = ComicVineProvider()
