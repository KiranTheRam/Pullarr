"""Regression cases traced from the three Teen Titans books on the live server."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr import settings_service
from pullarr.api import queue, search
from pullarr.db import get_session
from pullarr.jobs import tasks
from pullarr.jobs.service import create_job
from pullarr.models import Base, Download, DownloadStatus, Issue, Job, JobKind, RootFolder, Series, SeriesSourceLink
from pullarr.sources.base import SourceRelease
from pullarr.util import release_covers_issue, sanitize_filename, standalone_issue_for_release, strip_issue_suffix

BOOKS = [
    ('Teen Titans: Raven', 'Teen Titans – Raven (TPB) (2019)', 2019),
    ('Teen Titans: Beast Boy', 'Teen Titans – Beast Boy (2020)', 2020),
    ('Teen Titans: Beast Boy Loves Raven', 'Teen Titans – Beast Boy Loves Raven (2021)', 2021),
]


def book(title, year):
    issue = Issue(id=1, number=1, display_number='1', title='GN', monitored=True,
                  downloaded=False, released_at=datetime(year, 6, 1, tzinfo=timezone.utc))
    return Series(id=1, title=title, alt_titles='', year=year, status='finished',
                  total_issues=1, monitored=True, issues=[issue], source_links=[])


@pytest.mark.parametrize('title,release_title,year', BOOKS)
def test_unnumbered_book_covers_its_only_issue(title, release_title, year):
    series = book(title, year)
    release = SourceRelease('getcomics', 'post', release_title, year=year)
    assert release_covers_issue(release, series.issues[0], series)
    # Generic matching still requires a number without the complete series context.
    assert not release_covers_issue(release, series.issues[0])


@pytest.mark.parametrize('change,release_title', [
    ({}, 'Teen Titans – Beast Boy Loves Raven (2020)'),
    ({}, 'Teen Titans – Beast Boy (2025)'),
    ({}, 'Teen Titans – Beast Boy'),
    ({}, 'Teen Titans – Beast Boy Vol. 1 (2020)'),
    ({}, 'Teen Titans – Beast Boy (Complete Collection) (2020)'),
    ({}, 'Teen Titans – Beast Boy (Box Set) (2020)'),
    ({}, 'Teen Titans – Beast Boy #2 (2020)'),
    ({'total_issues': None}, 'Teen Titans – Beast Boy (2020)'),
    ({'total_issues': 6}, 'Teen Titans – Beast Boy (2020)'),
    ({'status': 'ongoing', 'issue_title': ''}, 'Teen Titans – Beast Boy (2020)'),
    ({'display_number': '1AU'}, 'Teen Titans – Beast Boy (2020)'),
])
def test_standalone_inference_rejects_ambiguous_or_wrong_releases(change, release_title):
    series = book('Teen Titans: Beast Boy', 2020)
    if 'issue_title' in change:
        series.issues[0].title = change['issue_title']
    if 'display_number' in change:
        series.issues[0].display_number = change['display_number']
    for key, value in change.items():
        if key not in {'issue_title', 'display_number'}:
            setattr(series, key, value)
    assert standalone_issue_for_release(series, release_title) is None


def test_partial_issue_list_cannot_be_treated_as_a_standalone():
    series = book('Teen Titans: Beast Boy', 2020)
    series.issues.append(Issue(id=2, number=2, display_number='2'))
    assert standalone_issue_for_release(series, BOOKS[1][1]) is None


@pytest.fixture
async def library(tmp_path, monkeypatch, request):
    title, release_title, year = BOOKS[getattr(request, "param", 1)]
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "test.db"}')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        series = book(title, year)
        (tmp_path / "comics").mkdir()
        series.root_folder = RootFolder(path=str(tmp_path / 'comics'))
        series.source_links = [SeriesSourceLink(source_name='getcomics', external_id=strip_issue_suffix(release_title))]
        session.add(series)
        await session.commit()
    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session
    monkeypatch.setattr(tasks, 'session_scope', scope)
    values = {**settings_service.DEFAULTS}
    monkeypatch.setattr(tasks.registry, 'apply_settings', AsyncMock(return_value=values))
    release = SourceRelease('getcomics', 'https://getcomics.org/dc/standalone-book/', release_title, year=year)
    source = SimpleNamespace(name='getcomics', list_releases=AsyncMock(return_value=[release]), search_releases=AsyncMock(return_value=[]))
    monkeypatch.setattr(tasks.registry, 'enabled_ddl_sources', lambda _: [source])
    monkeypatch.setitem(tasks.registry.DDL_SOURCES, 'getcomics', source)
    yield maker, source, release
    await engine.dispose()


@pytest.mark.parametrize('library', [0, 1, 2], indirect=True)
async def test_add_time_search_and_search_missing_queue_and_import_book(library, tmp_path, monkeypatch):
    maker, source, release = library
    monkeypatch.setattr(tasks, 'refresh_series_metadata', AsyncMock())
    monkeypatch.setattr(tasks, 'update_issues', AsyncMock())
    async with maker() as session:
        job = await create_job(session, JobKind.SEARCH_MISSING, series_id=1)
        job_id = job.id
    await tasks.refresh_series_full(1, grab_missing=True, only_monitored=False, job_id=job_id)
    async with maker() as session:
        row = (await session.execute(select(Download))).scalar_one()
        assert (row.issue_id, row.title, row.status) == (1, release.title, DownloadStatus.QUEUED)
        assert (await session.get(Job, job_id)).detail == 'Queued 1 download'
    # The next search must not download the book again while it is in flight.
    async with maker() as session:
        series = await tasks._load_series(session, 1)
        assert await tasks.grab_missing_issues(session, series, settings_service.DEFAULTS) == 0
    source.search_releases.assert_not_awaited()

    payload = tmp_path / 'staging'
    payload.mkdir()
    with zipfile.ZipFile(payload / (release.title + '.cbz'), 'w') as archive:
        archive.writestr('001.png', b'\x89PNG\r\n\x1a\n' + b'\x00' * 16)
    monkeypatch.setattr(tasks, 'download_release', AsyncMock(return_value=payload))
    await tasks.process_direct_queue()
    async with maker() as session:
        row = (await session.execute(select(Download))).scalar_one()
        issue = await session.get(Issue, 1)
        assert row.status == DownloadStatus.DONE
        assert issue.downloaded and issue.file_source == 'getcomics'
        assert Path(issue.file_path).name == sanitize_filename((await session.get(Series, 1)).title) + ' #001.cbz'
        assert Path(issue.file_path).is_file()


async def api_client(maker):
    app = FastAPI()
    app.include_router(queue.router)
    app.include_router(search.router)
    async def session_override():
        async with maker() as session:
            yield session
    app.dependency_overrides[get_session] = session_override
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test')


async def test_series_level_manual_grab_attaches_book_issue(library):
    maker, _, release = library
    async with await api_client(maker) as client:
        response = await client.post('/queue/grab', json={
            'series_id': 1, 'source_name': 'getcomics', 'external_id': release.external_id,
            'title': release.title,
        })
    assert response.status_code == 201, response.text
    assert response.json()['issue_id'] == 1
    assert response.json()['status'] == 'queued'


async def test_issue_search_finds_unnumbered_book_without_appending_number(library):
    maker, source, _ = library
    async with await api_client(maker) as client:
        response = await client.get('/search/releases?issue_id=1&include_source_status=true')
    assert response.status_code == 200, response.text
    assert len(response.json()['releases']) == 1
    source.list_releases.assert_awaited_once_with('Teen Titans – Beast Boy')
    source.search_releases.assert_not_awaited()
