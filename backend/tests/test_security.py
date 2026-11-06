import base64
import json
import logging
import zipfile

import httpx
import pytest
import respx
from fastapi import FastAPI, HTTPException
from pydantic import SecretStr, ValidationError

from pullarr import kavita, notifications, security
from pullarr.api import settings as settings_api
from pullarr.config import AppConfig
from pullarr.download.cbz import MAX_COMICINFO_BYTES, read_comicinfo, upsert_comicinfo
from pullarr.download.qbittorrent import QbtClient, QbtError, test_connection as qbt_connect
from pullarr.download.torrent import fetch_torrent
from pullarr.library.matcher import _comicinfo_identity
from pullarr.metadata.comicvine import ComicVineError, ComicVineProvider
from pullarr.schemas import KavitaTestIn, QbtTestIn, WebhookTestIn
from pullarr.settings_service import get_all
from pullarr.sources.torznab import MASK, TorznabIndexer, restore_keys

SECRET = "SECRETKEY123"


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def browser_app():
    app = FastAPI()
    app.middleware("http")(security.access_boundary)
    api = FastAPI()

    @api.get("/thing")
    async def thing():
        return {"api": True}

    app.mount("/api/v1", api)

    @app.get("/initialize.json")
    async def initialize():
        return {"apiKey": "key"}

    return app


def client_for(app, host: str = "127.0.0.1:6997") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=f"http://{host}"
    )


@pytest.fixture
def no_login(monkeypatch):
    monkeypatch.setattr(security.config, "allow_unauthenticated_browser", True)
    monkeypatch.setattr(security.config, "auth_username", "")
    monkeypatch.setattr(security.config, "auth_password", SecretStr(""))
    monkeypatch.setattr(security.config, "allowed_hosts", ["comics.example.com"])


@pytest.fixture
def login(monkeypatch):
    monkeypatch.setattr(security.config, "auth_username", "reader")
    monkeypatch.setattr(security.config, "auth_password", SecretStr("pässword"))
    monkeypatch.setattr(security.config, "allowed_hosts", [])


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.1:6997", "192.168.1.5:6997", "[::1]:6997", "localhost:6997",
    "LOCALHOST", "comics.example.com", "COMICS.example.com:443",
])
async def test_allowed_hosts_reach_bootstrap(browser_app, no_login, host):
    async with client_for(browser_app) as client:
        resp = await client.get("/initialize.json", headers={"Host": host})
    assert resp.status_code == 200


@pytest.mark.parametrize("host", [
    "evil.example", "comics.example.com.evil.example", "localhost:notaport",
    "localhost:99999", "evil.example/localhost", "user@localhost", "local host",
])
async def test_unrecognized_hosts_are_rejected(browser_app, no_login, host):
    async with client_for(browser_app) as client:
        for path in ("/initialize.json", "/api/v1/thing"):
            resp = await client.get(path, headers={"Host": host})
            assert resp.status_code == 400
            assert "PULLARR_ALLOWED_HOSTS" in resp.json()["detail"]


async def test_login_guards_bootstrap_but_not_the_api_key_boundary(browser_app, login):
    async with client_for(browser_app) as client:
        denied = await client.get("/initialize.json")
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"].startswith("Basic")
        assert denied.headers["cache-control"] == "no-store"
        assert (await client.get("/initialize.json", headers=basic("reader", "wrong"))).status_code == 401
        assert (await client.get("/initialize.json", headers=basic("wrong", "pässword"))).status_code == 401
        assert (await client.get("/initialize.json", headers={"Authorization": "Basic !!"})).status_code == 401
        assert (await client.get("/initialize.json", headers=basic("reader", "pässword"))).status_code == 200
        # the mounted API authenticates itself with X-Api-Key
        assert (await client.get("/api/v1/thing")).status_code == 200


def test_real_app_applies_the_boundary():
    from pullarr.main import app

    assert any(m.kwargs.get("dispatch") is security.access_boundary for m in app.user_middleware)


@pytest.mark.parametrize("env", [
    {"auth_username": "reader"},
    {"auth_password": "secret"},
    {"auth_username": "a:b", "auth_password": "secret"},
    {"allowed_hosts": ["comics.example.com:443"]},
    {"allowed_hosts": ["*.example.com"]},
    {"allowed_hosts": ["http://comics.example.com"]},
])
def test_invalid_security_config_stops_startup(env):
    with pytest.raises(ValidationError):
        AppConfig(_env_file=None, **env)


def test_empty_environment_values_mean_unset(monkeypatch):
    monkeypatch.setenv("PULLARR_ALLOWED_HOSTS", "")
    monkeypatch.setenv("PULLARR_AUTH_USERNAME", "")
    built = AppConfig(_env_file=None)
    assert built.allowed_hosts == [] and built.auth_username == ""


# ------------------------------------------------- saved secret destinations

@pytest.fixture
async def session(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from pullarr.models import Base, Setting

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        s.add_all([Setting(key=k, value=v) for k, v in {
            "webhook_url": "https://panel.local/hook",
            "webhook_secret": "hook-secret",
            "kavita_url": "https://kavita.local",
            "kavita_api_key": "kavita-secret",
            "qbittorrent_url": "http://qbt.local",
            "qbittorrent_username": "admin",
            "qbittorrent_password": "qbt-secret",
        }.items()])
        await s.commit()
        yield s
    await engine.dispose()


async def test_webhook_test_keeps_secret_home(session, monkeypatch):
    sent = []

    async def fake_send(url, secret, payload):
        sent.append((url, secret))
        return True

    monkeypatch.setattr(notifications, "send_webhook", fake_send)
    with pytest.raises(HTTPException) as exc:
        await settings_api.webhook_test(WebhookTestIn(url="https://evil.example/", secret=settings_api.MASK), session)
    assert exc.value.status_code == 422 and not sent
    await settings_api.webhook_test(WebhookTestIn(url="https://panel.local/hook", secret=settings_api.MASK), session)
    await settings_api.webhook_test(WebhookTestIn(url="https://elsewhere.local/", secret="typed-again"), session)
    assert sent == [("https://panel.local/hook", "hook-secret"), ("https://elsewhere.local/", "typed-again")]


@pytest.mark.parametrize("body", [
    QbtTestIn(url="https://evil.example", username="admin", password="••••••••"),
    QbtTestIn(url="http://qbt.local", username="other", password="••••••••"),
])
async def test_qbittorrent_test_refuses_changed_destination(session, monkeypatch, body):
    async def fail(*args):
        raise AssertionError("secret left the app")

    monkeypatch.setattr(settings_api, "test_connection", fail)
    with pytest.raises(HTTPException) as exc:
        await settings_api.qbt_test(body, session)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("key", ["••••••••", ""])
async def test_kavita_test_refuses_changed_destination(session, monkeypatch, key):
    async def fail(*args):
        raise AssertionError("secret left the app")

    monkeypatch.setattr(kavita, "test_connection", fail)
    with pytest.raises(HTTPException) as exc:
        await settings_api.kavita_test(KavitaTestIn(url="https://evil.example", api_key=key), session)
    assert exc.value.status_code == 422


@pytest.mark.parametrize("body", [
    {"webhook_url": "https://evil.example/", "webhook_secret": MASK},
    {"webhook_url": "https://evil.example/"},
    {"kavita_url": "https://evil.example", "kavita_api_key": MASK},
    {"qbittorrent_url": "https://evil.example", "qbittorrent_password": MASK},
    {"qbittorrent_username": "other"},
])
async def test_save_refuses_moving_a_masked_secret(session, body):
    before = await get_all(session)
    with pytest.raises(HTTPException) as exc:
        await settings_api.update_settings(body, session)
    assert exc.value.status_code == 422
    assert await get_all(session) == before


async def test_save_allows_reentered_cleared_and_unmoved_secrets(session):
    await settings_api.update_settings(
        {"webhook_url": "https://new.local/", "webhook_secret": "new-secret"}, session
    )
    await settings_api.update_settings(
        {"qbittorrent_url": "http://other.local", "qbittorrent_password": ""}, session
    )
    await settings_api.update_settings(
        {"kavita_url": "https://kavita.local", "kavita_api_key": MASK}, session
    )
    values = await get_all(session)
    assert values["webhook_secret"] == "new-secret"
    assert values["qbittorrent_password"] == ""
    assert values["kavita_api_key"] == "kavita-secret"


def torznab(url: str, key: str) -> str:
    return json.dumps([{"id": "a", "name": "Prowlarr", "url": url, "api_key": key}])


def test_torznab_key_stays_with_its_url():
    saved = torznab("https://prowlarr.local/api", SECRET)
    kept = json.loads(restore_keys(torznab("https://prowlarr.local/api", MASK), saved))
    assert kept[0]["api_key"] == SECRET
    with pytest.raises(ValueError, match="Re-enter"):
        restore_keys(torznab("https://evil.example/api", MASK), saved)
    moved = json.loads(restore_keys(torznab("https://new.local/api", "typed-again"), saved))
    assert moved[0]["api_key"] == "typed-again"


# ------------------------------------------------- credentials in logs/errors

def test_app_silences_http_client_url_logging():
    import pullarr.main  # noqa: F401

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


@respx.mock
async def test_comicvine_errors_omit_api_key():
    provider = ComicVineProvider(client=httpx.AsyncClient())
    provider.configure(SECRET)
    respx.get(url__startswith="https://comicvine.gamespot.com/").mock(return_value=httpx.Response(401))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await provider.search("Batman")
    assert SECRET not in str(exc.value)
    assert exc.value.response.status_code == 401
    respx.get(url__startswith="https://comicvine.gamespot.com/").mock(return_value=httpx.Response(404))
    assert await provider.get_series("1") is None


@respx.mock
async def test_comicvine_transport_errors_become_safe_errors(monkeypatch):
    provider = ComicVineProvider(client=httpx.AsyncClient())
    provider.configure(SECRET)
    respx.get(url__startswith="https://comicvine.gamespot.com/").mock(
        side_effect=httpx.ConnectError(f"failed for api_key={SECRET}")
    )

    async def no_sleep(_):
        return None

    monkeypatch.setattr("pullarr.util.asyncio.sleep", no_sleep)
    with pytest.raises(ComicVineError) as exc:
        await provider.search("Batman")
    assert SECRET not in str(exc.value) and exc.value.__cause__ is None


@respx.mock
async def test_torznab_errors_omit_api_key():
    indexer = TorznabIndexer({"name": "P", "url": "https://prowlarr.local/api", "api_key": SECRET})
    respx.get(url__startswith="https://prowlarr.local/").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await indexer.search("Batman")
    assert SECRET not in str(exc.value) and exc.value.response.status_code == 500


@respx.mock
async def test_torrent_fetch_errors_omit_indexer_key():
    url = f"https://prowlarr.local/dl?apikey={SECRET}"
    respx.get(url).mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await fetch_torrent(url)
    assert SECRET not in str(exc.value)


@respx.mock
async def test_kavita_errors_omit_api_key_and_response_body():
    client = kavita.KavitaClient("https://kavita.local", SECRET)
    route = respx.post(url__startswith="https://kavita.local/api/Plugin/authenticate")
    route.mock(side_effect=httpx.ConnectError(f"apiKey={SECRET}"))
    with pytest.raises(kavita.KavitaError) as exc:
        await client._authenticate()
    assert SECRET not in str(exc.value) and exc.value.__cause__ is None
    route.mock(return_value=httpx.Response(500, text=f"echo {SECRET}"))
    with pytest.raises(kavita.KavitaError) as exc:
        await client._authenticate()
    assert SECRET not in str(exc.value)
    await client.close()


@respx.mock
async def test_qbittorrent_errors_omit_url_and_response_body():
    login = respx.post("http://qbt.local/api/v2/auth/login")
    login.mock(side_effect=httpx.ConnectError("http://user:secret@qbt.local"))
    with pytest.raises(QbtError) as exc:
        await qbt_connect("http://qbt.local", "admin", "pw")
    assert "secret" not in str(exc.value) and exc.value.__cause__ is None
    login.mock(return_value=httpx.Response(401, text="secret body"))
    client = QbtClient("http://qbt.local", "admin", "pw")
    with pytest.raises(QbtError) as exc:
        await client.version()
    assert "secret" not in str(exc.value)
    await client.close()


@respx.mock
async def test_webhook_logs_omit_url(caplog):
    url = f"https://hooks.local/{SECRET}"
    respx.post(url).mock(side_effect=httpx.ConnectError(url))
    with caplog.at_level(logging.WARNING):
        assert await notifications.send_webhook(url, "", {}) is False
    respx.post(url).mock(return_value=httpx.Response(500))
    with caplog.at_level(logging.WARNING):
        assert await notifications.send_webhook(url, "", {}) is False
    assert caplog.text and SECRET not in caplog.text


# ------------------------------------------------------ ComicInfo read limits

GOOD = "<ComicInfo><Series>Batman</Series><Number>7</Number></ComicInfo>"


def archive(path, info: bytes | str, method=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.jpg", b"\xff\xd8\xff")
        zf.writestr("ComicInfo.xml", info, compress_type=method)
    return path


def test_normal_comicinfo_is_read(tmp_path):
    path = archive(tmp_path / "a.cbz", GOOD)
    assert read_comicinfo(path) == GOOD
    assert _comicinfo_identity(path, False) == ("Batman", "7", None)


@pytest.mark.parametrize("info,method", [
    (b" " * (MAX_COMICINFO_BYTES + 1), zipfile.ZIP_DEFLATED),
    (GOOD, zipfile.ZIP_BZIP2),
    (GOOD, zipfile.ZIP_LZMA),
])
def test_unsafe_comicinfo_is_ignored(tmp_path, info, method):
    path = archive(tmp_path / "a.cbz", info, method)
    assert read_comicinfo(path) is None
    assert _comicinfo_identity(path, False) == ("", "", None)


def test_comicinfo_read_is_bounded_when_the_header_understates_size(tmp_path):
    path = archive(tmp_path / "a.cbz", b" " * (MAX_COMICINFO_BYTES + 1))
    raw = path.read_bytes()
    size = (MAX_COMICINFO_BYTES + 1).to_bytes(4, "little")
    # rewrite the declared uncompressed size in the central directory
    central = raw.rindex(b"PK\x01\x02")
    offset = raw.index(size, central)
    path.write_bytes(raw[:offset] + (10).to_bytes(4, "little") + raw[offset + 4:])
    assert read_comicinfo(path) is None


def test_import_replaces_unsafe_comicinfo(tmp_path):
    path = archive(tmp_path / "a.cbz", GOOD, zipfile.ZIP_BZIP2)
    assert upsert_comicinfo(path, "<ComicInfo><Series>New</Series></ComicInfo>")
    with zipfile.ZipFile(path) as zf:
        assert [n for n in zf.namelist() if n.lower() == "comicinfo.xml"] == ["ComicInfo.xml"]
        assert zf.getinfo("ComicInfo.xml").compress_type == zipfile.ZIP_DEFLATED
    assert "New" in read_comicinfo(path)


async def test_metron_saved_password_cannot_move_to_another_account(session, monkeypatch):
    from pullarr.schemas import MetronTestIn
    from pullarr.settings_service import set_many

    await set_many(session, {"metron_username": "reader", "metron_password": "saved-password"})
    def no_configure(*args):
        raise AssertionError("Saved password left the app")
    monkeypatch.setattr(settings_api.metron, "configure", no_configure)
    with pytest.raises(HTTPException) as exc:
        await settings_api.metron_test(MetronTestIn(username="different", password=MASK), session)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await settings_api.update_settings({"metron_username": "different", "metron_password": MASK}, session)
    assert exc.value.status_code == 422
    assert (await get_all(session))["metron_username"] == "reader"


async def test_metron_unchanged_account_reuses_saved_password(session, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from pullarr.schemas import MetronTestIn
    from pullarr.settings_service import set_many

    await set_many(session, {"metron_username": "reader", "metron_password": "saved-password"})
    configure = Mock()
    monkeypatch.setattr(settings_api.metron, "configure", configure)
    monkeypatch.setattr(settings_api.metron, "test", AsyncMock(return_value={"ok": True}))
    await settings_api.metron_test(MetronTestIn(username="reader", password=MASK), session)
    configure.assert_called_once_with("reader", "saved-password")
    await settings_api.update_settings({"metron_username": "reader", "metron_password": MASK}, session)
    assert (await get_all(session))["metron_password"] == "saved-password"
    await settings_api.update_settings({"metron_username": "different", "metron_password": "new-password"}, session)
    assert (await get_all(session))["metron_username"] == "different"
