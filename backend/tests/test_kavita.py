import asyncio
import json

import pytest
import respx
from httpx import Response

from pullarr import kavita
from pullarr.kavita import KavitaError, KavitaLibrary, ScanRequest

BASE = "http://kavita.test"

AUTH_BODY = {"token": "jwt-token", "kavitaVersion": "0.9.0.2", "username": "u"}

LIBRARIES_BODY = [
    {"id": 5, "name": "DC Comics", "folders": ["/data/comics/DC"]},
    {"id": 4, "name": "Marvel Comics", "folders": ["/data/comics/Marvel"]},
]

ENABLED = {
    "kavita_enabled": "true",
    "kavita_url": BASE,
    "kavita_api_key": "key",
    "kavita_scan_mode": "series",
    "kavita_library_map": "",
}


@pytest.fixture(autouse=True)
def _reset_pending():
    kavita._pending.clear()
    kavita._flush_task = None
    yield
    kavita._pending.clear()
    kavita._flush_task = None


def mock_auth():
    return respx.post(f"{BASE}/api/Plugin/authenticate").mock(
        return_value=Response(200, json=AUTH_BODY)
    )


def mock_libraries():
    return respx.get(f"{BASE}/api/Library/libraries").mock(
        return_value=Response(200, json=LIBRARIES_BODY)
    )


# ------------------------------------------------------------------- client

@respx.mock
async def test_authenticate_exchanges_api_key_for_bearer_token():
    auth = mock_auth()
    libs = mock_libraries()
    version, libraries = await kavita.test_connection(BASE, "sekrit")
    assert version == "0.9.0.2"
    assert [lib.name for lib in libraries] == ["DC Comics", "Marvel Comics"]
    assert auth.calls[0].request.url.params["apiKey"] == "sekrit"
    assert auth.calls[0].request.url.params["pluginName"] == "pullarr"
    assert libs.calls[0].request.headers["authorization"] == "Bearer jwt-token"


@respx.mock
async def test_bad_api_key_raises():
    respx.post(f"{BASE}/api/Plugin/authenticate").mock(return_value=Response(401))
    with pytest.raises(KavitaError, match="rejected the API key"):
        await kavita.test_connection(BASE, "nope")


@respx.mock
async def test_non_kavita_host_raises_readable_error():
    respx.post(f"{BASE}/api/Plugin/authenticate").mock(
        return_value=Response(200, text="<html>hello</html>")
    )
    with pytest.raises(KavitaError, match="did not return a Kavita API response"):
        await kavita.test_connection(BASE, "key")


@respx.mock
async def test_scan_endpoints_send_kavita_payloads():
    mock_auth()
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    series_scan = respx.post(f"{BASE}/api/Series/scan").mock(return_value=Response(200))
    client = kavita.KavitaClient(BASE, "key")
    try:
        await client.scan_library(5)
        await client.scan_series(5, 763)
    finally:
        await client.close()
    assert lib_scan.calls[0].request.url.params["libraryId"] == "5"
    assert json.loads(series_scan.calls[0].request.content) == {
        "libraryId": 5, "seriesId": 763, "forceUpdate": False,
    }


@respx.mock
async def test_find_series_id_matches_loosely_but_rejects_other_titles():
    mock_auth()
    respx.get(f"{BASE}/api/Search/search").mock(return_value=Response(200, json={
        "series": [
            {"seriesId": 9, "name": "Batman Earth One", "libraryId": 5},
            {"seriesId": 7, "name": "The Batman", "libraryId": 5},
        ]
    }))
    client = kavita.KavitaClient(BASE, "key")
    try:
        # normalize_title drops a leading "The", so these are the same series
        assert await client.find_series_id(5, ["Batman"]) == 7
        assert await client.find_series_id(5, ["Superman"]) is None
    finally:
        await client.close()


@respx.mock
async def test_find_series_id_ignores_other_libraries():
    mock_auth()
    respx.get(f"{BASE}/api/Search/search").mock(return_value=Response(200, json={
        "series": [{"seriesId": 9, "name": "Daredevil", "libraryId": 4}]
    }))
    client = kavita.KavitaClient(BASE, "key")
    try:
        assert await client.find_series_id(5, ["Daredevil"]) is None
    finally:
        await client.close()


@respx.mock
async def test_a_title_shared_by_two_reboots_is_treated_as_no_match():
    """Comic titles repeat across reboots. Scanning the wrong Batman would
    leave the new issues undiscovered; giving up here means a library scan."""
    mock_auth()
    respx.get(f"{BASE}/api/Search/search").mock(return_value=Response(200, json={
        "series": [
            {"seriesId": 11, "name": "Batman", "libraryId": 5},
            {"seriesId": 12, "name": "Batman", "libraryId": 5},
        ]
    }))
    client = kavita.KavitaClient(BASE, "key")
    try:
        assert await client.find_series_id(5, ["Batman"]) is None
    finally:
        await client.close()


@respx.mock
async def test_a_year_qualified_title_resolves_what_the_bare_title_cannot():
    mock_auth()

    def search(request):
        query = request.url.params["queryString"]
        if query == "Batman (2016)":
            return Response(200, json={
                "series": [{"seriesId": 12, "name": "Batman (2016)", "libraryId": 5}]
            })
        return Response(200, json={"series": [
            {"seriesId": 11, "name": "Batman", "libraryId": 5},
            {"seriesId": 12, "name": "Batman", "libraryId": 5},
        ]})

    respx.get(f"{BASE}/api/Search/search").mock(side_effect=search)
    client = kavita.KavitaClient(BASE, "key")
    try:
        # the most specific candidate is tried before the ambiguous bare title
        assert await client.find_series_id(5, ["Batman (2016)", "Batman"]) == 12
    finally:
        await client.close()


@respx.mock
async def test_expired_token_is_refreshed_once():
    auth = mock_auth()
    route = respx.get(f"{BASE}/api/Library/libraries").mock(
        side_effect=[Response(401), Response(200, json=LIBRARIES_BODY)]
    )
    client = kavita.KavitaClient(BASE, "key")
    try:
        assert len(await client.libraries()) == 2
    finally:
        await client.close()
    assert len(auth.calls) == 2
    assert route.call_count == 2


# ----------------------------------------------------------- library mapping

def test_match_library_matches_on_shared_path_tail():
    libs = [
        KavitaLibrary(5, "DC", ["/data/comics/DC"]),
        KavitaLibrary(4, "Marvel", ["/data/comics/Marvel"]),
    ]
    # identical mount
    assert kavita.match_library(libs, "/data/comics/DC").id == 5
    # same share, different mount point in each container
    assert kavita.match_library(libs, "/mnt/media/comics/Marvel").id == 4
    assert kavita.match_library(libs, "/comics/DC").id == 5


def test_match_library_rejects_a_differently_named_folder():
    libs = [KavitaLibrary(5, "DC", ["/data/comics/DC"])]
    # "comics" is shared, but the library folders themselves are unrelated
    assert kavita.match_library(libs, "/data/comics/Image") is None
    assert kavita.match_library(libs, "") is None


def test_match_library_prefers_the_longest_tail():
    libs = [
        KavitaLibrary(1, "Broad", ["/media/DC"]),
        KavitaLibrary(2, "Exact", ["/tank/comics/DC"]),
    ]
    assert kavita.match_library(libs, "/tank/comics/DC").id == 2


def test_parse_library_map_survives_garbage():
    assert kavita.parse_library_map('{"1": 5, "2": 4}') == {1: 5, 2: 4}
    assert kavita.parse_library_map("") == {}
    assert kavita.parse_library_map("not json") == {}
    assert kavita.parse_library_map("[1,2]") == {}
    assert kavita.parse_library_map('{"1": "x", "y": 2, "3": 4}') == {3: 4}


@respx.mock
async def test_explicit_mapping_wins_over_path_match():
    mock_auth()
    mock_libraries()
    client = kavita.KavitaClient(BASE, "key")
    values = {**ENABLED, "kavita_library_map": '{"1": 4}'}
    try:
        # path says DC (5), the explicit map says Marvel (4)
        assert await kavita.resolve_library_id(client, values, 1, "/data/comics/DC") == 4
        # a root folder with no override still falls back to the path match
        assert await kavita.resolve_library_id(client, values, 2, "/data/comics/DC") == 5
        # nothing matches: refuse rather than scan an unrelated library
        assert await kavita.resolve_library_id(client, values, 2, "/elsewhere") is None
    finally:
        await client.close()


# ------------------------------------------------------------------ scanning

@respx.mock
async def test_known_series_gets_a_partial_scan():
    mock_auth()
    mock_libraries()
    respx.get(f"{BASE}/api/Search/search").mock(return_value=Response(200, json={
        "series": [{"seriesId": 763, "name": "Batman Earth One", "libraryId": 5}]
    }))
    series_scan = respx.post(f"{BASE}/api/Series/scan").mock(return_value=Response(200))
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    await kavita.run_scans(
        ENABLED, {ScanRequest(1, "/data/comics/DC", ("Batman Earth One",))}
    )
    assert series_scan.called
    assert not lib_scan.called


@respx.mock
async def test_series_kavita_has_never_seen_falls_back_to_a_library_scan():
    mock_auth()
    mock_libraries()
    respx.get(f"{BASE}/api/Search/search").mock(
        return_value=Response(200, json={"series": []})
    )
    series_scan = respx.post(f"{BASE}/api/Series/scan").mock(return_value=Response(200))
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    await kavita.run_scans(
        ENABLED, {ScanRequest(1, "/data/comics/DC", ("Brand New Series (2026)",))}
    )
    assert not series_scan.called
    assert lib_scan.calls[0].request.url.params["libraryId"] == "5"


@respx.mock
async def test_library_mode_never_searches_or_scans_per_series():
    mock_auth()
    mock_libraries()
    search = respx.get(f"{BASE}/api/Search/search").mock(return_value=Response(200, json={}))
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    await kavita.run_scans(
        {**ENABLED, "kavita_scan_mode": "library"},
        {ScanRequest(1, "/data/comics/DC", ("Batman Earth One",))},
    )
    assert not search.called
    assert lib_scan.called


@respx.mock
async def test_a_library_scan_subsumes_series_scans_in_that_library():
    mock_auth()
    mock_libraries()

    # "Known" resolves; "New" does not, forcing a scan of the whole library
    def search(request):
        query = request.url.params["queryString"]
        return Response(200, json={
            "series": [{"seriesId": 763, "name": "Known", "libraryId": 5}]
            if query == "Known" else []
        })

    respx.get(f"{BASE}/api/Search/search").mock(side_effect=search)
    series_scan = respx.post(f"{BASE}/api/Series/scan").mock(return_value=Response(200))
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    await kavita.run_scans(ENABLED, {
        ScanRequest(1, "/data/comics/DC", ("Known",)),
        ScanRequest(1, "/data/comics/DC", ("New",)),
    })
    assert lib_scan.call_count == 1
    assert not series_scan.called


@respx.mock
async def test_unmappable_root_folder_scans_nothing():
    mock_auth()
    mock_libraries()
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    await kavita.run_scans(ENABLED, {ScanRequest(9, "/nowhere/at/all", ("X",))})
    assert not lib_scan.called


# ------------------------------------------------------------------- notify

@respx.mock
async def test_notify_import_debounces_a_burst_into_one_scan(monkeypatch):
    monkeypatch.setattr(kavita, "DEBOUNCE_SECONDS", 0.05)
    mock_auth()
    mock_libraries()
    respx.get(f"{BASE}/api/Search/search").mock(
        return_value=Response(200, json={"series": []})
    )
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))
    for _ in range(20):
        kavita.notify_import(ENABLED, 1, "/data/comics/DC", ["Batman (2016)"])
    await asyncio.sleep(0.3)
    assert lib_scan.call_count == 1


async def test_notify_import_disabled_schedules_nothing():
    # would raise on an unmocked request if it tried to reach the network
    kavita.notify_import({**ENABLED, "kavita_enabled": "false"}, 1, "/data/comics/DC", ["X"])
    kavita.notify_import({**ENABLED, "kavita_url": ""}, 1, "/data/comics/DC", ["X"])
    kavita.notify_import({**ENABLED, "kavita_api_key": ""}, 1, "/data/comics/DC", ["X"])
    kavita.notify_import(ENABLED, 1, "", ["X"])
    await asyncio.sleep(0.01)
    assert not kavita._pending


@respx.mock
async def test_unreachable_kavita_never_raises_into_the_import_path(monkeypatch, caplog):
    monkeypatch.setattr(kavita, "DEBOUNCE_SECONDS", 0.05)
    respx.post(f"{BASE}/api/Plugin/authenticate").mock(return_value=Response(500))
    kavita.notify_import(ENABLED, 1, "/data/comics/DC", ["Batman (2016)"])
    await asyncio.sleep(0.3)
    assert "Kavita scan request failed" in caplog.text


@respx.mock
async def test_an_import_arriving_during_a_scan_is_not_dropped(monkeypatch):
    monkeypatch.setattr(kavita, "DEBOUNCE_SECONDS", 0.05)
    mock_auth()
    mock_libraries()
    respx.get(f"{BASE}/api/Search/search").mock(
        return_value=Response(200, json={"series": []})
    )
    scanning = asyncio.Event()
    release = asyncio.Event()

    async def slow_scan(request):
        scanning.set()
        await release.wait()
        return Response(200)

    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(side_effect=slow_scan)

    kavita.notify_import(ENABLED, 1, "/data/comics/DC", ["First"])
    await asyncio.wait_for(scanning.wait(), 1)
    # a second import lands mid-scan, so the timer that would have batched it
    # has already fired and drained the pending set
    kavita.notify_import(ENABLED, 2, "/data/comics/Marvel", ["Second"])
    release.set()
    await asyncio.sleep(0.4)
    assert {call.request.url.params["libraryId"] for call in lib_scan.calls} == {"5", "4"}


# ------------------------------------------------ wiring into the import path

@respx.mock
async def test_import_path_notifies_kavita_year_qualified_titles_first(monkeypatch):
    """tasks._notify_kavita is what downloads actually call: it must hand over
    the series' root folder and try the year-qualified name before the bare
    title that every reboot shares."""
    from pullarr.jobs import tasks
    from pullarr.models import RootFolder, Series

    monkeypatch.setattr(kavita, "DEBOUNCE_SECONDS", 0.05)
    mock_auth()
    mock_libraries()
    search = respx.get(f"{BASE}/api/Search/search").mock(
        return_value=Response(200, json={"series": []})
    )
    lib_scan = respx.post(f"{BASE}/api/Library/scan").mock(return_value=Response(200))

    series = Series(
        id=1, title="Batman", year=2016, folder_name="Batman (2016)",
        alt_titles="Batman Rebirth", root_folder_id=1,
    )
    series.root_folder = RootFolder(id=1, path="/comics/DC")
    tasks._notify_kavita(ENABLED, series)
    await asyncio.sleep(0.3)

    queried = [call.request.url.params["queryString"] for call in search.calls]
    assert queried[0] == "Batman (2016)"  # most specific first
    assert "Batman" in queried
    assert "Batman Rebirth" in queried
    # folder_name and series_folder(title, year) coincide here — sent once
    assert len(queried) == len(set(queried))
    # /comics/DC matched Kavita's /data/comics/DC on the folder name
    assert lib_scan.calls[0].request.url.params["libraryId"] == "5"


async def test_series_without_a_root_folder_notifies_nothing():
    from pullarr.jobs import tasks
    from pullarr.models import Series

    # would hit the unmocked network if it scheduled anything
    tasks._notify_kavita(ENABLED, Series(id=1, title="X", folder_name="X", alt_titles=""))
    await asyncio.sleep(0.01)
    assert not kavita._pending


# ---------------------------------------------------------------- validation

def test_validate_settings_requires_a_usable_connection_when_enabled():
    kavita.validate_settings({"kavita_enabled": "false", "kavita_url": ""})
    kavita.validate_settings(ENABLED)
    with pytest.raises(ValueError, match="Kavita URL is required"):
        kavita.validate_settings({**ENABLED, "kavita_url": ""})
    with pytest.raises(ValueError, match="Kavita API key is required"):
        kavita.validate_settings({**ENABLED, "kavita_api_key": ""})
    with pytest.raises(ValueError, match="valid http"):
        kavita.validate_settings({**ENABLED, "kavita_url": "kavita:5000"})
    with pytest.raises(ValueError, match="scan scope"):
        kavita.validate_settings({**ENABLED, "kavita_scan_mode": "everything"})
    with pytest.raises(ValueError, match="valid JSON"):
        kavita.validate_settings({**ENABLED, "kavita_library_map": "{oops"})
    with pytest.raises(ValueError, match="JSON object"):
        kavita.validate_settings({**ENABLED, "kavita_library_map": "[1]"})
