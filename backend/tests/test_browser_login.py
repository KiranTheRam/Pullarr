import asyncio
import stat
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr import browser_setup, reset_password, sign_in_throttle
from pullarr.api import deps
from pullarr.browser_setup import LOGIN_FILE
from pullarr.config import config
from pullarr.db import get_session
from pullarr.main import api, app
from pullarr.models import ApiKey, Base


@pytest.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'security.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def client(session, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "data_dir", tmp_path)
    monkeypatch.setattr(config, "auth_username", "")
    monkeypatch.setattr(config, "auth_password", SecretStr(""))
    monkeypatch.setattr(config, "allow_unauthenticated_browser", False)
    monkeypatch.setattr(config, "allowed_hosts", ["reader.example"])
    monkeypatch.setattr(deps, "_api_key", "bootstrap")
    monkeypatch.setattr(browser_setup, "_sign_in_throttle", sign_in_throttle.SignInThrottle())

    async def override():
        yield session

    api.dependency_overrides[get_session] = override
    with_session = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")
    async with with_session as client:
        yield client
    api.dependency_overrides.clear()


def enable_login(monkeypatch):
    monkeypatch.setattr(config, "auth_username", "reader")
    monkeypatch.setattr(config, "auth_password", SecretStr("password"))


async def test_browser_bootstrap_and_independent_api_auth(client, session, monkeypatch):
    response = await client.get("/initialize.json", headers={"X-Forwarded-For": "127.0.0.1"})
    assert response.status_code == 403
    assert "bootstrap" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/api/v1/settings", headers={"X-Api-Key": "bootstrap"})).status_code == 200
    monkeypatch.setattr(config, "allow_unauthenticated_browser", True)
    assert (await client.get("/initialize.json")).json()["apiKey"] == "bootstrap"
    enable_login(monkeypatch)
    for path in ("/", "/initialize.json", "/docs", "/openapi.json", "/assets/missing", "/api/v1-other"):
        response = await client.get(path, headers={"X-Api-Key": "bootstrap", "X-Forwarded-User": "reader"})
        assert response.status_code == 401
        assert "bootstrap" not in response.text
        assert response.headers["www-authenticate"].startswith("Basic ")
    for auth in (httpx.BasicAuth("reader", "wrong"), httpx.BasicAuth("wrong", "password")):
        assert (await client.get("/initialize.json", auth=auth)).status_code == 401
    for value in ("Bearer bogus", "Basic !!!", "Basic Og=="):
        assert (await client.get("/initialize.json", headers={"Authorization": value})).status_code == 401
    response = await client.get("/initialize.json", auth=httpx.BasicAuth("reader", "password"))
    assert response.json()["apiKey"] == "bootstrap"
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/api/v1/settings", auth=httpx.BasicAuth("reader", "password"))).status_code == 401
    assert (await client.get("/api/v1/settings", headers={"X-Api-Key": "wrong"})).status_code == 401
    session.add(ApiKey(name="integration", key="managed"))
    await session.commit()
    for key in ("managed", "bootstrap"):
        assert (await client.get("/api/v1/settings", headers={"X-Api-Key": key})).status_code == 200


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST:6996", "192.0.2.1:6996", "[::1]:6996", "reader.example:443"])
async def test_allowed_hosts(client, host):
    assert (await client.get("/setup", headers={"Host": host})).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "localhost.evil.example", "reader.example.evil", "localhost@evil.example", "localhost/path", "localhost#evil", "localhost:99999", "localhost:abc", "localhost\\evil", "", "localhost.", " localhost"])
async def test_rebinding_and_malformed_hosts_rejected_everywhere(client, host):
    for path in ("/initialize.json", "/api/v1/settings", "/assets/missing"):
        response = await client.get(path, headers={"Host": host, "X-Forwarded-Host": "localhost", "X-Api-Key": "bootstrap"})
        assert response.status_code == 400
        assert "bootstrap" not in response.text


async def test_duplicate_and_missing_host(client):
    assert (await client.get("/setup", headers=[("Host", "localhost"), ("Host", "evil.example")])).status_code == 400
    request = client.build_request("GET", "/setup")
    del request.headers["host"]
    assert (await client.send(request)).status_code == 400


async def test_mount_path_cannot_bypass_browser_login(client, monkeypatch):
    transport = httpx.ASGITransport(app=app, root_path="/api/v1")
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as mounted:
        assert (await mounted.get("/api/v1/")).headers["location"] == "/api/v1/setup"
        assert (await mounted.get("/api/v1/initialize.json")).status_code == 403
        enable_login(monkeypatch)
        assert (await mounted.get("/api/v1/initialize.json")).status_code == 401
        response = await mounted.get("/api/v1/initialize.json", auth=("reader", "password"))
        assert response.json()["apiKey"] == "bootstrap"
        assert response.headers["cache-control"] == "no-store"
        assert (await mounted.get("/api/v1/api/v1/settings", headers={"X-Api-Key": "bootstrap"})).status_code == 200


async def test_browser_setup_keeps_existing_api_and_accepts_one_login(client, tmp_path, monkeypatch):
    page = await client.get("/", follow_redirects=True)
    assert page.url.path == "/setup"
    assert "Set up your Pullarr login" in page.text
    assert "Use 8 to 128 characters" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert 'name="code"' not in page.text
    assert page.text.count('type="password" required maxlength="256"') == 2
    assert (await client.get("/initialize.json")).status_code == 403
    assert (await client.get("/api/v1/settings", headers={"X-Api-Key": "bootstrap"})).status_code == 200
    password = "abcdefgh"
    data = {"username": "reader", "password": password, "confirmation": password}
    headers = {"Origin": "http://localhost"}
    assert (await client.post("/setup", data=data)).status_code == 403
    assert (await client.post("/setup", data=data, headers={"Origin": "https://other.example"})).status_code == 403
    assert (await client.post("/setup", data=data, headers={**headers, "Sec-Fetch-Site": "cross-site"})).status_code == 403
    login_file = tmp_path / LOGIN_FILE
    assert not login_file.exists()
    rejected = await client.post("/setup", data={**data, "confirmation": "mismatch"}, headers=headers)
    assert rejected.status_code == 400
    assert 'value="reader"' in rejected.text and password not in rejected.text
    escaped = await client.post("/setup", data={"username": '<img src=x onerror="alert(1)">', "password": password}, headers=headers)
    assert escaped.status_code == 400
    assert "&lt;img" in escaped.text and "<img" not in escaped.text
    assert (await client.post("/setup", data={**data, "password": "x" * 8192}, headers=headers)).status_code == 413
    responses = await asyncio.gather(
        client.post("/setup", data=data, headers=headers),
        client.post("/setup", data={**data, "username": "other"}, headers=headers),
    )
    assert any(r.status_code == 303 and r.headers["location"] == "/" for r in responses)
    assert stat.S_IMODE(login_file.stat().st_mode) == 0o600
    assert password not in login_file.read_text()
    assert client.cookies.get(browser_setup.SESSION_COOKIE)
    assert (await client.get("/api/v1/settings")).status_code == 200
    client.cookies.clear()

    monkeypatch.setattr(config, "allow_unauthenticated_browser", True)
    assert (await client.get("/initialize.json")).status_code == 401
    assert (await client.get("/", follow_redirects=True)).url.path == "/login"
    recovery = (await client.get("/login")).text
    assert "Forgot your username or password?" in recovery
    assert "python -m pullarr.reset_password" in recovery
    assert password not in recovery
    for name in ("reader", "other"):
        response = await client.post("/login", data={"username": name, "password": password}, headers=headers)
        if response.status_code == 303:
            assert "HttpOnly" in response.headers["set-cookie"]
            break
        assert response.status_code == 401
    else:
        pytest.fail("Neither claimant could sign in")
    rival = "other" if name == "reader" else "reader"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as anonymous:
        assert (await anonymous.post("/login", data={"username": rival, "password": password}, headers=headers)).status_code == 401
    response = await client.get("/initialize.json")
    assert response.json()["apiKey"] == "" and response.json()["authMode"] == "managed"
    assert response.headers["cache-control"] == "no-store"
    assert (await client.get("/api/v1/settings")).status_code == 200
    assert (await client.put("/api/v1/settings", json={}, headers={"Origin": "http://evil.example"})).status_code == 403
    assert (await client.put("/api/v1/settings", json={}, headers=headers)).status_code == 200
    assert (await client.post("/setup", data={**data, "username": "attacker"}, headers=headers)).status_code == 303


async def test_password_change_and_logout_preserve_api_keys(client, session, monkeypatch):
    monkeypatch.setattr(browser_setup, "engine", session.bind)
    headers = {"Origin": "http://localhost"}
    password = "old fixture password"
    new = "new fixture password"
    assert (await client.post("/setup", data={"username": "reader", "password": password, "confirmation": password}, headers=headers)).status_code == 303
    client.cookies.clear()
    payload = {"current": password, "password": new, "confirmation": new}
    assert (await client.post("/auth/password", data=payload, headers={**headers, "X-Api-Key": "bootstrap"})).status_code == 401
    assert (await client.post("/login", data={"username": "reader", "password": password}, headers=headers)).status_code == 303
    cookie = client.cookies.get(browser_setup.SESSION_COOKIE)
    assert (await client.post("/auth/password", data=payload, headers={"Origin": "http://evil.example"})).status_code == 403
    assert (await client.post("/auth/password", data={**payload, "current": "wrong"}, headers=headers)).status_code == 400
    assert (await client.post("/auth/password", data={**payload, "confirmation": "wrong"}, headers=headers)).status_code == 400
    assert (await client.post("/auth/password", data=payload, headers=headers)).status_code == 204
    assert client.cookies.get(browser_setup.SESSION_COOKIE) is None
    assert (await client.get("/api/v1/settings", headers={"Cookie": f"{browser_setup.SESSION_COOKIE}={cookie}"})).status_code == 401
    assert (await client.get("/api/v1/settings", headers={"X-Api-Key": "bootstrap"})).status_code == 200
    assert (await client.post("/login", data={"username": "reader", "password": password}, headers=headers)).status_code == 401
    assert (await client.post("/login", data={"username": "reader", "password": new}, headers=headers)).status_code == 303
    assert (await client.get("/api/v1/settings")).status_code == 200
    assert (await client.post("/auth/logout", headers=headers)).status_code == 204
    assert (await client.get("/api/v1/settings")).status_code == 401


async def test_sign_in_throttle_checks_peer_before_password_verification(client, monkeypatch):
    now = 100.0
    monkeypatch.setattr(sign_in_throttle, "monotonic", lambda: now)
    password = "fixture password"
    headers = {"Origin": "http://localhost"}
    login = {"username": "reader", "password": password}
    assert (await client.post("/setup", data={**login, "confirmation": password}, headers=headers)).status_code == 303
    client.cookies.clear()

    async def failed(index):
        # Changing the username, claimed address or peer port cannot bypass the limit.
        transport = httpx.ASGITransport(app=app, client=("192.0.2.5", 1500 + index))
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as peer:
            return await peer.post("/login", data={"username": f"wrong-{index}", "password": "wrong"},
                                   headers={**headers, "X-Forwarded-For": f"198.51.100.{index + 1}"})

    failures = await asyncio.gather(*(failed(index) for index in range(sign_in_throttle.ATTEMPTS + 1)))
    assert sorted(response.status_code for response in failures) == [401] * sign_in_throttle.ATTEMPTS + [429]
    blocked = next(response for response in failures if response.status_code == 429)
    assert blocked.headers["retry-after"] == str(sign_in_throttle.WINDOW_SECONDS)
    assert blocked.headers["cache-control"] == "no-store"
    assert (await client.get("/api/v1/settings", headers={"X-Api-Key": "bootstrap"})).status_code == 200
    assert (await client.post("/login", data=login, headers=headers)).status_code == 303
    now += sign_in_throttle.WINDOW_SECONDS
    assert (await failed(0)).status_code == 401


async def test_local_reset_preserves_username_and_recovers_throttled_peer(client, session, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(browser_setup, "engine", session.bind)
    headers = {"Origin": "http://localhost"}
    old = "old fixture password"
    new = "\U0001f680" * 128
    login = {"username": "reader", "password": old}
    assert (await client.post("/setup", data={**login, "confirmation": old}, headers=headers)).status_code == 303
    assert (await client.post("/login", data=login, headers=headers)).status_code == 303
    cookie = client.cookies.get(browser_setup.SESSION_COOKIE)
    client.cookies.clear()
    for _ in range(sign_in_throttle.ATTEMPTS):
        assert (await client.post("/login", data={**login, "username": "wrong"}, headers=headers)).status_code == 401
    assert (await client.post("/login", data=login, headers=headers)).status_code == 429

    monkeypatch.setattr(reset_password.sys, "stdin", Mock(isatty=Mock(return_value=True)))
    monkeypatch.setattr(reset_password, "getpass", Mock(side_effect=[new, new]))
    assert await asyncio.to_thread(reset_password.main, []) == 0
    output = capsys.readouterr().out
    assert "'reader'" in output and new not in output

    assert stat.S_IMODE((tmp_path / LOGIN_FILE).stat().st_mode) == 0o600
    assert (await client.get("/")).headers["location"] == "/login"
    assert 'type="password" required maxlength="256"' in (await client.get("/login")).text
    assert (await client.get("/api/v1/settings", headers={"Cookie": f"{browser_setup.SESSION_COOKIE}={cookie}"})).status_code == 401
    assert (await client.get("/api/v1/settings", headers={"X-Api-Key": "bootstrap"})).status_code == 200
    assert (await client.post("/login", data=login, headers=headers)).status_code == 401
    assert (await client.post("/login", data={**login, "password": new}, headers=headers)).status_code == 303
    assert (await client.post("/auth/password", data={"current": new, "password": old, "confirmation": old},
                              headers=headers)).status_code == 204


def test_local_reset_requires_existing_managed_login(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "data_dir", tmp_path)
    monkeypatch.setattr(config, "auth_username", "")
    prompt = Mock()
    monkeypatch.setattr(reset_password, "getpass", prompt)
    assert reset_password.main([]) == 1
    assert "There is no browser login" in capsys.readouterr().err
    assert not (tmp_path / LOGIN_FILE).exists()

    enable_login(monkeypatch)
    assert reset_password.main([]) == 1
    assert "PULLARR_AUTH_PASSWORD" in capsys.readouterr().err
    prompt.assert_not_called()



async def test_expired_tampered_and_wrong_revision_sessions_are_rejected(client, monkeypatch):
    from starlette.requests import Request
    from pullarr.browser_setup import BrowserLogin, SESSION_COOKIE

    login = BrowserLogin("reader", b"fixture-salt", b"fixture-digest")
    token = login.session_token()
    def request(value):
        return Request({"type": "http", "headers": [(b"cookie", f"{SESSION_COOKIE}={value}".encode())]})
    assert login.has_session(request(token))
    assert not login.has_session(request(token + "x"))
    assert not BrowserLogin("reader", b"new-salt", b"new-digest").has_session(request(token))
    monkeypatch.setattr(browser_setup.time, "time", lambda: 10**12)
    assert not login.has_session(request(token))


def test_sign_in_throttle_bounds_peers_pending_and_ipv6_prefixes(monkeypatch):
    monkeypatch.setattr(sign_in_throttle, "MAX_PENDING", 1)
    monkeypatch.setattr(sign_in_throttle, "MAX_PEERS", 1)
    limiter = sign_in_throttle.SignInThrottle()
    attempt = limiter.reserve("2001:db8::1", b"revision")
    assert not isinstance(attempt, int)
    assert isinstance(limiter.reserve("2001:db8::2", b"revision"), int)
    limiter.finish("2001:db8::1", attempt, success=False)
    assert len(limiter._windows) == 1
    assert isinstance(limiter.reserve("192.0.2.3", b"revision"), int)
    # A credential replacement clears old client lockouts; reset remains usable.
    attempt = limiter.reserve("192.0.2.3", b"new-revision")
    assert not isinstance(attempt, int)
    limiter.finish("192.0.2.3", attempt, success=True)
    assert not limiter._windows


@pytest.mark.parametrize("password, accepted", [
    ("abcdefg", False), ("abcdefgh", True), ("12345678", True),
    ("ABCDEFGH", True), ("!@#$%^&*", True), ("a" * 128, True),
    ("a" * 129, False), ("abcdefg\n", False),
])
def test_password_length_without_composition_requirements(password, accepted):
    assert browser_setup.valid_password(password, password) is accepted
    assert not browser_setup.valid_password(password, password + "x")
