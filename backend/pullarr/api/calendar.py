"""Known ComicVine issue dates for series in the library."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Issue, Series

router = APIRouter(tags=["calendar"])


@router.get("/calendar")
async def calendar(
    start: datetime = Query(), end: datetime = Query(),
    session: AsyncSession = Depends(get_session),
):
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "Calendar dates must include a time zone")
    if end <= start or end - start > timedelta(days=93):
        raise HTTPException(422, "Calendar range must be 1–93 days")
    rows = (await session.execute(
        select(Issue, Series.title, Series.cover_url)
        .join(Series, Series.id == Issue.series_id)
        .where(Issue.released_at >= start.astimezone(timezone.utc),
               Issue.released_at < end.astimezone(timezone.utc))
        .order_by(Issue.released_at, Series.title, Issue.number)
    )).all()
    return [
        {
            "series_id": issue.series_id,
            "series_title": title,
            "cover_url": cover_url,
            "issue_id": issue.id,
            "issue_number": issue.display_number or f"{issue.number:g}",
            "issue_title": issue.title,
            "released_at": issue.released_at,
            "downloaded": issue.downloaded,
        }
        for issue, title, cover_url in rows
    ]
