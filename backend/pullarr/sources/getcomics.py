"""GetComics scraper (getcomics.org).

Search results are WordPress <article> cards; each post page carries
download buttons (div.aio-button-center > a, titled DOWNLOAD NOW / MEGA /
PIXELDRAIN / …). The "DOWNLOAD NOW" main-server link is an encoded
/dls/ URL that 302-redirects to the actual file (comicfiles.ru), which is
the one reliable direct download — mirrors need per-host handling and are
skipped.

All traffic (searches and the file downloads made by the DDL worker using
`client`) honors the optional proxy setting so it can be routed through a
VPN."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .. import USER_AGENT
from ..util import (
    RateLimiter,
    has_issue_marker,
    normalize_title,
    parse_issue_number,
    parse_volume_number,
    parse_year,
    rl_request,
    strip_issue_suffix,
)
from .base import DDLSource, SourceRelease, SourceSeries

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://getcomics.org"

# be a polite scraper: one page fetch per 2s
_limiter = RateLimiter(rate=1, per_seconds=2)

SEARCH_PAGES = 2  # result pages fetched per series query (20 posts/page)

SIZE_RE = re.compile(r"Size\s*:\s*([\d.,]+\s*[KMGT]?B)", re.I)


@dataclass
class DownloadButton:
    label: str
    url: str


def parse_search_page(html: str, base_url: str) -> list[SourceRelease]:
    """Parse the post cards on a search results page."""
    soup = BeautifulSoup(html, "lxml")
    releases: list[SourceRelease] = []
    for article in soup.find_all("article"):
        title_el = article.select_one(".post-title a")
        if title_el is None:
            continue
        url = urljoin(base_url, title_el.get("href", ""))
        title = title_el.get_text(strip=True)
        if not title or not url:
            continue
        # the excerpt's "Size :" marker sits in invalidly-nested <p> tags that
        # parsers relocate, so read it from the whole card's text
        m = SIZE_RE.search(article.get_text(" ", strip=True))
        size_text = m.group(1).strip() if m else ""
        posted_at = None
        time_el = article.find("time")
        if time_el is not None and time_el.get("datetime"):
            try:
                posted_at = datetime.fromisoformat(time_el["datetime"]).replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass
        issue = parse_issue_number(title)
        volume = parse_volume_number(title)
        # a bare volume title ("Batman Vol. 3 – … (TPB)") has no issue marker,
        # so a trailing number would be the volume, not an issue
        if volume is not None and not has_issue_marker(title):
            issue = None
        releases.append(SourceRelease(
            source_name="getcomics",
            external_id=url,
            title=title,
            url=url,
            issue_number=issue,
            volume_number=volume,
            year=parse_year(title),
            size_text=size_text,
            posted_at=posted_at,
        ))
    return releases


def parse_download_buttons(html: str) -> list[DownloadButton]:
    """All download buttons on a post page, in page order."""
    soup = BeautifulSoup(html, "lxml")
    buttons: list[DownloadButton] = []
    for div in soup.select("div.aio-button-center"):
        a = div.find("a", href=True)
        if a is None:
            continue
        label = (a.get("title") or a.get_text(strip=True) or "").strip()
        buttons.append(DownloadButton(label=label, url=a["href"]))
    return buttons


def main_server_links(buttons: list[DownloadButton]) -> list[str]:
    """The main-server ("DOWNLOAD NOW") links — resolve to a direct file via a
    plain redirect. Multi-part packs have several (all parts of one release)."""
    return [b.url for b in buttons if b.label.upper() == "DOWNLOAD NOW"]


def mirror_link(buttons: list[DownloadButton], label: str) -> str | None:
    for b in buttons:
        if b.label.upper() == label.upper():
            return b.url
    return None


PIXELDRAIN_ID_RE = re.compile(r"pixeldrain\.com/(?:u|l)/([A-Za-z0-9]+)")


def pixeldrain_api_url(page_url: str) -> str | None:
    """Direct-download API URL for a pixeldrain file/list page."""
    m = PIXELDRAIN_ID_RE.search(page_url)
    return f"https://pixeldrain.com/api/file/{m.group(1)}?download" if m else None


class GetComicsSource(DDLSource):
    name = "getcomics"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or self._make_client(None)
        self._base_url = DEFAULT_BASE_URL
        self._proxy: str | None = None
        self._external_client = client is not None

    @staticmethod
    def _make_client(proxy: str | None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=60,
            follow_redirects=True,
            proxy=proxy or None,
        )

    def configure(self, base_url: str, proxy: str) -> None:
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        proxy = proxy.strip() or None
        if not self._external_client and proxy != self._proxy:
            self._proxy = proxy
            self._client = self._make_client(proxy)

    @property
    def client(self) -> httpx.AsyncClient:
        """Shared client (proxy-aware) — the DDL worker downloads files
        through this so file transfers follow the same VPN route."""
        return self._client

    @property
    def limiter(self) -> RateLimiter:
        return _limiter

    async def _search_pages(self, query: str, pages: int) -> list[SourceRelease]:
        releases: list[SourceRelease] = []
        seen: set[str] = set()
        for page in range(1, pages + 1):
            url = self._base_url if page == 1 else f"{self._base_url}/page/{page}"
            resp = await rl_request(
                self._client, "GET", url, limiter=_limiter, params={"s": query}
            )
            if resp.status_code == 404:  # past the last page
                break
            resp.raise_for_status()
            page_releases = parse_search_page(resp.text, self._base_url)
            new = [r for r in page_releases if r.url not in seen]
            seen.update(r.url for r in new)
            releases.extend(new)
            if len(page_releases) < 10:  # short page → last page
                break
        return releases

    async def search_series(self, query: str) -> list[SourceSeries]:
        """Distinct series names appearing in post titles for this query —
        picking one sets the search term used for all future release lookups."""
        releases = await self._search_pages(query, pages=1)
        results: list[SourceSeries] = []
        seen: set[str] = set()
        for r in releases:
            series_name = strip_issue_suffix(r.title)
            key = normalize_title(series_name)
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(SourceSeries(
                source_name=self.name,
                external_id=series_name,
                title=series_name,
                url=r.url,
            ))
        return results

    async def list_releases(self, external_id: str) -> list[SourceRelease]:
        """Releases whose parsed series name matches the linked search term."""
        wanted = normalize_title(external_id)
        releases = await self._search_pages(external_id, pages=SEARCH_PAGES)
        return [
            r for r in releases
            if normalize_title(strip_issue_suffix(r.title)) == wanted
        ]

    async def search_releases(self, query: str) -> list[SourceRelease]:
        return await self._search_pages(query, pages=1)

    async def resolve_downloads(self, release_external_id: str) -> list[list[str]]:
        resp = await rl_request(
            self._client, "GET", release_external_id, limiter=_limiter
        )
        resp.raise_for_status()
        buttons = parse_download_buttons(resp.text)
        options: list[list[str]] = []

        # primary: the main server (may be multi-part)
        main = main_server_links(buttons)
        if main:
            options.append(main)

        # fallback: the Pixeldrain mirror — a clean direct download when the
        # main server is blocked/down (older files often 403 on comicfiles).
        # Its getcomics /dls/ link 302s to a pixeldrain page we turn into the
        # download API URL.
        px_dls = mirror_link(buttons, "PIXELDRAIN")
        if px_dls:
            api_url = await self._resolve_pixeldrain(px_dls)
            if api_url:
                options.append([api_url])

        if not options:
            raise RuntimeError("post has no usable download link")
        return options

    async def _resolve_pixeldrain(self, dls_url: str) -> str | None:
        try:
            resp = await self._client.get(dls_url, follow_redirects=False)
            location = resp.headers.get("location", "")
            return pixeldrain_api_url(location)
        except httpx.HTTPError as exc:
            log.warning("pixeldrain resolve failed: %s", exc)
            return None


source = GetComicsSource()
