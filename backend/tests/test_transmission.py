import base64
import hashlib
import json
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr import settings_service
from pullarr.api import queue, search, settings
from pullarr.db import _migrate_sqlite
from pullarr.download import clients
from pullarr.download.torrent import TorrentClientError
from pullarr.download.transmission import SESSION_HEADER, TransmissionClient
from pullarr.jobs import tasks
from pullarr.models import Base, Download, DownloadKind, DownloadStatus, Issue, RootFolder, Series
from pullarr.schemas import GrabIn, TorrentClientTestIn
from pullarr.sources.base import TorrentRelease

RPC = "http://transmission:9091/transmission/rpc"
INFO = b"d4:name4:test6:lengthi123ee"
TORRENT = b"d4:info" + INFO + b"e"
HASH = hashlib.sha1(INFO).hexdigest()
MAGNET = f"magnet:?xt=urn:btih:{HASH}"


def rpc_result(**arguments):
    return httpx.Response(200, json={"result": "success", "arguments": arguments})


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@respx.mock
async def test_session_challenge_and_basic_auth():
    route = respx.post(RPC).mock(side_effect=[
        httpx.Response(409, headers={SESSION_HEADER: "session-id"}),
        rpc_result(version="4.1.3"),
    ])
    client = TransmissionClient(RPC, "reader", "password")
    try:
        assert await client.version() == "4.1.3"
        first, retried = (call.request for call in route.calls)
        assert SESSION_HEADER not in first.headers
        assert retried.headers[SESSION_HEADER] == "session-id"
        assert first.content == retried.content
        auth = base64.b64encode(b"reader:password").decode()
        assert retried.headers["Authorization"] == f"Basic {auth}"
    finally:
        await client.close()


@respx.mock
async def test_transmission_saved_password_cannot_move_to_another_endpoint(session):
    await settings_service.set_many(session, {
        "transmission_url": RPC, "transmission_username": "reader", "transmission_password": "saved",
    })
    assert (await settings.get_settings(session))["transmission_password"] == settings.MASK
    body = TorrentClientTestIn(url="http://elsewhere:9091/transmission/rpc", username="reader", password=settings.MASK)
    with pytest.raises(HTTPException) as denied:
        await settings.transmission_test(body, session)
    assert denied.value.status_code == 422
    with pytest.raises(HTTPException) as denied:
        await settings.update_settings({"transmission_url": body.url}, session)
    assert denied.value.status_code == 422
    route = respx.post(RPC).mock(return_value=rpc_result(version="4.1.3"))
    result = await settings.transmission_test(body.model_copy(update={"url": RPC}), session)
    assert result == {"ok": True, "version": "4.1.3"}
    assert route.calls.last.request.headers["Authorization"] == "Basic " + base64.b64encode(b"reader:saved").decode()


@respx.mock
async def test_rpc_rejection_redacts_response_and_blank_removal_sends_nothing():
    route = respx.post(RPC).respond(json={"result": "failed: https://example/?apikey=secret"})
    client = TransmissionClient(RPC, "", "")
    try:
        await client.delete_torrents([])
        await client.delete_torrents(["unknown"])
        assert route.call_count == 0
        with pytest.raises(TorrentClientError, match="Transmission rejected session-get") as error:
            await client.version()
        assert "secret" not in str(error.value)
    finally:
        await client.close()


@pytest.mark.parametrize("field,value", [
    ("torrent_download_client", "unknown"), ("transmission_url", "file:///rpc"),
    ("transmission_url", "http://user:secret@transmission/rpc"),
    ("transmission_url", "http://transmission/rpc?key=secret"),
    ("transmission_download_dir", "downloads"),
])
def test_transmission_configuration_validation(field, value):
    with pytest.raises(ValueError):
        settings_service.validate_updates({field: value})


async def test_existing_torrents_are_migrated_to_qbittorrent():
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE downloads (id INTEGER PRIMARY KEY)")
            await conn.run_sync(Issue.__table__.create)
            await conn.exec_driver_sql("INSERT INTO downloads (id) VALUES (1)")
            await _migrate_sqlite(conn)
            await _migrate_sqlite(conn)
            assert (await conn.exec_driver_sql("SELECT torrent_client FROM downloads")).scalar_one() == "qbittorrent"
    finally:
        await engine.dispose()


async def library(session, tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    series = Series(title="Test Series", root_folder=RootFolder(path=str(root)))
    series.issues = [Issue(number=1, display_number="1"), Issue(number=2, display_number="2")]
    session.add(series)
    await session.commit()
    return series


@respx.mock
async def test_torznab_search_and_monitor_work_with_transmission_only(session, monkeypatch, tmp_path):
    series = await library(session, tmp_path)
    await settings_service.set_many(session, {"transmission_enabled": "true", "transmission_url": RPC,
                                              "source_getcomics_enabled": "false"})
    indexer = SimpleNamespace(name="Indexer", search=AsyncMock(return_value=[
        TorrentRelease(source_name="Indexer", title="Test Series 1", magnet=MAGNET, seeders=5, url="",
                       size_bytes=123, leechers=0),
    ]))
    monkeypatch.setattr(tasks.registry, "enabled_torrent_indexers", lambda _: [indexer])
    found = await search.search_releases(issue_id=series.issues[0].id, session=session, q="Test Series 1")
    assert [item.magnet for item in found] == [MAGNET]
    route = respx.post(RPC).mock(return_value=rpc_result(**{"torrent-added": {"hashString": HASH}}))
    values = await settings_service.get_all(session)
    assert await tasks.grab_missing_issues(session, series, values) == 1
    assert json.loads(route.calls.last.request.content)["arguments"] == {"filename": MAGNET}


@respx.mock
async def test_grab_import_retry_and_remove_follow_original_client(session, monkeypatch, tmp_path):
    series = await library(session, tmp_path)
    issue, other = series.issues
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    nested = downloads / "pack"
    nested.mkdir()
    for filename in ("Test Series #1.cbz", "Test Series #2.cbz"):
        with zipfile.ZipFile(nested / filename, "w") as archive:
            archive.writestr("001.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    await settings_service.set_many(session, {
        "transmission_enabled": "true", "transmission_url": RPC,
        "transmission_download_dir": str(downloads), "qbittorrent_enabled": "true",
        "torrent_download_client": "transmission",
    })
    qbt = AsyncMock()
    monkeypatch.setattr(clients, "QbtClient", qbt)
    request_log = []

    def rpc(request):
        body = json.loads(request.content)
        method, arguments = body["method"], body["arguments"]
        request_log.append(body)
        if method == "torrent-add":
            assert base64.b64decode(arguments["metainfo"]) == TORRENT
            assert arguments["download-dir"] == str(downloads)
            return rpc_result(**{"torrent-added": {"hashString": HASH}})
        assert arguments["ids"] == [HASH]
        if method == "torrent-remove":
            assert arguments["delete-local-data"] is True
            return rpc_result()
        assert method == "torrent-get"
        if "files" in arguments["fields"]:
            return rpc_result(torrents=[{
                "hashString": HASH, "files": [
                    {"name": "pack/Test Series #1.cbz"}, {"name": "pack/Test Series #2.cbz"},
                ], "wanted": [1, 0],
            }])
        return rpc_result(torrents=[{
            "hashString": HASH, "downloadDir": str(downloads), "percentDone": 1,
            "metadataPercentComplete": 1, "status": 6,
        }])

    respx.post(RPC).mock(side_effect=rpc)
    torrent_url = "http://indexer/torrent"
    respx.get(torrent_url).respond(content=TORRENT)
    added = await queue.grab(GrabIn(series_id=series.id, issue_id=issue.id, magnet=torrent_url,
                                    title="Test Series #1"), session)
    row = await session.get(Download, added.id)
    assert (row.kind, row.torrent_client, row.torrent_hash) == (DownloadKind.TORRENT, "transmission", HASH)
    assert added.torrent_client == "transmission"
    await settings_service.set_many(session, {"torrent_download_client": "qbittorrent"})

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(tasks, "session_scope", scope)
    await tasks.sync_torrents("qbittorrent")
    await tasks.sync_torrents("transmission")
    qbt.assert_not_called()
    assert row.status == DownloadStatus.DONE
    assert issue.downloaded and not other.downloaded
    assert (nested / "Test Series #1.cbz").exists()
    row.status = DownloadStatus.FAILED
    await session.commit()
    assert (await queue.retry_download(row.id, session)).status == DownloadStatus.DOWNLOADING
    assert await queue._remove_downloads(session, [row.id]) == 1
    assert row.status == DownloadStatus.FAILED
    assert row.error_code == "cancelled"
    assert [body["method"] for body in request_log] == [
        "torrent-add", "torrent-get", "torrent-get", "torrent-get", "torrent-remove",
    ]
    qbt.assert_not_called()


@pytest.mark.parametrize("metadata,progress,status,done", [
    (0, 1, 4, False), (1, 0.4, 4, False), (1, 1, 2, False),
    (1, 1, 0, True), (1, 1, 6, True),
])
@respx.mock
async def test_transfer_completion_requires_metadata_and_finished_verification(metadata, progress, status, done):
    respx.post(RPC).mock(return_value=rpc_result(torrents=[{
        "hashString": HASH, "downloadDir": "/downloads", "percentDone": progress,
        "metadataPercentComplete": metadata, "status": status,
    }]))
    client = TransmissionClient(RPC, "", "")
    try:
        transfer = await client.get_torrent(HASH)
        assert transfer is not None and transfer.progress == progress and transfer.is_complete is done
    finally:
        await client.close()
