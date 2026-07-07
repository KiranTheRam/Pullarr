from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SeriesMetadata:
    provider: str
    provider_id: str
    title: str
    alt_titles: list[str] = field(default_factory=list)
    description: str = ""
    status: str = "unknown"  # matches models.SeriesStatus values
    publisher: str = ""
    year: int | None = None
    cover_url: str = ""
    genres: list[str] = field(default_factory=list)
    total_issues: int | None = None


@dataclass
class IssueMetadata:
    provider_id: str  # the issue's id at the provider
    number: float
    title: str = ""
    released_at: datetime | None = None
    cover_url: str = ""


class MetadataProvider(ABC):
    """Series + issue metadata. Unlike mangarr (where content sources supply
    chapter lists), the provider is the single authority for the issue list."""

    name: str

    @abstractmethod
    async def search(self, query: str, limit: int = 20) -> list[SeriesMetadata]: ...

    @abstractmethod
    async def get_series(self, provider_id: str) -> SeriesMetadata | None: ...

    @abstractmethod
    async def list_issues(self, provider_id: str) -> list[IssueMetadata]: ...
