import httpx
import pytest
import respx
from httpx import Response

from pullarr.api.discover import group_volumes
from pullarr.metadata.comicvine import ComicVineProvider


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
