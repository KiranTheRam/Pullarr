import base64

import httpx
import pytest
import respx
from fastapi import HTTPException

from pullarr.api import queue
from pullarr.download.qbittorrent import QbtClient, QbtError
from pullarr.download.torrent import magnet_btih_hex
from pullarr.jobs import tasks
from pullarr.schemas import GrabIn

BASE = "http://qbt:8080"


def client():
    c = QbtClient(BASE, "admin", "pw")
    c._logged_in = True  # skip the login round-trip
    return c


@respx.mock
async def test_default_save_path():
    respx.get(f"{BASE}/api/v2/app/preferences").respond(json={"save_path": "/downloads"})
    assert await client().default_save_path() == "/downloads"


@respx.mock
async def test_default_save_path_missing_is_empty():
    respx.get(f"{BASE}/api/v2/app/preferences").respond(json={})
    assert await client().default_save_path() == ""


@respx.mock
async def test_ensure_category_created():
    route = respx.post(f"{BASE}/api/v2/torrents/createCategory").respond(200)
    await client().ensure_category("pullarr", "/downloads/pullarr")
    assert route.called
    sent = route.calls.last.request.content.decode()
    assert "category=pullarr" in sent and "savePath=%2Fdownloads%2Fpullarr" in sent


@respx.mock
async def test_ensure_category_conflict_edits():
    respx.post(f"{BASE}/api/v2/torrents/createCategory").respond(409)
    edit = respx.post(f"{BASE}/api/v2/torrents/editCategory").respond(200)
    await client().ensure_category("pullarr", "/downloads/pullarr")
    assert edit.called  # already exists → path updated instead


@respx.mock
async def test_ensure_category_raises_on_server_error():
    respx.post(f"{BASE}/api/v2/torrents/createCategory").respond(500)
    with pytest.raises(QbtError):
        await client().ensure_category("pullarr", "/downloads/pullarr")


@respx.mock
async def test_add_magnet_sends_category_and_savepath():
    route = respx.post(f"{BASE}/api/v2/torrents/add").respond(200, text="Ok.")
    await client().add_magnet("magnet:?xt=urn:btih:abc", category="pullarr",
                              save_path="/downloads/pullarr")
    body = route.calls.last.request.content.decode()
    assert "category=pullarr" in body
    assert "savepath=%2Fdownloads%2Fpullarr" in body
    assert "autoTMM=false" in body


@respx.mock
async def test_delete_torrents_removes_hashes_and_files():
    respx.get(f"{BASE}/api/v2/torrents/info").respond(json=[
        {"hash": "C" * 40, "infohash_v1": "A" * 40},
        {"hash": "b" * 40},  # Older qBittorrent has no separate v1 hash.
    ])
    route = respx.post(f"{BASE}/api/v2/torrents/delete").respond(200)
    qbt = client()
    try:
        await qbt.delete_torrents(["a" * 40, "B" * 40])
    finally:
        await qbt.close()
    body = route.calls.last.request.content.decode()
    assert f"hashes={'c' * 40}%7C{'b' * 40}" in body
    assert "deleteFiles=true" in body


@pytest.mark.parametrize("invalid_id", ["all", "a" * 40 + "|" + "b" * 40, "g" * 40, "a" * 64])
@pytest.mark.parametrize("source", ["alias", "caller"])
@respx.mock
async def test_delete_torrents_rejects_invalid_ids_without_sending_delete(invalid_id, source):
    listing = respx.get(f"{BASE}/api/v2/torrents/info").respond(json=[
        {"hash": invalid_id, "infohash_v1": "a" * 40},
    ])
    delete = respx.post(f"{BASE}/api/v2/torrents/delete").respond(200)
    qbt = client()
    try:
        requested = ["a" * 40] if source == "alias" else ["b" * 40, invalid_id]
        with pytest.raises(QbtError, match="Invalid qBittorrent torrent ID"):
            await qbt.delete_torrents(requested)
    finally:
        await qbt.close()
    assert listing.called == (source == "alias")
    assert not delete.called


HEX_HASH = "0123456789abcdef0123456789abcdef01234567"
BASE32_HASH = base64.b32encode(bytes.fromhex(HEX_HASH)).decode()


@pytest.mark.parametrize("magnet,expected", [
    (f"magnet:?xt=urn:btih:{HEX_HASH}&dn=x", HEX_HASH),
    (f"magnet:?xt=urn:btih:{HEX_HASH.upper()}", HEX_HASH),
    # older base32 links must match the hex hash qBittorrent reports
    (f"magnet:?xt=urn:btih:{BASE32_HASH}&dn=x", HEX_HASH),
    (f"magnet:?xt=urn:btih:{BASE32_HASH.lower()}", HEX_HASH),
    (f"magnet:?dn=urn:btih:{HEX_HASH}", ""),
    ("magnet:?xt=urn:btih:abc", ""),
    ("magnet:?dn=no-hash", ""),
])
def test_magnet_btih_hex(magnet, expected):
    assert magnet_btih_hex(magnet) == expected


@respx.mock
async def test_hybrid_lookup_falls_back_to_v1_alias_and_reuses_listing():
    hybrid_id = "c" * 40
    other_v1 = "d" * 40
    other_id = "e" * 40
    for info_hash in (HEX_HASH, other_v1):
        respx.get(f"{BASE}/api/v2/torrents/info", params={"hashes": info_hash}).respond(json=[])
    listing = respx.get(f"{BASE}/api/v2/torrents/info").respond(json=[
        {"hash": hybrid_id, "infohash_v1": HEX_HASH, "progress": 1},
        {"hash": other_id, "infohash_v1": other_v1, "progress": 0.5},
    ])
    qbt = client()
    try:
        hybrid = await qbt.get_torrent(HEX_HASH)
        assert hybrid and hybrid.hash == hybrid_id and hybrid.is_complete
        other = await qbt.get_torrent(other_v1)
        assert other and other.hash == other_id and not other.is_complete
        assert listing.call_count == 1
    finally:
        await qbt.close()


async def test_submit_torrent_rejects_magnet_without_btih():
    with pytest.raises(ValueError, match="btih"):
        await tasks.submit_torrent("magnet:?dn=no-hash", {})


async def test_grab_rejects_magnet_without_btih(monkeypatch):
    async def fake_settings(session):
        return {"qbittorrent_enabled": "true"}

    monkeypatch.setattr(queue.registry, "apply_settings", fake_settings)

    with pytest.raises(HTTPException) as exc:
        await queue.grab(GrabIn(magnet="magnet:?dn=no-hash"), session=None)

    assert exc.value.status_code == 422
