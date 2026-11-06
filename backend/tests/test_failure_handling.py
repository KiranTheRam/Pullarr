"""Retry scheduling, outage isolation, and local failures through real DB paths."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr.api import queue
from pullarr.db import _migrate_sqlite
from pullarr.jobs import tasks
from pullarr.library.scanner import LibraryUnavailableError
from pullarr.models import (Base, Download, DownloadKind, DownloadStatus, HistoryEvent,
                            Issue, RootFolder, Series, SeriesSourceLink)
from pullarr.sources.base import SourceRelease, SourceSeries
from pullarr.util import is_transient_error


def http_error(status):
    request = httpx.Request("GET", "https://source.test/release")
    return httpx.HTTPStatusError("source failed", request=request,
                                 response=httpx.Response(status, request=request))


def wrapped(cause):
    exc = RuntimeError("wrapped release failure")
    exc.__cause__ = cause
    return exc


@pytest.fixture(autouse=True)
def clean_outages(monkeypatch):
    monkeypatch.setattr(tasks, "_source_cooldown_until", {})
    monkeypatch.setattr(tasks, "_link_retry_at", {})


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


class Source:
    name = "fake"

    def __init__(self, *, error=None, fail="list"):
        self.error, self.fail = error, fail
        self.calls = {"list": 0, "search": 0, "link": 0}

    def call(self, operation):
        self.calls[operation] += 1
        if self.fail == operation and self.error:
            raise self.error

    async def list_releases(self, query):
        self.call("list")
        return []

    async def search_releases(self, query):
        self.call("search")
        return [SourceRelease(source_name=self.name, external_id="release",
                              title="Series #1", issue_number=1)]

    async def search_series(self, query):
        self.call("link")
        return [SourceSeries(source_name=self.name, external_id="Series", title="Series")]


async def series_in(session, tmp_path, *, linked=True, root=True):
    series = Series(title="Series", sort_title="series", folder_name="Series", alt_titles="Alias")
    if root:
        series.root_folder = (await session.execute(select(RootFolder).where(
            RootFolder.path == str(tmp_path)))).scalar_one_or_none() or RootFolder(path=str(tmp_path))
    series.issues = [Issue(number=n, monitored=True) for n in range(1, 4)]
    if linked:
        series.source_links = [SeriesSourceLink(source_name="fake", external_id="Series")]
    session.add(series)
    await session.commit()
    return await tasks._load_series(session, series.id)


async def download_in(session, series, **kwargs):
    dl = Download(series_id=series.id, issue_id=series.issues[0].id,
                  kind=DownloadKind.DIRECT, status=DownloadStatus.QUEUED,
                  source_name="fake", title="Series #1", payload="release", **kwargs)
    session.add(dl)
    await session.commit()
    return dl


def use_source(monkeypatch, source, tmp_path):
    values = {"source_fake_enabled": "true", "source_priority": "fake",
              "download_retry_attempts": "0", "ddl_directory": str(tmp_path / "staging"),
              "qbittorrent_enabled": "false", "naming_template": "{series} #{issue}"}
    async def apply(session):
        return values
    monkeypatch.setattr(tasks.registry, "apply_settings", apply)
    monkeypatch.setattr(tasks.registry, "enabled_ddl_sources", lambda values: [source])
    monkeypatch.setitem(tasks.registry.DDL_SOURCES, "fake", source)
    return values


@pytest.mark.parametrize("error,code,transient", [
    (httpx.ConnectError("refused"), "network_error", True),
    (ConnectionRefusedError("refused"), "network_error", True),
    (TimeoutError("stalled"), "timeout", True),
    (wrapped(httpx.ReadTimeout("timed out")), "timeout", True),
    (httpx.RemoteProtocolError("peer closed"), "incomplete_transfer", True),
    (http_error(408), "http_408", True),
    (http_error(425), "http_425", True),
    (http_error(429), "http_429", True),
    (wrapped(http_error(503)), "http_503", True),
    (httpx.InvalidURL("bad proxy URL"), "source_configuration", False),
    (httpx.UnsupportedProtocol("unsupported URL scheme"), "source_configuration", False),
    (httpx.LocalProtocolError("bad client header"), "source_configuration", False),
    (RuntimeError("invalid series folder outside library root"), "import_error", False),
    (http_error(403), "http_403", False),
    (http_error(404), "http_404", False),
    (ValueError("timeout field invalid"), "invalid_archive", False),
    (RuntimeError("unexpected response"), "download_error", False),
    (PermissionError("cannot write"), "filesystem_error", False),
    (OSError(28, "No space left on device"), "filesystem_error", False),
    (LibraryUnavailableError("unmounted"), "library_unavailable", False),
    (RuntimeError("import collision: target exists"), "import_collision", False),
])
def test_classification(error, code, transient):
    assert tasks._download_error_code(error) == code
    assert tasks._retryable_download_error(code) is transient


def test_http_transport_wrapping_dns_oserror_stays_transient():
    import socket
    error = httpx.ConnectError("cannot resolve source")
    error.__cause__ = socket.gaierror("name resolution failed")
    assert tasks._download_error_code(wrapped(error)) == "network_error"
    assert is_transient_error(wrapped(error))


def test_exception_cycles_terminate():
    error = ValueError("bad payload")
    error.__cause__ = error
    assert not is_transient_error(error)


@pytest.mark.parametrize("error,transient", [(httpx.ConnectError("down"), True),
                                              (http_error(404), False)])
async def test_worker_failure_window_and_visibility(db_session, tmp_path, monkeypatch, error, transient):
    series = await series_in(db_session, tmp_path)
    values = use_source(monkeypatch, Source(), tmp_path)
    dl = await download_in(db_session, series)
    async def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(tasks, "download_release", fail)
    await tasks._run_direct_download(db_session, dl)
    assert dl.status == DownloadStatus.FAILED
    assert dl.transient_error is transient
    # Clear the separate short source outage so we exercise the release block.
    tasks._source_cooldown_until.clear()
    dl.updated_at = datetime.now(timezone.utc) - timedelta(hours=3)
    await db_session.commit()
    assert tasks.failed_release_blocks(dl, datetime.now(timezone.utc)) is (not transient)
    failed = await queue.get_failed_queue(session=db_session)
    assert [item.id for item in failed] == [dl.id]
    assert failed[0].transient_error is transient


async def test_repeated_exhausted_transients_escalate_per_release(db_session, tmp_path, monkeypatch):
    series = await series_in(db_session, tmp_path)
    values = use_source(monkeypatch, Source(), tmp_path)
    for n in range(3):
        dl = await download_in(db_session, series, attempt_count=1)
        await tasks._record_direct_failure(db_session, dl, series, series.issues[0],
                                           httpx.ReadTimeout("down"), values)
        assert dl.transient_error is (n < 2)
    assert tasks.failed_release_blocks(dl, datetime.now(timezone.utc) + timedelta(hours=3))
    assert not tasks.failed_release_blocks(dl, datetime.now(timezone.utc) + timedelta(hours=25))
    other = await download_in(db_session, series, attempt_count=1)
    other.payload = "other release"
    await tasks._record_direct_failure(db_session, other, series, series.issues[0],
                                       httpx.ReadTimeout("down"), values)
    assert other.transient_error


async def test_automatic_retry_retains_queue_error_and_does_not_spend_attempts_while_cooling(
    db_session, tmp_path, monkeypatch,
):
    source = Source()
    values = use_source(monkeypatch, source, tmp_path)
    values["download_retry_attempts"] = "2"
    series = await series_in(db_session, tmp_path)
    dl = await download_in(db_session, series)
    called = []
    async def fail(*args, **kwargs):
        called.append(True)
        raise httpx.ReadTimeout("down")
    monkeypatch.setattr(tasks, "download_release", fail)
    await tasks._run_direct_download(db_session, dl)
    assert dl.status == DownloadStatus.QUEUED and dl.attempt_count == 1
    assert dl.error_code == "timeout" and dl.transient_error
    assert dl.next_retry_at > datetime.now(timezone.utc) + timedelta(minutes=9)
    assert (await queue.get_queue(db_session))[0].error == "down"
    await tasks._run_direct_download(db_session, dl)
    assert len(called) == 1 and dl.attempt_count == 1
    events = (await db_session.execute(select(HistoryEvent))).scalars().all()
    assert [event.event for event in events] == ["retrying"]


@pytest.mark.parametrize("local", ["source_disabled", "missing_root_folder", "library_unavailable",
                                      "filesystem_error", "import_collision", "import_error", "cancelled"])
async def test_local_failures_do_not_block_but_explicit_user_blocks_do(db_session, tmp_path, local):
    series = await series_in(db_session, tmp_path)
    dl = await download_in(db_session, series)
    dl.status, dl.error_code = DownloadStatus.FAILED, local
    await db_session.commit()
    assert not tasks.failed_release_blocks(dl, datetime.now(timezone.utc))
    dl.blocked = True
    assert tasks.failed_release_blocks(dl, datetime.now(timezone.utc) + timedelta(days=100))


@pytest.mark.parametrize("configuration", ["disabled", "missing_root"])
async def test_fixed_configuration_can_grab_same_release_again(db_session, tmp_path, monkeypatch, configuration):
    source = Source()
    values = use_source(monkeypatch, source, tmp_path)
    series = await series_in(db_session, tmp_path, root=configuration != "missing_root")
    dl = await download_in(db_session, series)
    if configuration == "disabled":
        values["source_fake_enabled"] = "false"
    async def forbidden(*args, **kwargs):
        pytest.fail("configuration failure reached the source")
    monkeypatch.setattr(tasks, "download_release", forbidden)
    await tasks._run_direct_download(db_session, dl)
    assert dl.status == DownloadStatus.FAILED
    assert not tasks.failed_release_blocks(dl, datetime.now(timezone.utc))
    values["source_fake_enabled"] = "true"
    if configuration == "missing_root":
        series.root_folder = RootFolder(path=str(tmp_path))
        await db_session.commit()
    assert await tasks.grab_missing_issues(db_session, series, values) == 1
    assert not tasks._source_cooldown_until


async def test_no_root_does_not_search_or_queue(db_session, tmp_path, monkeypatch):
    source = Source()
    values = use_source(monkeypatch, source, tmp_path)
    series = await series_in(db_session, tmp_path, root=False)
    for _ in range(3):
        assert await tasks.grab_missing_issues(db_session, series, values) == 0
    assert not any(source.calls.values())
    assert not (await db_session.execute(select(Download))).scalars().all()


@pytest.mark.parametrize("failing", ["list", "search", "link"])
async def test_outage_is_shared_across_series_and_expires(db_session, tmp_path, monkeypatch, failing, caplog):
    now = [1000.0]
    monkeypatch.setattr(tasks.time, "monotonic", lambda: now[0])
    source = Source(error=httpx.ConnectError("down"), fail=failing)
    values = use_source(monkeypatch, source, tmp_path)
    series = [await series_in(db_session, tmp_path, linked=failing != "link") for _ in range(3)]
    for item in series:
        if failing == "link":
            await tasks.link_sources(db_session, item, values)
        else:
            await tasks.grab_missing_issues(db_session, item, values)
    assert source.calls[failing] == 1
    assert len([r for r in caplog.records if "skipping it for" in r.message]) == 1
    assert all(bool(s.source_links) is (failing != "link") for s in series)
    now[0] += tasks.SOURCE_COOLDOWN_SECONDS + 1
    if failing == "link":
        await tasks.link_sources(db_session, series[-1], values)
    else:
        await tasks.grab_missing_issues(db_session, series[-1], values)
    assert source.calls[failing] == 2


async def test_permanent_link_failure_waits_without_inventing_link(db_session, tmp_path, monkeypatch):
    source = Source(error=ValueError("bad parser"), fail="link")
    values = use_source(monkeypatch, source, tmp_path)
    series = await series_in(db_session, tmp_path, linked=False)
    await tasks.link_sources(db_session, series, values)
    await tasks.link_sources(db_session, series, values)
    assert source.calls["link"] == 1 and not series.source_links
    assert not tasks._source_cooldown_until


async def test_another_source_can_satisfy_issues_during_outage(db_session, tmp_path, monkeypatch):
    source, backup = Source(error=httpx.ConnectError("down")), Source()
    backup.name = "backup"
    values = use_source(monkeypatch, source, tmp_path)
    monkeypatch.setattr(tasks.registry, "enabled_ddl_sources", lambda values: [source, backup])
    series = await series_in(db_session, tmp_path)
    series.source_links.append(SeriesSourceLink(source_name="backup", external_id="Series"))
    await db_session.commit()
    assert await tasks.grab_missing_issues(db_session, series, values) == 1
    downloads = (await db_session.execute(select(Download))).scalars().all()
    assert [dl.source_name for dl in downloads] == ["backup"]


async def test_manual_retry_clears_transient_marker_and_block(db_session, tmp_path):
    series = await series_in(db_session, tmp_path)
    dl = await download_in(db_session, series, transient_error=True, blocked=True)
    dl.status = DownloadStatus.FAILED
    await db_session.commit()
    await queue.retry_download(dl.id, db_session)
    assert not dl.transient_error and not dl.blocked
    assert dl.next_retry_at is None and dl.error_code == ""


async def test_legacy_download_migration_is_idempotent_and_keeps_long_suppression(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql("DROP TABLE downloads")
        await conn.exec_driver_sql("CREATE TABLE downloads (id INTEGER PRIMARY KEY)")
        await conn.exec_driver_sql("INSERT INTO downloads (id) VALUES (1)")
        await _migrate_sqlite(conn)
        await _migrate_sqlite(conn)
        assert (await conn.exec_driver_sql("SELECT transient_error FROM downloads")).scalar_one() == 0
    await engine.dispose()


@pytest.mark.parametrize("transient,age,expected", [(True, 1, 0), (True, 3, 1),
                                                   (False, 3, 0), (False, 25, 1)])
async def test_monitor_obeys_release_retry_window(db_session, tmp_path, monkeypatch,
                                                  transient, age, expected):
    source = Source()
    values = use_source(monkeypatch, source, tmp_path)
    series = await series_in(db_session, tmp_path)
    dl = await download_in(db_session, series, transient_error=transient)
    dl.status = DownloadStatus.FAILED
    dl.error_code = "timeout" if transient else "http_404"
    dl.updated_at = datetime.now(timezone.utc) - timedelta(hours=age)
    await db_session.commit()
    assert await tasks.grab_missing_issues(db_session, series, values) == expected


@pytest.mark.parametrize("error,expected_code", [(PermissionError("denied"), "filesystem_error"),
                                                (ValueError("bad naming template"), "import_error")])
async def test_local_import_errors_do_not_retry_download_or_cool_source(
    db_session, tmp_path, monkeypatch, error, expected_code,
):
    source = Source()
    values = use_source(monkeypatch, source, tmp_path)
    values["download_retry_attempts"] = "4"
    series = await series_in(db_session, tmp_path)
    dl = await download_in(db_session, series)
    payload = tmp_path / "payload"
    payload.mkdir()
    async def downloaded(*args, **kwargs):
        return payload
    async def bad_import(*args, **kwargs):
        raise error
    monkeypatch.setattr(tasks, "download_release", downloaded)
    monkeypatch.setattr(tasks, "_import_download_payload", bad_import)
    await tasks._run_direct_download(db_session, dl)
    assert dl.status == DownloadStatus.FAILED and dl.error_code == expected_code
    assert dl.next_retry_at is None and not dl.transient_error
    assert not tasks._source_cooldown_until
    assert not tasks.failed_release_blocks(dl, datetime.now(timezone.utc))


async def test_torznab_outage_skips_other_issues_and_series(db_session, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    values = use_source(monkeypatch, Source(), tmp_path)
    monkeypatch.setattr(tasks.registry, "enabled_ddl_sources", lambda values: [])
    indexer = SimpleNamespace(name="Prowlarr", search=AsyncMock(side_effect=httpx.ReadTimeout("down")))
    monkeypatch.setattr(tasks.registry, "enabled_torrent_indexers", lambda values: [indexer])
    values["qbittorrent_enabled"] = "true"
    for _ in range(3):
        series = await series_in(db_session, tmp_path)
        assert await tasks.grab_missing_issues(db_session, series, values) == 0
    assert indexer.search.await_count == 1


async def test_qbittorrent_configuration_does_not_cool_successful_indexer(db_session, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    values = use_source(monkeypatch, Source(), tmp_path)
    monkeypatch.setattr(tasks.registry, "enabled_ddl_sources", lambda values: [])
    release = SimpleNamespace(title="Series 1", magnet="magnet:test", seeders=5, source_name="Prowlarr")
    indexer = SimpleNamespace(name="Prowlarr", search=AsyncMock(return_value=[release]))
    monkeypatch.setattr(tasks.registry, "enabled_torrent_indexers", lambda values: [indexer])
    monkeypatch.setattr(tasks, "submit_torrent", AsyncMock(side_effect=httpx.ConnectError("local qbt unavailable")))
    values["qbittorrent_enabled"] = "true"
    series = await series_in(db_session, tmp_path)
    assert await tasks.grab_missing_issues(db_session, series, values) == 0
    assert not tasks._source_cooldown_until
    tasks.submit_torrent.assert_awaited_once()


async def test_queue_continues_past_cooling_source_without_network_or_attempt_penalty(
    db_session, tmp_path, monkeypatch,
):
    from contextlib import asynccontextmanager
    source, healthy = Source(), Source()
    healthy.name = "healthy"
    values = use_source(monkeypatch, source, tmp_path)
    values["source_healthy_enabled"] = "true"
    monkeypatch.setitem(tasks.registry.DDL_SOURCES, "healthy", healthy)
    series = await series_in(db_session, tmp_path)
    first = await download_in(db_session, series)
    deferred = await download_in(db_session, series)
    other = await download_in(db_session, series)
    other.source_name = "healthy"
    await db_session.commit()
    calls = []
    async def fail(src, *args, **kwargs):
        calls.append(src.name)
        if src.name == "fake":
            raise httpx.ConnectError("outage")
        raise ValueError("permanent release error")
    @asynccontextmanager
    async def scope():
        yield db_session
    monkeypatch.setattr(tasks, "session_scope", scope)
    monkeypatch.setattr(tasks, "download_release", fail)
    await tasks.process_direct_queue()
    assert calls == ["fake", "healthy"]
    assert first.status == other.status == DownloadStatus.FAILED
    assert deferred.status == DownloadStatus.QUEUED and deferred.attempt_count == 0
    assert deferred.next_retry_at > datetime.now(timezone.utc) + timedelta(minutes=9)
