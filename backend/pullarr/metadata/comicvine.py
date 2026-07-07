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
    return html.unescape(TAG_RE.sub("", text)).strip()


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

    async def list_issues(self, provider_id: str) -> list[IssueMetadata]:
        issues: list[IssueMetadata] = []
        offset = 0
        while True:
            data = await self._get("issues", {
                "filter": f"volume:{int(provider_id)}",
                "field_list": ISSUE_FIELDS,
                "limit": 100,
                "offset": offset,
            })
            page = data.get("results") or []
            for item in page:
                number = parse_issue_number(item.get("issue_number"))
                if number is None:
                    continue
                image = item.get("image") or {}
                issues.append(IssueMetadata(
                    provider_id=str(item.get("id") or ""),
                    number=number,
                    title=item.get("name") or "",
                    released_at=_parse_date(item.get("store_date"))
                    or _parse_date(item.get("cover_date")),
                    cover_url=image.get("small_url") or "",
                ))
            offset += len(page)
            if offset >= int(data.get("number_of_total_results") or 0) or not page:
                break
        issues.sort(key=lambda i: i.number)
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
