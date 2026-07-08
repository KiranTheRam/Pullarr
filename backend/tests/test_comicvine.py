from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from pullarr.metadata.base import IssueMetadata
from pullarr.metadata.comicvine import (
    API_URL,
    ComicVineError,
    ComicVineProvider,
    _clean_html,
    derive_status,
    parse_issue_number,
)


class TestParseIssueNumber:
    def test_plain(self):
        assert parse_issue_number("15") == 15.0

    def test_fractional(self):
        assert parse_issue_number("10.5") == 10.5

    def test_suffixed(self):
        assert parse_issue_number("1.MU") == 1.0

    def test_half(self):
        assert parse_issue_number("½") == 0.5

    def test_non_numeric(self):
        assert parse_issue_number("Special") is None
        assert parse_issue_number("") is None
        assert parse_issue_number(None) is None


class TestDeriveStatus:
    def _issue(self, days_ago: int) -> IssueMetadata:
        return IssueMetadata(
            provider_id="1", number=1.0,
            released_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )

    def test_recent_release_is_releasing(self):
        assert derive_status([self._issue(30)], 10) == "releasing"

    def test_old_release_is_finished(self):
        assert derive_status([self._issue(800)], 10) == "finished"

    def test_no_dates_unknown(self):
        assert derive_status([IssueMetadata(provider_id="1", number=1.0)], 10) == "unknown"


def _provider() -> ComicVineProvider:
    p = ComicVineProvider(client=httpx.AsyncClient())
    p.configure("test-key")
    return p


@pytest.mark.asyncio
async def test_requires_api_key():
    p = ComicVineProvider(client=httpx.AsyncClient())
    with pytest.raises(ComicVineError, match="API key"):
        await p.search("batman")


@pytest.mark.asyncio
@respx.mock
async def test_search_parses_volumes():
    respx.get(f"{API_URL}/search/").respond(json={
        "status_code": 1,
        "number_of_total_results": 1,
        "results": [{
            "id": 796,
            "name": "Batman",
            "start_year": "2011",
            "count_of_issues": 57,
            "description": "<p>The Dark Knight.</p>",
            "aliases": "The Bat\nCaped Crusader",
            "publisher": {"name": "DC Comics"},
            "image": {"medium_url": "https://img/batman.jpg"},
        }],
    })
    results = await _provider().search("batman")
    assert len(results) == 1
    r = results[0]
    assert r.provider_id == "796"
    assert r.title == "Batman"
    assert r.year == 2011
    assert r.publisher == "DC Comics"
    assert r.total_issues == 57
    assert r.description == "The Dark Knight."
    assert r.alt_titles == ["The Bat", "Caped Crusader"]
    assert r.cover_url == "https://img/batman.jpg"


@pytest.mark.asyncio
@respx.mock
async def test_list_issues_paginates_and_sorts():
    route = respx.get(f"{API_URL}/issues/")
    route.side_effect = [
        httpx.Response(200, json={
            "status_code": 1,
            "number_of_total_results": 3,
            "results": [
                {"id": 2, "issue_number": "2", "name": "Two",
                 "cover_date": "2024-11-01", "store_date": "2024-10-25"},
                {"id": 1, "issue_number": "1", "name": "One",
                 "cover_date": "2024-10-01", "store_date": None},
            ],
        }),
        httpx.Response(200, json={
            "status_code": 1,
            "number_of_total_results": 3,
            "results": [
                {"id": 3, "issue_number": "Special", "name": "skipped",
                 "cover_date": None, "store_date": None},
            ],
        }),
    ]
    issues = await _provider().list_issues("796")
    # "Special" is retained with a synthetic sort key and its display label.
    assert [i.number for i in issues] == [1.0, 2.0, 3.0]
    assert [i.display_number for i in issues] == ["1", "2", "Special"]
    assert issues[0].title == "One"
    # store_date preferred over cover_date
    assert issues[1].released_at.day == 25


@pytest.mark.asyncio
@respx.mock
async def test_api_error_raises():
    respx.get(f"{API_URL}/search/").respond(json={
        "status_code": 100, "error": "Invalid API Key", "results": [],
    })
    with pytest.raises(ComicVineError, match="Invalid API Key"):
        await _provider().search("batman")


@pytest.mark.asyncio
@respx.mock
async def test_list_issues_keeps_suffixed_issue_distinct():
    respx.get(f"{API_URL}/issues/").respond(json={
        "status_code": 1,
        "number_of_total_results": 2,
        "results": [
            {"id": 1, "issue_number": "1", "name": "One"},
            {"id": 2, "issue_number": "1.MU", "name": "Tie-in"},
        ],
    })

    issues = await _provider().list_issues("796")

    assert [i.display_number for i in issues] == ["1", "1.MU"]
    assert issues[0].number == 1.0
    assert issues[1].number > 1.0


def test_clean_html_strips_escaped_tags():
    assert _clean_html("&lt;img src=x onerror=alert(1)&gt;Safe") == "Safe"
