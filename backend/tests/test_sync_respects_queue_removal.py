"""Queue removal must win over stale qBittorrent sync status transitions."""

import asyncio
import zipfile
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr import settings_service
from pullarr.api import queue
from pullarr.download.qbittorrent import QbtError, QbtTorrent
from pullarr.jobs import tasks
from pullarr.models import (
    Base, Download, DownloadKind, DownloadStatus, HistoryEvent, Issue, RootFolder, Series,
)

HASH = "e" * 40


class FakeQbt:
    def __init__(self, *args):
        pass

    async def get_torrent(self, torrent_hash):
        await self.hold("get_torrent")
        if self.missing:
            return None
        return QbtTorrent(
            hash=torrent_hash, name="Test Series #1", progress=self.progress,
            state="uploading" if self.progress == 1 else "downloading",
            content_path=str(self.payload), category="pullarr",
            save_path=str(self.payload.parent),
        )

    async def torrent_files(self, torrent_hash):
        await self.hold("torrent_files")
        if self.file_error:
            raise QbtError("qBittorrent request failed: HTTP 503")
        return self.files

    async def hold(self, call):
        if self.hold_call == call:
            self.entered.set()
            await self.release.wait()

    async def delete_torrents(self, hashes, delete_files=True):
        # Keep the payload present so a stale sync really can import it.
        self.deleted.extend(hashes)

    async def close(self):
        pass


@pytest.fixture
async def env(tmp_path, monkeypatch):
    # A file DB gives sync and removal independent connections, as in the app.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    library = tmp_path / "library"
    library.mkdir()
    payload = tmp_path / "downloads" / "Test Series #1.cbz"
    payload.parent.mkdir()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("001.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    async with maker() as session:
        await settings_service.set_many(session, {"qbittorrent_enabled": "true"})
        series = Series(title="Test Series", sort_title="test series",
                        root_folder=RootFolder(path=str(library)), folder_name="Test Series")
        series.issues = [Issue(number=1, downloaded=False)]
        session.add(series)
        await session.commit()
        dl = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                      status=DownloadStatus.DOWNLOADING, title="Test Series #1",
                      source_name="manual", torrent_hash=HASH)
        session.add(dl)
        await session.commit()
        dl_id = dl.id

    @asynccontextmanager
    async def scope():
        async with maker() as session:
            yield session

    client = FakeQbt()
    client.payload, client.files = payload, [payload.name]
    client.entered, client.release = asyncio.Event(), asyncio.Event()
    client.hold_call = None
    client.file_error, client.missing = False, False
    client.progress, client.deleted = 1.0, []
    imports = []
    real_import = tasks._import_torrent

    async def record_import(session, dl, *args, **kwargs):
        async with maker() as other:
            imports.append((dl.id, (await other.get(Download, dl.id)).status))
        await real_import(session, dl, *args, **kwargs)

    monkeypatch.setattr(tasks, "session_scope", scope)
    monkeypatch.setattr(tasks, "QbtClient", lambda *args: client)
    monkeypatch.setattr(queue, "QbtClient", lambda *args: client)
    monkeypatch.setattr(tasks, "_import_torrent", record_import)
    monkeypatch.setattr(tasks.notifications, "notify_import", lambda *args: None)
    monkeypatch.setattr(tasks, "_notify_kavita", lambda *args: None)
    yield maker, client, dl_id, library, imports
    await engine.dispose()


async def remove_during_sync(env):
    maker, client, dl_id, _, _ = env
    sync = asyncio.create_task(tasks.sync_qbittorrent())
    try:
        await asyncio.wait_for(client.entered.wait(), timeout=5)
        async with maker() as session:
            assert await queue._remove_downloads(session, [dl_id]) == 1
    finally:
        client.release.set()
        await asyncio.wait_for(sync, timeout=5)


async def assert_removed(env):
    maker, client, dl_id, library, imports = env
    async with maker() as session:
        dl = await session.get(Download, dl_id)
        assert (dl.status, dl.error, dl.error_code) == (
            DownloadStatus.FAILED, "removed by user", "cancelled",
        )
        assert dl_id not in [item.id for item in await queue.get_queue(session)]
        assert not (await session.scalar(select(Issue))).downloaded
        assert (await session.scalars(select(HistoryEvent.event))).all() == ["removed"]
    assert client.deleted == [HASH]
    assert imports == []
    assert list(library.rglob("*.cbz")) == []
    # Future sync passes must leave the removed row and payload alone.
    await tasks.sync_qbittorrent()
    assert imports == []


@pytest.mark.parametrize("hold", ["get_torrent", "torrent_files"])
async def test_removal_during_qbt_calls_is_not_imported(env, hold):
    env[1].hold_call = hold
    await remove_during_sync(env)
    await assert_removed(env)


@pytest.mark.parametrize("failure", ["api_error", "unsafe_path"])
async def test_removal_is_not_overwritten_by_file_list_failure(env, failure):
    client = env[1]
    client.hold_call = "torrent_files"
    client.file_error = failure == "api_error"
    if failure == "unsafe_path":
        client.files = ["../outside.cbz"]
    await remove_during_sync(env)
    await assert_removed(env)


async def test_incomplete_torrent_progress_does_not_change_removal(env):
    env[1].hold_call = "get_torrent"
    env[1].progress = 0.5
    await remove_during_sync(env)
    await assert_removed(env)


async def test_finished_torrent_still_claims_and_imports(env):
    maker, _, dl_id, library, imports = env
    await tasks.sync_qbittorrent()
    assert imports == [(dl_id, DownloadStatus.IMPORTING)]
    async with maker() as session:
        dl = await session.get(Download, dl_id)
        assert (dl.status, dl.error) == (DownloadStatus.DONE, "")
        issue = await session.scalar(select(Issue))
        assert issue.downloaded
        assert issue.file_path.startswith(str(library / "Test Series"))
    assert len(list(library.rglob("*.cbz"))) == 1


async def test_missing_torrent_stays_queued_without_import(env):
    maker, client, dl_id, _, imports = env
    client.missing = True
    await tasks.sync_qbittorrent()
    async with maker() as session:
        assert (await session.get(Download, dl_id)).status == DownloadStatus.DOWNLOADING
    assert imports == []
