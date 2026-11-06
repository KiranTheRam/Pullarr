from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr

from pullarr import main, security
from pullarr.api import deps
from pullarr.browser_setup import LOGIN_FILE
from pullarr.config import config
from pullarr.db import get_session


@pytest.fixture
async def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "data_dir", tmp_path)
    monkeypatch.setattr(config, "auth_username", "")
    monkeypatch.setattr(config, "auth_password", SecretStr(""))
    monkeypatch.setattr(config, "allow_unauthenticated_browser", False)
    monkeypatch.setattr(config, "allowed_hosts", [])
    monkeypatch.setattr(deps, "_api_key", "configured-secret")

    async def no_database():
        # An unknown integration key gets a database lookup, then a 401.
        yield Mock(execute=AsyncMock(return_value=Mock(scalar_one_or_none=Mock(return_value=None))))

    main.api.dependency_overrides[get_session] = no_database
    try:
        # No lifespan: ping must work without a database or scheduler startup.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://localhost") as client:
            yield client
    finally:
        main.api.dependency_overrides.pop(get_session, None)


@pytest.mark.parametrize("mode", ["setup", "environment", "managed", "external"])
async def test_ping_is_public_and_does_not_load_login_or_api_key(client, monkeypatch, tmp_path, mode):
    if mode == "environment":
        monkeypatch.setattr(config, "auth_username", "reader")
        monkeypatch.setattr(config, "auth_password", SecretStr("password-secret"))
    elif mode == "managed":
        (tmp_path / LOGIN_FILE).write_text("must not read this")
    elif mode == "external":
        monkeypatch.setattr(config, "allow_unauthenticated_browser", True)
    monkeypatch.setattr(security, "load_browser_login", Mock(side_effect=AssertionError("login touched")))
    monkeypatch.setattr(config.__class__, "resolve_api_key", Mock(side_effect=AssertionError("key touched")))
    for headers in ({}, {"X-Api-Key": "wrong", "Authorization": "Basic !!!"}):
        response = await client.get("/ping", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "OK"}
        assert response.headers["cache-control"] == "no-store"
        assert "secret" not in response.text
        assert "set-cookie" not in response.headers


async def test_ping_does_not_exempt_similar_paths_or_api(client):
    for path in ("/ping/", "/ping/initialize.json", "/ping-other", "/initialize.json"):
        assert (await client.get(path)).status_code in {307, 403}
    for headers in ({}, {"X-Api-Key": "wrong"}):
        assert (await client.get("/api/v1/system/status", headers=headers)).status_code == 401
    assert (await client.get("/api/v1/ping")).status_code == 404
    assert (await client.post("/ping")).status_code == 403


@pytest.mark.parametrize("host", ["evil.example", "localhost:abc", "localhost:99999", "", "localhost@evil.example"])
async def test_ping_retains_host_boundary(client, host):
    response = await client.get("/ping", headers={"Host": host, "X-Forwarded-Host": "localhost"})
    assert response.status_code == 400


async def test_ping_rejects_missing_and_duplicate_hosts(client):
    assert (await client.get("/ping", headers=[("Host", "localhost"), ("Host", "evil.example")])).status_code == 400
    request = client.build_request("GET", "/ping")
    del request.headers["host"]
    assert (await client.send(request)).status_code == 400


async def test_ping_mount_uses_same_exact_boundary(client):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app, root_path="/api/v1"), base_url="http://localhost") as mounted:
        assert (await mounted.get("/api/v1/ping")).json() == {"status": "OK"}
        assert (await mounted.get("/api/v1/initialize.json")).status_code == 403
        assert (await mounted.get("/api/v1/api/v1/system/status")).status_code == 401


def test_server_entrypoint_uses_configured_listener_and_forwarded_policy(monkeypatch):
    import uvicorn

    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(config, "host", "::1")
    monkeypatch.setattr(config, "port", 8123)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "127.0.0.1")
    main.run()
    run.assert_called_once_with("pullarr.main:app", host="::1", port=8123, log_level="info", forwarded_allow_ips="127.0.0.1")
