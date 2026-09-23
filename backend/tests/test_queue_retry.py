"""Retry behavior for failed Activity downloads."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr.api import queue
from pullarr.jobs import tasks
from pullarr.models import (
    Base,
    Download,
    DownloadKind,
    DownloadStatus,
    HistoryEvent,
    Issue,
    Series,
)

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _failed_direct(session):
    series = Series(title="Test Series", sort_title="test series")
    session.add(series)
    await session.flush()
    issue = Issue(series_id=series.id, number=1.0)
    session.add(issue)
    await session.flush()
    dl = Download(
        series_id=series.id,
        issue_id=issue.id,
        kind=DownloadKind.DIRECT,
        status=DownloadStatus.FAILED,
        title="Test Series #1",
        source_name="getcomics",
        payload="https://example.test/test-series-1",
        progress=0.4,
        error="temporary failure",
        error_code="timeout",
        attempt_count=3,
    )
    session.add(dl)
    await session.commit()
    return dl


async def _failed_torrent(session):
    dl = Download(
        kind=DownloadKind.TORRENT,
        status=DownloadStatus.FAILED,
        title="Test pack",
        source_name="manual",
        payload=MAGNET,
        error="removed by user",
        error_code="cancelled",
    )
    session.add(dl)
    await session.commit()
    return dl


def _fake_settings(enabled="true"):
    async def fake(session):
        return {"qbittorrent_enabled": enabled}
    return fake


async def test_retry_failed_direct_download_requeues_same_item(db_session):
    dl = await _failed_direct(db_session)

    out = await queue.retry_download(dl.id, db_session)

    await db_session.refresh(dl)
    assert out.id == dl.id
    assert out.status == DownloadStatus.QUEUED
    assert out.series_title == "Test Series"
    assert dl.progress == 0.0
    assert dl.attempt_count == 0
    assert dl.error == ""
    assert dl.error_code == ""
    event = (await db_session.execute(select(HistoryEvent))).scalar_one()
    assert event.event == "retrying"
    assert event.issue_id == dl.issue_id


async def test_retry_rejects_non_failed_download(db_session):
    dl = await _failed_direct(db_session)
    dl.status = DownloadStatus.QUEUED
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await queue.retry_download(dl.id, db_session)

    assert exc.value.status_code == 409


async def test_retry_failed_torrent_resubmits_stored_magnet(db_session, monkeypatch):
    dl = await _failed_torrent(db_session)
    submitted = []

    async def fake_submit(payload, values, *, keep_existing=False):
        submitted.append((payload, keep_existing))
        return "a" * 40

    monkeypatch.setattr(queue.registry, "apply_settings", _fake_settings())
    monkeypatch.setattr(queue, "submit_torrent", fake_submit)

    out = await queue.retry_download(dl.id, db_session)

    assert submitted == [(MAGNET, True)]
    assert out.id == dl.id
    assert out.status == DownloadStatus.DOWNLOADING
    assert dl.torrent_hash == "a" * 40
    assert dl.error == ""
    assert dl.error_code == ""
    event = (await db_session.execute(select(HistoryEvent))).scalar_one()
    assert event.event == "retrying"


async def test_retry_torrent_requires_qbittorrent(db_session, monkeypatch):
    dl = await _failed_torrent(db_session)
    monkeypatch.setattr(queue.registry, "apply_settings", _fake_settings("false"))

    with pytest.raises(HTTPException) as exc:
        await queue.retry_download(dl.id, db_session)

    assert exc.value.status_code == 400
    await db_session.refresh(dl)
    assert dl.status == DownloadStatus.FAILED


async def test_retry_torrent_reports_qbittorrent_errors(db_session, monkeypatch):
    dl = await _failed_torrent(db_session)

    async def failing_submit(payload, values, *, keep_existing=False):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(queue.registry, "apply_settings", _fake_settings())
    monkeypatch.setattr(queue, "submit_torrent", failing_submit)

    with pytest.raises(HTTPException) as exc:
        await queue.retry_download(dl.id, db_session)

    assert exc.value.status_code == 502
    await db_session.refresh(dl)
    assert dl.status == DownloadStatus.FAILED


class FakeQbt:
    def __init__(self, existing):
        self.existing = existing
        self.added: list[str] = []

    async def get_torrent(self, torrent_hash):
        return object() if self.existing else None

    async def default_save_path(self):
        return "/downloads"

    async def ensure_category(self, category, save_path):
        pass

    async def add_magnet(self, magnet, category, save_path=None):
        self.added.append(magnet)

    async def close(self):
        pass


QBT_VALUES = {
    "qbittorrent_url": "http://qbt",
    "qbittorrent_username": "u",
    "qbittorrent_password": "p",
    "qbittorrent_category": "pullarr",
}


@pytest.mark.parametrize("existing,keep_existing,added", [
    (False, True, 1),   # gone from qBittorrent: add it again
    (True, True, 0),    # still there (e.g. import failed): track it, don't re-add
    (True, False, 1),   # a fresh grab always submits
])
async def test_submit_torrent_reuses_existing_only_when_asked(
    monkeypatch, existing, keep_existing, added,
):
    client = FakeQbt(existing)
    monkeypatch.setattr(tasks, "QbtClient", lambda *args: client)

    torrent_hash = await tasks.submit_torrent(MAGNET, QBT_VALUES, keep_existing=keep_existing)

    assert torrent_hash == "a" * 40
    assert len(client.added) == added
