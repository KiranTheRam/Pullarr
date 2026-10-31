from pathlib import Path

import httpx
import pytest
import respx

from pullarr.sources.getcomics import (
    GetComicsSource,
    main_server_links,
    mirror_link,
    parse_download_buttons,
    parse_search_page,
    pixeldrain_api_url,
)
from pullarr.sources.base import SourceRelease

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


class TestParseSearchPage:
    """Against a real getcomics.org search results page (?s=absolute+batman)."""

    def test_finds_posts(self):
        releases = parse_search_page(load("getcomics_search.html"), "https://getcomics.org")
        assert len(releases) >= 10
        by_title = {r.title: r for r in releases}
        r = by_title["Absolute Batman #21 (2026)"]
        assert r.url == "https://getcomics.org/dc/absolute-batman-21-2026/"
        assert r.external_id == r.url
        assert r.issue_number == 21.0
        assert r.year == 2026
        assert r.size_text == "125 MB"
        assert r.posted_at is not None and r.posted_at.year == 2026

    def test_issue_numbers_parsed_across_results(self):
        releases = parse_search_page(load("getcomics_search.html"), "https://getcomics.org")
        numbered = [r for r in releases if r.issue_number is not None]
        assert len(numbered) >= 5

    def test_source_name(self):
        releases = parse_search_page(load("getcomics_search.html"), "https://getcomics.org")
        assert all(r.source_name == "getcomics" for r in releases)


class TestParseDownloadButtons:
    """Against a real getcomics.org post page (Absolute Batman #15)."""

    def test_all_buttons_found(self):
        buttons = parse_download_buttons(load("getcomics_post.html"))
        labels = {b.label.upper() for b in buttons}
        assert "DOWNLOAD NOW" in labels
        assert "MEGA" in labels
        assert "PIXELDRAIN" in labels

    def test_main_server_link(self):
        buttons = parse_download_buttons(load("getcomics_post.html"))
        links = main_server_links(buttons)
        assert len(links) == 1
        assert links[0].startswith("https://getcomics.org/dls/")

    def test_read_online_not_a_download(self):
        buttons = parse_download_buttons(load("getcomics_post.html"))
        links = main_server_links(buttons)
        assert not any("readcomicsonline" in l for l in links)

    def test_pixeldrain_mirror_present(self):
        buttons = parse_download_buttons(load("getcomics_post.html"))
        assert mirror_link(buttons, "PIXELDRAIN") is not None


class TestPixeldrainApiUrl:
    def test_user_page(self):
        assert pixeldrain_api_url("https://pixeldrain.com/u/jb9EAA5h") == \
            "https://pixeldrain.com/api/file/jb9EAA5h?download"

    def test_list_page(self):
        assert pixeldrain_api_url("https://pixeldrain.com/l/abc123") == \
            "https://pixeldrain.com/api/list/abc123/zip?download"

    def test_non_pixeldrain(self):
        assert pixeldrain_api_url("https://mega.nz/file/xyz") is None


class TestVolumeTitles:
    def test_tpb_title_not_treated_as_issue(self):
        html = """
        <article><div class="post-info">
        <h1 class="post-title"><a href="https://getcomics.org/x/">
        Batman and Robin Vol. 3 - The Quiet Man (TPB) (2026)</a></h1>
        </div></article>
        """
        releases = parse_search_page(html, "https://getcomics.org")
        assert len(releases) == 1
        assert releases[0].issue_number is None
        assert releases[0].volume_number == 3

    def test_collected_edition_kept_for_series_browse(self):
        release = SourceRelease(
            source_name="getcomics",
            external_id="url",
            title="Batman and Robin Vol. 3 - The Quiet Man (TPB) (2026)",
            volume_number=3,
        )
        assert GetComicsSource._belongs_to_series(release, "batman and robin")


@pytest.mark.asyncio
@respx.mock
async def test_mediafire_page_resolves_direct_download():
    client = httpx.AsyncClient()
    source = GetComicsSource(client)
    respx.get("https://www.mediafire.com/file/abc/book.cbz/file").mock(
        return_value=httpx.Response(
            200,
            text='<a id="downloadButton" href="https://download123.mediafire.com/token/book.cbz">Download</a>',
        )
    )
    assert await source._resolve_mediafire(
        "https://www.mediafire.com/file/abc/book.cbz/file"
    ) == "https://download123.mediafire.com/token/book.cbz"
    await client.aclose()


COLLECTION_URL = "https://getcomics.org/dc/nightwatch-collection/"


def collection_parent():
    return SourceRelease(
        "getcomics", COLLECTION_URL,
        "Nightwatch (Year 1 – 5 + Nightwatch 2) (Complete Collection) (2013-2018)",
        url=COLLECTION_URL, size_text="10.0 GB",
    )


def collection_releases():
    from pullarr.sources.getcomics import bundle_release, parse_collection_bundles
    return [bundle_release(b, collection_parent())
            for b in parse_collection_bundles(load("getcomics_collection.html"))]


def test_collection_bundles_have_independent_identity_coverage_and_size():
    first, second, volume, sequel = collection_releases()
    assert (first.issue_number, first.issue_end, first.year, first.size_text) == (1, 36, 2013, "912 MB")
    assert "(Year One)" in first.title
    assert (second.issue_number, second.issue_end, second.year) == (1, 24, 2014)
    assert volume.issue_number is None and volume.volume_number == 1
    assert len({r.external_id for r in [first, second, volume, sequel]}) == 4
    assert first.url == COLLECTION_URL
    assert GetComicsSource._belongs_to_series(first, "nightwatch")
    assert not GetComicsSource._belongs_to_series(second, "nightwatch")
    assert not GetComicsSource._belongs_to_series(sequel, "nightwatch")


@pytest.fixture
async def collection_source(monkeypatch):
    from pullarr.sources import getcomics
    from pullarr.util import RateLimiter
    monkeypatch.setattr(getcomics, "_limiter", RateLimiter(100000))
    async with httpx.AsyncClient() as client:
        yield GetComicsSource(client)


@pytest.mark.asyncio
@respx.mock
async def test_collection_search_expands_and_filters_bundles(collection_source, monkeypatch):
    async def search(*args, **kwargs):
        return [collection_parent()]
    monkeypatch.setattr(collection_source, "_search_pages", search)
    respx.get(COLLECTION_URL).respond(200, text=load("getcomics_collection.html"))
    releases = await collection_source.list_releases("Nightwatch")
    assert len(releases) == 2  # original numbered series and its TPB
    assert releases[0].issue_end == 36
    assert all(r.size_text != "10.0 GB" for r in releases)
    assert len(await collection_source.search_releases("Nightwatch 1")) == 4


@pytest.mark.asyncio
@respx.mock
async def test_selected_bundle_resolves_only_its_mirrors(collection_source):
    respx.get(COLLECTION_URL).respond(200, text=load("getcomics_collection.html"))
    respx.get("https://getcomics.org/dls/year-one-pixel").respond(
        302, headers={"location": "https://pixeldrain.com/u/fixture1"},
    )
    options = await collection_source.resolve_downloads(collection_releases()[0].external_id)
    assert options == [
        ["https://getcomics.org/dls/year-one-main"],
        ["https://getcomics.org/dls/year-one-backup"],
        ["https://pixeldrain.com/api/file/fixture1?download"],
    ]


@pytest.mark.asyncio
@respx.mock
async def test_collection_respects_mirror_preference(collection_source):
    collection_source._service_preference = ["pixeldrain", "main"]
    respx.get(COLLECTION_URL).respond(200, text=load("getcomics_collection.html"))
    respx.get("https://getcomics.org/dls/year-one-pixel").respond(
        302, headers={"location": "https://pixeldrain.com/u/fixture1"},
    )
    options = await collection_source.resolve_downloads(collection_releases()[0].external_id)
    assert options[0] == ["https://pixeldrain.com/api/file/fixture1?download"]


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("fragment", ["", "#pullarr-bundle=missing"])
async def test_collection_cannot_download_all_groups_or_guess_missing_group(collection_source, fragment):
    respx.get(COLLECTION_URL).respond(200, text=load("getcomics_collection.html"))
    with pytest.raises(RuntimeError, match="search releases again"):
        await collection_source.resolve_downloads(COLLECTION_URL + fragment)


def test_bundle_ids_survive_reordered_rows():
    from bs4 import BeautifulSoup
    from pullarr.sources.getcomics import parse_collection_bundles
    html = load("getcomics_collection.html")
    original = parse_collection_bundles(html)
    soup = BeautifulSoup(html, "lxml")
    soup.ul.append(soup.li.extract())
    reordered = parse_collection_bundles(str(soup))
    assert [b.key for b in original] != [b.key for b in reordered]
    assert {b.title: b.key for b in original} == {b.title: b.key for b in reordered}


@pytest.mark.asyncio
async def test_collection_bundle_automatically_queues_once_for_original_series(monkeypatch):
    from pullarr.jobs import tasks
    from pullarr.models import Issue, Series
    from pullarr.util import normalize_title
    calls = []
    async def enqueue(*args):
        calls.append(args)
    monkeypatch.setattr(tasks, "enqueue_direct", enqueue)
    series = Series(id=1, title="Nightwatch", alt_titles="")
    remaining = {float(n): Issue(id=n, series_id=1, number=n) for n in range(1, 37)}
    assert await tasks._grab_matches(
        object(), series, "getcomics", collection_releases(), remaining,
        {normalize_title(series.title)}, set(),
    ) == 1
    assert not remaining and len(calls) == 1
    assert calls[0][4] == collection_releases()[0].external_id


@pytest.mark.asyncio
@respx.mock
async def test_modern_multipart_buttons_remain_one_option(collection_source):
    respx.get(COLLECTION_URL).respond(200, text='''
        <div class="aio-button-center"><a title="DOWNLOAD NOW" href="https://files.test/part1">Part 1</a></div>
        <div class="aio-button-center"><a title="DOWNLOAD NOW" href="https://files.test/part2">Part 2</a></div>
    ''')
    assert await collection_source.resolve_downloads(COLLECTION_URL) == [
        ["https://files.test/part1", "https://files.test/part2"]
    ]
