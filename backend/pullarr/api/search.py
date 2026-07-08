from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..metadata.comicvine import ComicVineError, provider as comicvine
from ..models import Issue, Series
from ..schemas import MetadataResult, ReleaseOut
from ..sources import registry
from ..util import normalize_title, strip_issue_suffix

router = APIRouter(prefix="/search", tags=["search"])


@router.get("/metadata", response_model=list[MetadataResult])
async def search_metadata(q: str, session: AsyncSession = Depends(get_session)):
    await registry.apply_settings(session)  # configures the ComicVine key
    try:
        results = await comicvine.search(q)
    except ComicVineError as exc:
        raise HTTPException(400, str(exc)) from exc
    in_library = {
        row[0]
        for row in (await session.execute(select(Series.comicvine_id))).all()
        if row[0] is not None
    }
    return [
        MetadataResult(
            provider=r.provider,
            provider_id=r.provider_id,
            title=r.title,
            alt_titles=r.alt_titles,
            description=r.description,
            status=r.status,
            publisher=r.publisher,
            year=r.year,
            cover_url=r.cover_url,
            genres=r.genres,
            total_issues=r.total_issues,
            in_library=int(r.provider_id) in in_library,
        )
        for r in results
    ]


@router.get("/releases", response_model=list[ReleaseOut])
async def search_releases(
    series_id: int | None = None,
    issue_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Interactive search. With issue_id: releases for that issue. With
    series_id only: everything the sources have for the series (single
    issues, TPBs, packs) plus torrents when enabled."""
    issue = None
    if issue_id is not None:
        issue = await session.get(Issue, issue_id)
        if issue is None:
            raise HTTPException(404, "Issue not found")
        series_id = issue.series_id
    if series_id is None:
        raise HTTPException(422, "series_id or issue_id required")

    result = await session.execute(
        select(Series).options(selectinload(Series.source_links)).where(Series.id == series_id)
    )
    series = result.scalar_one_or_none()
    if series is None:
        raise HTTPException(404, "Series not found")

    values = await registry.apply_settings(session)
    releases: list[ReleaseOut] = []
    links = {sl.source_name: sl for sl in series.source_links}
    wanted = {normalize_title(series.title)}
    wanted.update(normalize_title(t) for t in series.alt_titles.split("\n") if t)
    wanted.discard("")

    for src in registry.enabled_ddl_sources(values):
        link = links.get(src.name)
        term = link.external_id if link else series.title
        wanted.add(normalize_title(term))
            try:
                if issue is not None:
                    issue_term = issue.display_number or f"{issue.number:g}"
                    found = await src.search_releases(f"{term} {issue_term}")
                found = [
                    r for r in found
                    if r.issue_number is not None
                    and r.issue_number <= issue.number <= (r.issue_end or r.issue_number)
                    and normalize_title(strip_issue_suffix(r.title)) in wanted
                ]
            else:
                found = await src.list_releases(term)
        except Exception:
            continue
        for r in found[:40]:
            releases.append(ReleaseOut(
                kind="ddl",
                source_name=src.name,
                title=r.title,
                issue_number=r.issue_number,
                issue_end=r.issue_end,
                volume_number=r.volume_number,
                external_id=r.external_id,
                url=r.url,
                size_text=r.size_text,
                year=r.year,
                posted_at=r.posted_at,
            ))

    if values["qbittorrent_enabled"] == "true":
        for indexer in registry.enabled_torrent_indexers(values):
            try:
                torrents = await indexer.search(series.title)
            except Exception:
                continue
            for t in torrents[:25]:
                releases.append(ReleaseOut(
                    kind="torrent",
                    source_name=indexer.name,
                    title=t.title,
                    magnet=t.magnet,
                    url=t.url,
                    size_bytes=t.size_bytes,
                    seeders=t.seeders,
                    leechers=t.leechers,
                ))
    return releases
