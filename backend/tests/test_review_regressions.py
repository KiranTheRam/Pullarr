import asyncio
import subprocess
import struct
import zipfile
import zlib
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr.api import queue, search
from pullarr.download.archive import ImportValidationError, validate_archive
from pullarr.jobs import tasks
from pullarr.jobs.service import recover_interrupted_downloads
from pullarr.library.importer import import_payload
from pullarr.library.naming import DEFAULT_TEMPLATE
from pullarr.models import Base, Download, DownloadKind, DownloadStatus, HistoryEvent, Issue, Series, SeriesSourceLink
from pullarr.sources.base import SourceRelease


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def download(session, status=DownloadStatus.QUEUED, kind=DownloadKind.DIRECT):
    row = Download(kind=kind, status=status, source_name="getcomics", payload="bad-url")
    session.add(row)
    await session.commit()
    return row


async def test_removing_queued_download_does_not_cancel_retry(session):
    row = await download(session)
    await queue._remove_downloads(session, [row.id])
    await queue.retry_download(row.id, session)
    assert row.status == DownloadStatus.QUEUED
    assert not tasks._is_cancelled(row.id)


async def test_retry_waits_for_cancelled_worker_to_exit(session):
    row = await download(session, DownloadStatus.DOWNLOADING)
    token = Event()
    tasks._active_downloads[row.id] = token
    try:
        await queue._remove_downloads(session, [row.id])
        assert token.is_set()
        with pytest.raises(HTTPException) as exc:
            await queue.retry_download(row.id, session)
        assert exc.value.status_code == 409
    finally:
        tasks._active_downloads.pop(row.id)
    await queue.retry_download(row.id, session)
    assert not tasks._is_cancelled(row.id)


async def test_import_cannot_be_cancelled_halfway_through_files(session):
    row = await download(session, DownloadStatus.IMPORTING)
    tasks._importing_downloads.add(row.id)
    try:
        with pytest.raises(HTTPException) as exc:
            await queue._remove_downloads(session, [row.id])
        assert exc.value.status_code == 409
        assert row.status == DownloadStatus.IMPORTING
    finally:
        tasks._importing_downloads.discard(row.id)


async def test_stalled_import_without_file_work_can_be_removed(session):
    row = await download(session, DownloadStatus.IMPORTING, DownloadKind.TORRENT)
    await queue._remove_downloads(session, [row.id])
    assert row.status == DownloadStatus.FAILED


async def test_restart_only_recovers_interrupted_direct_attempts(session):
    rows = [await download(session, state) for state in DownloadStatus]
    torrent = await download(session, DownloadStatus.DOWNLOADING, DownloadKind.TORRENT)
    assert await recover_interrupted_downloads(session) == 2
    assert await recover_interrupted_downloads(session) == 0
    for row, original in zip(rows, DownloadStatus):
        if original in (DownloadStatus.DOWNLOADING, DownloadStatus.IMPORTING):
            assert row.status == DownloadStatus.FAILED
            assert row.error_code == "interrupted"
            await queue.retry_download(row.id, session)
            assert row.status == DownloadStatus.QUEUED
        else:
            assert row.status == original
    assert torrent.status == DownloadStatus.DOWNLOADING


async def test_worker_token_removed_even_on_early_failure(session, monkeypatch):
    row = await download(session)
    async def fail(*args):
        assert tasks.download_is_active(row.id)
        raise RuntimeError("test failure")
    monkeypatch.setattr(tasks, "_run_direct_attempt", fail)
    with pytest.raises(RuntimeError):
        await tasks._run_direct_download(session, row)
    assert not tasks.download_is_active(row.id)


async def series_fixture(session):
    series = Series(title="Test", sort_title="test", alt_titles="", issues=[
        Issue(number=1, display_number="1", monitored=True, downloaded=False),
    ], source_links=[SeriesSourceLink(source_name="getcomics", external_id="Test")])
    session.add(series)
    await session.commit()
    return series


@pytest.mark.parametrize("issue_linked", [True, False])
async def test_blocked_release_does_not_hide_alternative(session, monkeypatch, issue_linked):
    series = await series_fixture(session)
    blocked = await download(session, DownloadStatus.FAILED)
    blocked.series_id = series.id
    blocked.issue_id = series.issues[0].id if issue_linked else None
    blocked.blocked = True
    await session.commit()
    source = SimpleNamespace(name="getcomics", list_releases=AsyncMock(return_value=[
        SourceRelease("getcomics", "bad-url", "Test #1", issue_number=1),
        SourceRelease("getcomics", "good-url", "Test #1", issue_number=1),
    ]))
    monkeypatch.setattr(tasks.registry, "enabled_ddl_sources", lambda values: [source])
    assert await tasks.grab_missing_issues(session, series, {}) == 1
    queued = (await session.execute(select(Download).where(Download.status == DownloadStatus.QUEUED))).scalar_one()
    assert queued.payload == "good-url"


async def test_search_failure_is_distinct_from_no_matches(session, monkeypatch):
    series = await series_fixture(session)
    source = SimpleNamespace(name="getcomics", list_releases=AsyncMock(side_effect=RuntimeError("secret-url")))
    monkeypatch.setattr(search.registry, "apply_settings", AsyncMock(return_value={"qbittorrent_enabled": "false"}))
    monkeypatch.setattr(search.registry, "enabled_ddl_sources", lambda values: [source])
    with pytest.raises(HTTPException) as exc:
        await search.search_releases(series_id=series.id, session=session)
    assert exc.value.status_code == 502
    assert "getcomics" in exc.value.detail
    assert "secret-url" not in exc.value.detail
    source.list_releases = AsyncMock(return_value=[])
    assert await search.search_releases(series_id=series.id, session=session) == []


async def test_partial_source_failure_keeps_results_and_warns(session, monkeypatch):
    series = await series_fixture(session)
    sources = [
        SimpleNamespace(name="bad", list_releases=AsyncMock(side_effect=TimeoutError())),
        SimpleNamespace(name="good", list_releases=AsyncMock(return_value=[SourceRelease("good", "url", "Test #1", issue_number=1)])),
    ]
    monkeypatch.setattr(search.registry, "apply_settings", AsyncMock(return_value={"qbittorrent_enabled": "false"}))
    monkeypatch.setattr(search.registry, "enabled_ddl_sources", lambda values: sources)
    result = await search.search_releases(series_id=series.id, session=session, include_source_status=True)
    assert len(result.releases) == 1
    assert len(result.warnings) == 1
    assert "bad" in result.warnings[0]


async def test_resolving_manual_repair_never_requeues_payload(session):
    row = await download(session, DownloadStatus.NEEDS_ATTENTION)
    await queue.resolve_download(row.id, session)
    assert row.status == DownloadStatus.DONE
    assert (await session.execute(select(HistoryEvent.event))).scalar_one() == "resolved"
    with pytest.raises(HTTPException):
        await queue.resolve_download(row.id, session)


async def test_failed_downloads_can_be_paged(session):
    rows = [await download(session, DownloadStatus.FAILED) for _ in range(4)]
    page = await queue.get_failed_queue(limit=2, offset=2, session=session)
    assert [item.id for item in page] == [rows[1].id, rows[0].id]


async def test_filesystem_workers_are_serialized_without_blocking_loop(monkeypatch):
    from pullarr.library import work
    monkeypatch.setattr(work, "_filesystem_lock", asyncio.Lock())
    started, release, second_started = Event(), Event(), Event()
    def first():
        started.set()
        assert release.wait(timeout=5)
    first_task = asyncio.create_task(work.run_library_work(first))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        second_task = asyncio.create_task(work.run_library_work(second_started.set))
        await asyncio.sleep(0)
        assert not second_started.is_set()
        release.set()
        await asyncio.gather(first_task, second_task)
        assert second_started.is_set()
    finally:
        release.set()
        await first_task


async def test_scan_applies_detached_worker_results_on_the_loop(monkeypatch, tmp_path):
    import threading
    from pullarr.library import scanner
    original = Issue(id=1, number=1, downloaded=False, file_path="")
    loop_thread = threading.get_ident()
    def scan(series, copies, folders):
        assert threading.get_ident() != loop_thread
        assert copies[0] is not original
        copies[0].downloaded = True
        copies[0].file_path = str(tmp_path / "comic.cbz")
        assert original.downloaded is False
        return scanner.ScanResult(matched_issues=1)
    monkeypatch.setattr(scanner, "scan_series", scan)
    result = await scanner.scan_series_async(Series(title="Test"), [original], [tmp_path])
    assert result.matched_issues == 1
    assert original.downloaded


@pytest.mark.parametrize("payload", [b"<html>blocked</html>", b"PK\x03\x04truncated", b"Rar!\x1a\x07\x00broken", b"7z\xbc\xaf\x27\x1cbroken"])
def test_invalid_archive_never_enters_library(tmp_path, payload):
    path = tmp_path / "Test 001.cbz"
    path.write_bytes(payload)
    issue = Issue(id=1, number=1, display_number="1", downloaded=False)
    series = Series(id=1, title="Test", alt_titles="", folder_name="Test")
    with pytest.raises(ImportValidationError):
        import_payload(path, series, [issue], tmp_path / "library", DEFAULT_TEMPLATE)
    assert not issue.downloaded
    assert not list((tmp_path / "library").rglob("*.cbz"))


def test_valid_7z_pages_are_decoded_without_extraction(tmp_path):
    # bsdtar can create 7z without adding a Python dependency or test fixture download.
    page = tmp_path / "001.png"
    page.write_bytes(b"page fixture")
    archive = tmp_path / "comic.cb7"
    subprocess.run(["bsdtar", "--format=7zip", "-cf", str(archive), "-C", str(tmp_path), page.name], check=True)
    page.unlink()
    validate_archive(archive)
    assert not page.exists()


def test_valid_stored_rar_pages_are_decoded_without_extraction(tmp_path):
    # Synthetic RAR4 stored page, with header and file CRCs; no external fixture.
    def block(kind, flags, body):
        header = struct.pack("<BHH", kind, flags, 7 + len(body)) + body
        return struct.pack("<H", zlib.crc32(header) & 0xFFFF) + header
    name, content = b"001.png", b"synthetic page fixture"
    file_header = struct.pack(
        "<IIBIIBBHI", len(content), len(content), 3, zlib.crc32(content),
        0, 20, 0x30, len(name), 0o100644,
    ) + name
    path = tmp_path / "comic.cbr"
    path.write_bytes(b"Rar!\x1a\x07\x00" + block(0x73, 0, b"\0" * 6)
                     + block(0x74, 0x8000, file_header) + content + block(0x7B, 0, b""))
    validate_archive(path)
    assert not (tmp_path / "001.png").exists()
