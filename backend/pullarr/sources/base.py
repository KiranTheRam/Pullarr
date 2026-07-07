from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SourceSeries:
    """A series identity on a source. For DDL sites without series pages the
    external_id is the search term whose posts belong to the series."""

    source_name: str
    external_id: str
    title: str
    url: str = ""
    alt_titles: list[str] = field(default_factory=list)


@dataclass
class SourceRelease:
    """One downloadable release (post) on a DDL source: a single issue, a
    TPB/collected volume, or a multi-issue pack."""

    source_name: str
    external_id: str  # what resolve_downloads needs (the post URL)
    title: str
    url: str = ""
    issue_number: float | None = None  # parsed from the title, if single-issue
    # for a multi-issue bundle ("#1-3"), issue_number is the first and
    # issue_end the last; the release covers every issue in [start, end]
    issue_end: float | None = None
    volume_number: int | None = None  # parsed TPB/volume marker
    year: int | None = None
    size_text: str = ""  # human-readable ("125 MB")
    posted_at: datetime | None = None


@dataclass
class TorrentRelease:
    source_name: str
    title: str
    magnet: str
    url: str = ""
    size_bytes: int = 0
    seeders: int = 0
    leechers: int = 0


class DDLSource(ABC):
    """A site we can search for releases and resolve into direct-download
    file URLs. Files arrive as ready-made CBR/CBZ archives (or packs of
    them), so downloads go through the shared archive importer."""

    name: str

    @abstractmethod
    async def search_series(self, query: str) -> list[SourceSeries]: ...

    @abstractmethod
    async def list_releases(self, external_id: str) -> list[SourceRelease]:
        """Releases whose titles match the series search term."""

    @abstractmethod
    async def search_releases(self, query: str) -> list[SourceRelease]:
        """Free-form release search (interactive search / issue fallback)."""

    @abstractmethod
    async def resolve_downloads(self, release_external_id: str) -> list[list[str]]:
        """Ordered download options for a release, best mirror first. Each
        option is a list of file-part URLs that together make up the release
        (usually one file). The worker tries options in order until one fully
        downloads, so a blocked/dead primary mirror falls back to the next."""


class TorrentIndexer(ABC):
    name: str

    @abstractmethod
    async def search(self, query: str) -> list[TorrentRelease]: ...
