import httpx
import pytest
import respx

from pullarr.download.qbittorrent import QbtClient

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
async def test_add_magnet_sends_category_and_savepath():
    route = respx.post(f"{BASE}/api/v2/torrents/add").respond(200, text="Ok.")
    await client().add_magnet("magnet:?xt=urn:btih:abc", category="pullarr",
                              save_path="/downloads/pullarr")
    body = route.calls.last.request.content.decode()
    assert "category=pullarr" in body
    assert "savepath=%2Fdownloads%2Fpullarr" in body
    assert "autoTMM=false" in body
