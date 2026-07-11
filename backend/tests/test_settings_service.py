import pytest

from pullarr.settings_service import parse_monitor_interval, validate_updates


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
