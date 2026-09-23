import base64

import httpx
import pytest
import respx
from fastapi import HTTPException

from pullarr.api import queue
from pullarr.download.qbittorrent import QbtClient
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
    with pytest.raises(httpx.HTTPStatusError):
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
    route = respx.post(f"{BASE}/api/v2/torrents/delete").respond(200)
    await client().delete_torrents(["abc", "def"])
    body = route.calls.last.request.content.decode()
    assert "hashes=abc%7Cdef" in body
    assert "deleteFiles=true" in body


HEX_HASH = "0123456789abcdef0123456789abcdef01234567"
BASE32_HASH = base64.b32encode(bytes.fromhex(HEX_HASH)).decode()


@pytest.mark.parametrize("magnet,expected", [
    (f"magnet:?xt=urn:btih:{HEX_HASH}&dn=x", HEX_HASH),
    (f"magnet:?xt=urn:btih:{HEX_HASH.upper()}", HEX_HASH),
    # older base32 links must match the hex hash qBittorrent reports
    (f"magnet:?xt=urn:btih:{BASE32_HASH}&dn=x", HEX_HASH),
    ("magnet:?xt=urn:btih:abc", ""),
    ("magnet:?dn=no-hash", ""),
])
def test_magnet_btih_hex(magnet, expected):
    assert tasks.magnet_btih_hex(magnet) == expected


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
