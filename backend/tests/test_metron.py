from datetime import datetime

import httpx
import pytest
import respx

from pullarr.metadata.metron import MetronError, MetronProvider
from pullarr.models import Issue, Series, SeriesStatus


@pytest.mark.asyncio
@respx.mock
async def test_metron_crosswalk_and_enrichment():
    client = httpx.AsyncClient()
    provider = MetronProvider(client)
    provider.configure("reader", "secret")

    series_list = respx.get("https://metron.cloud/api/series/", params={"cv_id": 123}).mock(
        return_value=httpx.Response(200, json={"count": 1, "results": [{"id": 77}]})
    )
    respx.get("https://metron.cloud/api/series/77/").mock(return_value=httpx.Response(200, json={
        "id": 77, "status": "Ongoing", "desc": "Better series summary",
        "publisher": {"name": "DC Comics"}, "genres": [{"name": "Superhero"}],
    }))
    issue_list = respx.get("https://metron.cloud/api/issue/", params={"cv_id": 456}).mock(
        return_value=httpx.Response(200, json={"count": 1, "results": [{"id": 88}]})
    )
    respx.get("https://metron.cloud/api/issue/88/").mock(return_value=httpx.Response(200, json={
        "id": 88,
        "desc": "Issue summary",
        "page": 32,
        "store_date": "2026-07-01",
        "resource_url": "https://metron.cloud/issue/test/",
        "imprint": {"name": "Black Label"},
        "series": {"genres": [{"name": "Mystery"}], "series_type": {"name": "Single Issue"}},
        "arcs": [{"name": "The Long Night"}],
        "characters": [{"name": "Batman"}],
        "teams": [{"name": "Outsiders"}],
        "reprints": [{"id": 5, "issue": "Batman #1"}],
        "credits": [
            {"creator": "Writer One", "role": [{"name": "Writer"}]},
            {"creator": "Artist One", "role": [{"name": "Penciller"}, {"name": "Cover"}]},
        ],
    }))

    series = Series(comicvine_id=123, title="Batman", sort_title="batman")
    issue = Issue(comicvine_id=456, number=1, display_number="1")
    assert await provider.enrich_series(series)
    assert series.metron_id == 77
    assert series.status == SeriesStatus.RELEASING
    assert series.description == "Better series summary"

    assert await provider.enrich_issue(issue)
    assert issue.metron_id == 88
    assert issue.summary == "Issue summary"
    assert issue.page_count == 32
    assert issue.writers == "Writer One"
    assert issue.pencillers == "Artist One"
    assert issue.cover_artists == "Artist One"
    assert issue.story_arcs == "The Long Night"
    assert issue.reprints == "5|Batman #1"
    assert issue.released_at == datetime(2026, 7, 1, tzinfo=issue.released_at.tzinfo)
    assert series_list.called and issue_list.called
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_metron_auth_error_is_clear():
    client = httpx.AsyncClient()
    provider = MetronProvider(client)
    provider.configure("bad", "credentials")
    respx.get("https://metron.cloud/api/series/").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid credentials"})
    )
    with pytest.raises(MetronError, match="invalid"):
        await provider.test()
    await client.aclose()
