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
