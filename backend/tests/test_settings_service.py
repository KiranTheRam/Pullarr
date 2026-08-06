import pytest

from pullarr.settings_service import (
    DEFAULTS,
    SECRET_KEYS,
    parse_monitor_interval,
    validate_updates,
)


def test_parse_monitor_interval_accepts_positive_integer():
    assert parse_monitor_interval("15") == 15


@pytest.mark.parametrize("value", ["0", "-1", "abc", "10081"])
def test_parse_monitor_interval_rejects_bad_values(value):
    with pytest.raises(ValueError):
        parse_monitor_interval(value)


def test_validate_updates_rejects_bad_naming_template():
    with pytest.raises(ValueError, match="Invalid naming template"):
        validate_updates({"naming_template": "{missing}"})


def test_validate_updates_ignores_unknown_keys():
    assert validate_updates({"not_real": "x"}) == {}


@pytest.mark.parametrize("value", ["-1", "11", "many"])
def test_validate_updates_rejects_bad_retry_count(value):
    with pytest.raises(ValueError):
        validate_updates({"download_retry_attempts": value})


def test_validate_updates_normalizes_service_preference():
    assert validate_updates({"getcomics_service_preference": "Pixeldrain, main, main"}) == {
        "getcomics_service_preference": "pixeldrain,main"
    }


class TestKavita:
    def test_defaults_are_valid(self):
        from pullarr.kavita import validate_settings
        validate_settings(dict(DEFAULTS))

    def test_api_key_is_masked_like_other_secrets(self):
        assert "kavita_api_key" in SECRET_KEYS

    @pytest.mark.asyncio
    async def test_set_many_checks_kavita_across_settings(self, tmp_path):
        """Enabling Kavita needs a URL and key, which the page may not resend —
        set_many has to validate the resulting state, not just the payload."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from pullarr.models import Base
        from pullarr.settings_service import get_all, set_many

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as session:
                # turning it on alone is rejected: nothing to connect to yet
                with pytest.raises(ValueError, match="Kavita URL is required"):
                    await set_many(session, {"kavita_enabled": "true"})
                await set_many(session, {
                    "kavita_url": "http://kavita:5000", "kavita_api_key": "k",
                })
                # now the same toggle succeeds against the stored connection
                await set_many(session, {"kavita_enabled": "true"})
                assert (await get_all(session))["kavita_enabled"] == "true"
                with pytest.raises(ValueError, match="scan scope"):
                    await set_many(session, {"kavita_scan_mode": "everything"})
        finally:
            await engine.dispose()
