import httpx
import pytest
import respx
from httpx import Response

from pullarr.api.discover import drop_manga_volumes, group_volumes
from pullarr.metadata.comicvine import ComicVineProvider
from pullarr.metadata.publishers import is_manga_publisher


def issue(issue_id, volume_id, volume_name, number="1", store_date="2026-07-15"):
    return {
        "id": issue_id,
        "name": f"Issue {number}",
        "issue_number": number,
        "store_date": store_date,
        "image": {"medium_url": f"http://img/{issue_id}.jpg"},
        "volume": {"id": volume_id, "name": volume_name},
    }


@respx.mock
@pytest.mark.asyncio
async def test_issues_in_stores_builds_store_date_filter():
    route = respx.get("https://comicvine.gamespot.com/api/issues/").mock(
        return_value=Response(200, json={"status_code": 1, "results": [issue(1, 10, "Batman")]})
    )
    provider = ComicVineProvider(client=httpx.AsyncClient())
    provider.configure("cv-key")
    results = await provider.issues_in_stores("2026-07-11", "2026-07-18", issue_number="1")
    assert len(results) == 1

    params = dict(route.calls[0].request.url.params)
    assert params["filter"] == "store_date:2026-07-11|2026-07-18,issue_number:1"
    assert params["sort"] == "store_date:desc"
    assert params["api_key"] == "cv-key"


def test_group_volumes_dedupes_and_labels():
    raw = [
        issue(1, 10, "Batman (2026)", number="3", store_date="2026-07-15"),
        issue(2, 10, "Batman (2026)", number="2", store_date="2026-07-08"),
        issue(3, 20, "Saga", number="72", store_date="2026-07-15"),
        {"id": 4, "volume": None},  # malformed rows are skipped
    ]
    items = group_volumes(raw)
    assert [i["comicvine_volume_id"] for i in items] == [10, 20]
    batman = items[0]
    assert batman["volume_name"] == "Batman (2026)"
    assert batman["issue_number"] == "3"
    assert batman["subtitle"] == "#3 · Jul 15"
    assert batman["cover_url"] == "http://img/1.jpg"


def test_group_volumes_respects_limit():
    raw = [issue(i, i, f"Vol {i}") for i in range(50)]
    assert len(group_volumes(raw, limit=5)) == 5


def test_group_volumes_without_limit_keeps_everything():
    raw = [issue(i, i, f"Vol {i}") for i in range(1, 51)]
    assert len(group_volumes(raw, limit=None)) == 50


@pytest.mark.parametrize("name, expected", [
    ("Kodansha", True),
    ("Kodansha Comics", True),          # imprints match as substrings
    ("Shogakukan", True),
    ("Shueisha", True),
    ("Akita Shoten", True),
    ("Nihon Bungeisha", True),
    ("VIZ Media", True),                # western house, manga catalogue
    ("Seven Seas Entertainment", True),
    ("Yen Press", True),
    ("Marvel", False),
    ("DC Comics", False),
    ("Image", False),
    ("Dark Horse Comics", False),
    ("Mad Cave Studios", False),
    ("DSTLRY", False),
    ("", False),                        # unknown publisher is never manga
])
def test_is_manga_publisher(name, expected):
    assert is_manga_publisher(name) is expected


def cv_volume(volume_id, name, publisher):
    return {
        "id": volume_id, "name": name, "start_year": "2026",
        "publisher": {"name": publisher}, "image": {}, "description": "",
        "count_of_issues": 5, "aliases": "",
    }


@respx.mock
@pytest.mark.asyncio
async def test_volumes_by_ids_batches_into_one_request():
    route = respx.get("https://comicvine.gamespot.com/api/volumes/").mock(
        return_value=Response(200, json={"status_code": 1, "results": [
            cv_volume(10, "Batman", "DC Comics"),
            cv_volume(20, "Takane no Hana-san", "Nihon Bungeisha"),
        ]})
    )
    provider = ComicVineProvider(client=httpx.AsyncClient())
    provider.configure("cv-key")
    volumes = await provider.volumes_by_ids([10, 20, 10])  # duplicate collapses

    assert route.call_count == 1
    assert dict(route.calls[0].request.url.params)["filter"] == "id:10|20"
    assert volumes[10].publisher == "DC Comics"
    assert volumes[20].publisher == "Nihon Bungeisha"


@respx.mock
@pytest.mark.asyncio
async def test_drop_manga_volumes_filters_and_annotates(monkeypatch):
    from pullarr.metadata import comicvine as cv_module

    respx.get("https://comicvine.gamespot.com/api/volumes/").mock(
        return_value=Response(200, json={"status_code": 1, "results": [
            cv_volume(10, "Batman", "DC Comics"),
            cv_volume(20, "Mokushiroku no Yonkishi", "Kodansha"),
            cv_volume(30, "Cocoon", "Viz"),
        ]})
    )
    cv_module.provider.configure("cv-key")
    items = group_volumes([
        issue(1, 10, "Batman"), issue(2, 20, "Mokushiroku no Yonkishi"),
        issue(3, 30, "Cocoon"), issue(4, 40, "Unknown Volume"),
    ], limit=None)

    kept = await drop_manga_volumes(items)
    assert [i["comicvine_volume_id"] for i in kept] == [10, 40]  # unknown fails open
    assert kept[0]["publisher"] == "DC Comics"
    assert kept[1]["publisher"] == ""


@respx.mock
@pytest.mark.asyncio
async def test_drop_manga_volumes_serves_unfiltered_when_lookup_fails():
    from pullarr.metadata import comicvine as cv_module

    respx.get("https://comicvine.gamespot.com/api/volumes/").mock(
        return_value=Response(500)
    )
    cv_module.provider.configure("cv-key")
    items = group_volumes([issue(1, 10, "Batman"), issue(2, 20, "Manga")], limit=None)
    kept = await drop_manga_volumes(items)
    assert [i["comicvine_volume_id"] for i in kept] == [10, 20]
