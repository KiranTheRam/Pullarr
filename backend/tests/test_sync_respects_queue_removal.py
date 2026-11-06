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


@pytest.mark.parametrize("pause", ["after_claim", "before_completion_commit"])
async def test_removal_is_rejected_through_claim_and_completion(env, monkeypatch, pause):
    from fastapi import HTTPException

    maker, client, dl_id, library, _ = env
    entered, release = asyncio.Event(), asyncio.Event()
    if pause == "after_claim":
        original = tasks._load_series

        async def paused_load(*args):
            # The IMPORTING claim has committed on a separate connection.
            entered.set()
            await release.wait()
            return await original(*args)

        monkeypatch.setattr(tasks, "_load_series", paused_load)
    else:
        # Pause the real session's commit after files have been written and
        # ownership/history have been staged, before any are durable.
        from sqlalchemy.ext.asyncio import AsyncSession
        original = AsyncSession.commit

        async def paused_commit(session):
            if any(isinstance(row, HistoryEvent) and row.event == "imported"
                   for row in session.new):
                entered.set()
                await release.wait()
            return await original(session)

        monkeypatch.setattr(AsyncSession, "commit", paused_commit)
    sync = asyncio.create_task(tasks.sync_qbittorrent())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with maker() as session:
            assert (await session.get(Download, dl_id)).status == DownloadStatus.IMPORTING
            with pytest.raises(HTTPException) as exc:
                await queue._remove_downloads(session, [dl_id])
            assert exc.value.status_code == 409
        assert client.deleted == []
        assert bool(list(library.rglob("*.cbz"))) == (pause == "before_completion_commit")
    finally:
        release.set()
        await asyncio.wait_for(sync, 5)
    assert not tasks.download_is_importing(dl_id)
    async with maker() as session:
        assert (await session.get(Download, dl_id)).status == DownloadStatus.DONE
        assert (await session.scalar(select(Issue))).downloaded
        assert (await session.scalars(select(HistoryEvent.event))).all() == ["imported"]
    assert len(list(library.rglob("*.cbz"))) == 1


async def test_removal_transaction_finishes_before_import_claim(env, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    maker, client, dl_id, _, _ = env
    entered, release = asyncio.Event(), asyncio.Event()
    original = AsyncSession.commit

    async def paused_commit(session):
        if any(isinstance(row, HistoryEvent) and row.event == "removed"
               for row in session.new):
            entered.set()
            await release.wait()
        return await original(session)

    monkeypatch.setattr(AsyncSession, "commit", paused_commit)

    async def remove():
        async with maker() as session:
            await queue._remove_downloads(session, [dl_id])

    claim_entered = asyncio.Event()
    original_guard = tasks.importing_download

    @asynccontextmanager
    async def attempted_claim(download_id):
        claim_entered.set()
        async with original_guard(download_id):
            yield

    monkeypatch.setattr(tasks, "importing_download", attempted_claim)
    removal = asyncio.create_task(remove())
    await asyncio.wait_for(entered.wait(), 5)
    sync = asyncio.create_task(tasks.sync_qbittorrent())
    try:
        # Sync has finished qBittorrent calls and is trying to acquire the
        # same download's lock while the removal transaction still owns it.
        await asyncio.wait_for(claim_entered.wait(), 5)
        assert not tasks.download_is_importing(dl_id)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(removal, sync), 5)
    assert not tasks.download_is_importing(dl_id)
    await assert_removed(env)


@pytest.mark.parametrize("pause", ["after_claim", "before_completion_commit"])
async def test_direct_import_guard_also_covers_awaits_and_completion(env, monkeypatch, pause):
    from fastapi import HTTPException
    from sqlalchemy.ext.asyncio import AsyncSession

    maker, client, dl_id, library, _ = env
    async with maker() as session:
        dl = await session.get(Download, dl_id)
        dl.kind, dl.status, dl.source_name = DownloadKind.DIRECT, DownloadStatus.QUEUED, "fake"
        await session.commit()
    values = dict(settings_service.DEFAULTS, source_fake_enabled="true")

    async def settings(session):
        return values

    async def download(*args, **kwargs):
        return client.payload.parent

    monkeypatch.setattr(tasks.registry, "apply_settings", settings)
    monkeypatch.setitem(tasks.registry.DDL_SOURCES, "fake", object())
    monkeypatch.setattr(tasks, "download_release", download)
    entered, release = asyncio.Event(), asyncio.Event()
    if pause == "after_claim":
        original = tasks._import_download_payload

        async def paused_import(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(tasks, "_import_download_payload", paused_import)
    else:
        original = AsyncSession.commit

        async def paused_commit(session):
            if any(isinstance(row, HistoryEvent) and row.event == "imported"
                   for row in session.new):
                entered.set()
                await release.wait()
            return await original(session)

        monkeypatch.setattr(AsyncSession, "commit", paused_commit)

    async def run():
        async with maker() as session:
            await tasks._run_direct_download(session, await session.get(Download, dl_id))

    worker = asyncio.create_task(run())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with maker() as session:
            assert (await session.get(Download, dl_id)).status == DownloadStatus.IMPORTING
            with pytest.raises(HTTPException) as exc:
                await queue._remove_downloads(session, [dl_id])
            assert exc.value.status_code == 409
    finally:
        release.set()
        await asyncio.wait_for(worker, 5)
    assert not tasks.download_is_importing(dl_id)
    assert not tasks.download_is_active(dl_id)
    async with maker() as session:
        assert (await session.get(Download, dl_id)).status == DownloadStatus.DONE
        assert (await session.scalar(select(Issue))).downloaded
        assert (await session.scalars(select(HistoryEvent.event))).all() == ["imported"]
    assert len(list(library.rglob("*.cbz"))) == 1
