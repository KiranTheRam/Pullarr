"""Optional Metron enrichment keyed by Pullarr's existing ComicVine IDs.

Metron is deliberately an enrichment provider rather than the identity source:
ComicVine volume/issue IDs remain stable in existing Pullarr libraries, while
Metron adds status, creators, summaries, arcs, collected-edition reprints and
other ComicInfo fields via its exact ``cv_id`` filters.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from .. import USER_AGENT
from ..models import Issue, Series, SeriesStatus, utcnow
from ..util import RateLimiter, rl_request

log = logging.getLogger(__name__)

API_URL = "https://metron.cloud/api"
_limiter = RateLimiter(rate=1, per_seconds=3.1)  # under Metron's 20/min burst


class MetronError(RuntimeError):
    pass


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _names(values: list[dict] | None) -> str:
    return ", ".join(v.get("name", "").strip() for v in values or [] if v.get("name", "").strip())


def _status(value: str | None) -> SeriesStatus | None:
    key = (value or "").strip().lower().replace(" ", "_")
    aliases = {
        "ongoing": SeriesStatus.RELEASING,
        "releasing": SeriesStatus.RELEASING,
        "completed": SeriesStatus.FINISHED,
        "ended": SeriesStatus.FINISHED,
        "finished": SeriesStatus.FINISHED,
        "hiatus": SeriesStatus.HIATUS,
        "cancelled": SeriesStatus.CANCELLED,
        "canceled": SeriesStatus.CANCELLED,
        "upcoming": SeriesStatus.NOT_YET_RELEASED,
    }
    return aliases.get(key)


class MetronProvider:
    name = "metron"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._external_client = client is not None
        self._client = client or httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True
        )
        self._username = ""
        self._password = ""

    def configure(self, username: str, password: str) -> None:
        self._username = username.strip()
        self._password = password

    @property
    def enabled(self) -> bool:
        return bool(self._username and self._password)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.enabled:
            raise MetronError("Metron username/password not configured")
        response = await rl_request(
            self._client,
            "GET",
            f"{API_URL}/{path.lstrip('/')}",
            limiter=_limiter,
            params=params or {},
            auth=httpx.BasicAuth(self._username, self._password),
        )
        if response.status_code in (401, 403):
            raise MetronError("Metron username or password is invalid")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MetronError(f"Metron request failed ({response.status_code})") from exc
        try:
            return response.json()
        except ValueError as exc:
            raise MetronError("Metron returned invalid JSON") from exc

    async def test(self) -> dict:
        data = await self._get("series/", {"name": "Batman", "page_size": 1})
        return {"ok": True, "results": int(data.get("count") or 0)}

    async def _detail_by_cv_id(self, resource: str, comicvine_id: int) -> dict | None:
        result = await self._get(f"{resource}/", {"cv_id": comicvine_id})
        matches = result.get("results") or []
        if not matches:
            return None
        return await self._get(f"{resource}/{matches[0]['id']}/")

    async def get_series_by_cv_id(self, comicvine_id: int) -> dict | None:
        return await self._detail_by_cv_id("series", comicvine_id)

    async def get_issue_by_cv_id(self, comicvine_id: int) -> dict | None:
        return await self._detail_by_cv_id("issue", comicvine_id)

    async def enrich_series(self, series: Series) -> bool:
        if not series.comicvine_id or not self.enabled:
            return False
        data = await self.get_series_by_cv_id(series.comicvine_id)
        if data is None:
            return False
        series.metron_id = int(data["id"])
        if data.get("desc"):
            series.description = data["desc"]
        if data.get("publisher", {}).get("name"):
            series.publisher = data["publisher"]["name"]
        genres = _names(data.get("genres"))
        if genres:
            series.genres = genres
        status = _status(data.get("status"))
        if status is not None:
            series.status = status
        series.metadata_refreshed_at = utcnow()
        return True

    async def enrich_issue(self, issue: Issue) -> bool:
        if not issue.comicvine_id or not self.enabled:
            return False
        data = await self.get_issue_by_cv_id(issue.comicvine_id)
        if data is None:
            return False
        issue.metron_id = int(data["id"])
        issue.summary = data.get("desc") or issue.summary
        issue.page_count = data.get("page") or issue.page_count
        issue.web_url = data.get("resource_url") or issue.web_url
        issue.imprint = (data.get("imprint") or {}).get("name", "") or issue.imprint
        issue.genres = _names((data.get("series") or {}).get("genres")) or issue.genres
        issue.story_arcs = _names(data.get("arcs"))
        issue.characters = _names(data.get("characters"))
        issue.teams = _names(data.get("teams"))
        issue.reprints = "\n".join(
            f"{item.get('id')}|{item.get('issue', '')}" for item in data.get("reprints") or []
        )
        series_type = ((data.get("series") or {}).get("series_type") or {}).get("name", "")
        issue.format = series_type
        if data.get("store_date") or data.get("cover_date"):
            issue.released_at = _date(data.get("store_date")) or _date(data.get("cover_date"))

        role_fields: dict[str, list[str]] = {
            "writers": [], "pencillers": [], "inkers": [], "colorists": [],
            "letterers": [], "cover_artists": [], "editors": [], "translators": [],
        }
        role_aliases = {
            "writer": "writers", "script": "writers",
            "penciller": "pencillers", "pencils": "pencillers", "artist": "pencillers",
            "inker": "inkers", "inks": "inkers",
            "colorist": "colorists", "colors": "colorists",
            "letterer": "letterers", "letters": "letterers",
            "cover": "cover_artists", "cover artist": "cover_artists",
            "editor": "editors", "translator": "translators",
        }
        for credit in data.get("credits") or []:
            creator = str(credit.get("creator") or "").strip()
            for role in credit.get("role") or []:
                target = role_aliases.get(str(role.get("name") or "").strip().lower())
                if creator and target and creator not in role_fields[target]:
                    role_fields[target].append(creator)
        for field, names in role_fields.items():
            setattr(issue, field, ", ".join(names))
        issue.metadata_refreshed_at = utcnow()
        return True


provider = MetronProvider()
