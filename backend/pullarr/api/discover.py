"""Recent-release discovery, backing NextPanel's comic recommendation rows.

ComicVine has no popularity or rating data, so discovery is recency-based:
issues that hit stores in a window, deduped to their volumes. Results are
cached in-memory to protect the ComicVine quota (200 requests/resource/hour);
the in-library annotation is computed fresh on every call.
"""

import time
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..metadata.comicvine import ComicVineError, provider as comicvine
from ..models import Series
from ..sources import registry

router = APIRouter(prefix="/discover", tags=["discover"])

CACHE_TTL_SECONDS = 6 * 3600
MAX_VOLUMES = 30

# cache key -> (fetched_at, grouped volume entries)
_cache: dict[str, tuple[float, list[dict]]] = {}


def clear_cache() -> None:
    """Test hook."""
    _cache.clear()


def _short_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return date.fromisoformat(iso).strftime("%b %-d")
    except ValueError:
        return iso


def group_volumes(raw_issues: list[dict], limit: int = MAX_VOLUMES) -> list[dict]:
    """Collapse an issue list (newest first) to one entry per volume."""
    items: list[dict] = []
    seen: set[int] = set()
    for issue in raw_issues:
        volume = issue.get("volume") or {}
        volume_id = volume.get("id")
        if not volume_id or volume_id in seen:
            continue
        seen.add(volume_id)
        image = issue.get("image") or {}
        number = str(issue.get("issue_number") or "").strip()
        store_date = issue.get("store_date")
        subtitle = " · ".join(
            part for part in (f"#{number}" if number else "", _short_date(store_date))
            if part
        )
        items.append({
            "comicvine_volume_id": int(volume_id),
            "volume_name": volume.get("name") or "Unknown",
            "issue_number": number,
            "issue_name": issue.get("name") or "",
            "store_date": store_date,
            "subtitle": subtitle,
            "cover_url": image.get("medium_url") or image.get("original_url") or "",
        })
        if len(items) >= limit:
            break
    return items


@router.get("/releases")
async def recent_releases(
    days: int = Query(default=7, ge=1, le=60),
    first_issues: bool = False,
    session: AsyncSession = Depends(get_session),
):
    """Volumes with issues in stores over the last `days` days, newest
    first. first_issues=true keeps only #1s (new series launches)."""
    await registry.apply_settings(session)  # configures the ComicVine key
    cache_key = f"{days}:{first_issues}"
    cached = _cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        items = cached[1]
    else:
        today = date.today()
        start = today - timedelta(days=days)
        try:
            raw = await comicvine.issues_in_stores(
                start.isoformat(), today.isoformat(),
                issue_number="1" if first_issues else None,
            )
        except ComicVineError as exc:
            raise HTTPException(400, str(exc)) from exc
        items = group_volumes(raw)
        _cache[cache_key] = (time.monotonic(), items)

    in_library = {
        row[0]
        for row in (await session.execute(select(Series.comicvine_id))).all()
        if row[0] is not None
    }
    return [
        {**item, "in_library": item["comicvine_volume_id"] in in_library}
        for item in items
    ]
