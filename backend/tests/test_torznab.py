import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastapi import HTTPException
from lxml import etree
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr import settings_service
from pullarr.api import queue, search, settings
from pullarr.db import _migrate_sqlite
from pullarr.download.torrent import fetch_torrent, torrent_info_hash
from pullarr.jobs import tasks
from pullarr.models import Base, Download, Issue, Series
from pullarr.schemas import GrabIn, SeriesUpdateIn
from pullarr.sources.queries import search_queries, search_variants, validate_templates
from pullarr.sources.torznab import TorznabIndexer, configured_indexers, validate_indexers

HASH = 'a' * 40
MAGNET = f'magnet:?xt=urn:btih:{HASH}'
CONFIG = dict(id='one', name='Prowlarr', url='http://prowlarr:9696/all/api', api_key='secret', categories='7030', enabled=True)
INFO = b'd4:name4:test6:lengthi123ee'
TORRENT = b'd4:info' + INFO + b'e'
FEED = f'''<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
<item><title>Batman T03</title><prowlarrindexer>French tracker</prowlarrindexer><comments>https://tracker/item/1</comments><enclosure url="http://prowlarr/download/1" length="123"/><torznab:attr name="seeders" value="7"/><torznab:attr name="peers" value="9"/></item>
<item><title>Magnet</title><torznab:attr name="magneturl" value="{MAGNET}"/><torznab:attr name="size" value="456"/></item>
</channel></rss>'''.encode()


@pytest.fixture
async def session():
    engine = create_async_engine('sqlite+aiosqlite://')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def library(session):
    series = Series(title='Batman', search_title='Batman français', search_templates='{series} T{issue:02d}\n{series} Tome {issue}')
    issue = Issue(number=3, display_number='3', released_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    series.issues = [issue]
    series.source_links = []
    session.add(series)
    await session.commit()
    return series, issue


@respx.mock
async def test_search_parameters_and_parse():
    route = respx.get(CONFIG['url']).mock(return_value=httpx.Response(200, content=FEED))
    results = await TorznabIndexer(CONFIG).search('Batman T03')
    assert dict(route.calls[0].request.url.params) == dict(t='search', q='Batman T03', apikey='secret', cat='7030', extended='1', limit='100')
    assert results[0].source_name == 'Prowlarr / French tracker'
    assert (results[0].size_bytes, results[0].seeders, results[0].leechers) == (123, 7, 2)
    assert results[1].magnet == MAGNET
    assert results[1].size_bytes == 456


@pytest.mark.parametrize('content', [b'<error code="100"/>', b'<html/>', b'<rss>'])
def test_bad_feed_is_reported(content):
    with pytest.raises((ValueError, etree.XMLSyntaxError)):
        TorznabIndexer(CONFIG).parse(content)


@pytest.mark.parametrize('patch', [dict(categories='comics'), dict(url='ftp://bad'), dict(url='http://host/api?apikey=oops'), dict(search_type='tvsearch'), dict(enabled='false')])
def test_invalid_configuration(patch):
    with pytest.raises(ValueError):
        validate_indexers(json.dumps([{**CONFIG, **patch}]))


async def test_api_keys_masked_preserved_and_removable(session):
    saved = await settings.update_settings({'torznab_indexers': json.dumps([CONFIG])}, session)
    assert 'secret' not in saved['torznab_indexers']
    masked = json.loads(saved['torznab_indexers'])
    masked[0]['categories'] = '7030,7000'
    await settings.update_settings({'torznab_indexers': json.dumps(masked)}, session)
    stored = json.loads(await settings_service.get(session, 'torznab_indexers'))
    assert stored[0]['api_key'] == 'secret'
    masked[0]['api_key'] = ''
    await settings.update_settings({'torznab_indexers': json.dumps(masked)}, session)
    assert json.loads(await settings_service.get(session, 'torznab_indexers'))[0]['api_key'] == ''
    assert configured_indexers(json.dumps([{**CONFIG, 'enabled': False}])) == []


@pytest.mark.parametrize('template', ['{series.__class__}', '{unknown}', '{issue:999999999d}', '{issue!r}', '{series'])
def test_bad_query_templates(template):
    with pytest.raises(ValueError):
        validate_templates(template)


async def test_variants_and_series_override(session):
    series, issue = await library(session)
    queries = search_queries(series, issue, settings_service.DEFAULTS)
    assert queries == ['Batman français T03', 'Batman français Tome 3']
    indexer = SimpleNamespace(search=AsyncMock(side_effect=[[], ['found']]))
    assert await search_variants(indexer, queries) == ['found']
    assert indexer.search.await_count == 2
    assert search_queries(series, None, settings_service.DEFAULTS) == ['Batman français']


@respx.mock
async def test_torrent_file_and_magnet_redirect():
    route = respx.get('http://prowlarr/download').mock(return_value=httpx.Response(200, content=TORRENT))
    _, actual_hash, data = await fetch_torrent('http://prowlarr/download')
    assert data == TORRENT
    assert actual_hash == hashlib.sha1(INFO).hexdigest()
    route.mock(return_value=httpx.Response(302, headers={'location': MAGNET}))
    magnet, actual_hash, data = await fetch_torrent('http://prowlarr/download')
    assert (magnet, actual_hash, data) == (MAGNET, HASH, None)


@pytest.mark.parametrize('data', [b'<html/>', b'de', b'd4:infoi1ee', TORRENT + b'junk', b'd4:infod4:name99:xe'])
def test_invalid_torrent(data):
    with pytest.raises(ValueError):
        torrent_info_hash(data)


@respx.mock
async def test_file_submission_uses_multipart_and_tracks_hash(monkeypatch):
    from pullarr.download.qbittorrent import QbtClient
    respx.get('http://prowlarr/download').mock(return_value=httpx.Response(200, content=TORRENT))
    client = QbtClient('http://qbt', 'admin', 'password')
    monkeypatch.setattr(tasks, 'QbtClient', lambda *args: client)
    respx.post('http://qbt/api/v2/auth/login').mock(return_value=httpx.Response(200, text='Ok.'))
    respx.get('http://qbt/api/v2/app/preferences').mock(return_value=httpx.Response(200, json={'save_path': '/downloads'}))
    respx.post('http://qbt/api/v2/torrents/createCategory').mock(return_value=httpx.Response(200))
    add = respx.post('http://qbt/api/v2/torrents/add').mock(return_value=httpx.Response(200, text='Ok.'))
    result = await tasks.submit_torrent('http://prowlarr/download', settings_service.DEFAULTS)
    assert result == hashlib.sha1(INFO).hexdigest()
    body = add.calls[0].request.content
    assert TORRENT in body and b'/downloads/pullarr' in body
    assert 'multipart/form-data' in add.calls[0].request.headers['content-type']


async def test_interactive_query_and_partial_source_failure(session, monkeypatch):
    series, issue = await library(session)
    good = TorznabIndexer(CONFIG)
    good.search = AsyncMock(return_value=good.parse(FEED))
    bad = SimpleNamespace(name='Broken', search=AsyncMock(side_effect=RuntimeError('apikey=secret')))
    values = {**settings_service.DEFAULTS, 'qbittorrent_enabled': 'true', 'source_getcomics_enabled': 'false'}
    monkeypatch.setattr(search.registry, 'apply_settings', AsyncMock(return_value=values))
    monkeypatch.setattr(search.registry, 'enabled_torrent_indexers', lambda _: [bad, good])
    result = await search.search_releases(issue_id=issue.id, session=session, include_source_status=True, q='Custom T03')
    good.search.assert_awaited_once_with('Custom T03')
    assert len(result.releases) == 2
    assert result.releases[0].source_name == 'Prowlarr / French tracker'
    assert len(result.warnings) == 1 and 'secret' not in result.warnings[0]


async def test_grab_preserves_issue_and_source(session, monkeypatch):
    series, issue = await library(session)
    await settings_service.set_many(session, {'qbittorrent_enabled': 'true'})
    monkeypatch.setattr(tasks, 'submit_torrent', AsyncMock(return_value=HASH))
    result = await queue.grab(GrabIn(series_id=series.id, issue_id=issue.id, source_name='Prowlarr', magnet=MAGNET, title='Batman T03'), session)
    row = await session.get(Download, result.id)
    assert (row.issue_id, row.source_name, row.torrent_hash) == (issue.id, 'Prowlarr', HASH)


async def test_automatic_search_requires_matching_query_and_avoids_duplicates(session, monkeypatch, tmp_path):
    series, issue = await library(session)
    from pullarr.models import RootFolder
    series.root_folder = RootFolder(path=str(tmp_path))
    await session.commit()
    indexer = SimpleNamespace(name='Prowlarr', search=AsyncMock(return_value=[
        SimpleNamespace(title='Batman français T030', magnet=MAGNET, source_name='Prowlarr', seeders=10),
        SimpleNamespace(title='Batman français T03 CBZ', magnet=MAGNET, source_name='Prowlarr', seeders=5),
    ]))
    monkeypatch.setattr(tasks.registry, 'enabled_torrent_indexers', lambda _: [indexer])
    monkeypatch.setattr(tasks, 'submit_torrent', AsyncMock(return_value=HASH))
    values = {**settings_service.DEFAULTS, 'qbittorrent_enabled': 'true', 'source_getcomics_enabled': 'false'}
    assert await tasks.grab_missing_issues(session, series, values) == 1
    assert await tasks.grab_missing_issues(session, series, values) == 0
    row = (await session.execute(select(Download))).scalar_one()
    assert row.title == 'Batman français T03 CBZ' and row.issue_id == issue.id


async def test_series_search_settings_persist_and_validate(session):
    from pullarr.api.series import update_series
    series, _ = await library(session)
    result = await update_series(series.id, SeriesUpdateIn(search_title='French title', search_templates='{series} T{issue:02d}'), session)
    assert result.search_title == 'French title'
    assert result.search_templates == '{series} T{issue:02d}'
    with pytest.raises(HTTPException):
        await update_series(series.id, SeriesUpdateIn(search_templates='{bad}'), session)


async def test_existing_database_migration_is_idempotent():
    engine = create_async_engine('sqlite+aiosqlite://')
    async with engine.begin() as conn:
        await conn.exec_driver_sql('CREATE TABLE series (id INTEGER PRIMARY KEY, title VARCHAR)')
        await conn.exec_driver_sql('CREATE TABLE issues (id INTEGER PRIMARY KEY, series_id INTEGER, comicvine_id INTEGER, number FLOAT, display_number VARCHAR)')
        await conn.exec_driver_sql("INSERT INTO series VALUES (1, 'Batman')")
        await _migrate_sqlite(conn)
        await _migrate_sqlite(conn)
        row = (await conn.exec_driver_sql('SELECT title, search_title, search_templates FROM series')).one()
        assert tuple(row) == ('Batman', '', '')
    await engine.dispose()


async def test_completed_torrent_imports_linked_issue_and_keeps_seeding_data(session, tmp_path):
    import zipfile
    from pullarr.models import DownloadKind, DownloadStatus, RootFolder

    series, issue = await library(session)
    (tmp_path / "library").mkdir()
    root = RootFolder(path=str(tmp_path / 'library'))
    session.add(root)
    await session.flush()
    series.root_folder_id = root.id
    row = Download(series_id=series.id, issue_id=issue.id, kind=DownloadKind.TORRENT,
                   status=DownloadStatus.DOWNLOADING, source_name='Prowlarr', title='Batman français T03')
    session.add(row)
    await session.commit()
    payload = tmp_path / 'download.cbz'
    with zipfile.ZipFile(payload, 'w') as archive:
        archive.writestr('001.png', b'\x89PNG\r\n\x1a\n' + b'\x00' * 16)
    await tasks._import_torrent(session, row, payload, settings_service.DEFAULTS)
    assert row.status == DownloadStatus.DONE
    assert issue.downloaded and issue.file_source == 'Prowlarr'
    assert Path(issue.file_path).name == 'Batman #003.cbz'
    assert payload.exists()
