from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RootFolderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    path: str


class RootFolderIn(BaseModel):
    path: str


class SourceLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_name: str
    external_id: str
    external_title: str
    external_url: str


class IssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    metron_id: int | None
    number: float
    display_number: str
    volume: int | None
    title: str
    summary: str
    imprint: str
    writers: str
    pencillers: str
    inkers: str
    colorists: str
    letterers: str
    cover_artists: str
    editors: str
    translators: str
    story_arcs: str
    reprints: str
    characters: str
    teams: str
    genres: str
    web_url: str
    format: str
    language: str
    page_count: int | None
    monitored: bool
    downloaded: bool
    file_path: str
    released_at: datetime | None


class SeriesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    comicvine_id: int | None
    metron_id: int | None
    title: str
    description: str
    status: str
    publisher: str
    year: int | None
    cover_url: str
    genres: str
    monitored: bool
    root_folder_id: int | None
    folder_name: str
    total_issues: int | None
    added_at: datetime
    issue_count: int = 0
    downloaded_count: int = 0


class SeriesDetailOut(SeriesOut):
    issues: list[IssueOut] = []
    source_links: list[SourceLinkOut] = []


class AddSeriesIn(BaseModel):
    comicvine_id: int
    root_folder_id: int
    monitored: bool = True
    search_now: bool = False
    # series folder under the root; empty means derive from title/year
    folder_name: str = ""
    extra_folders: list[str] = Field(default_factory=list)


class FolderPreviewIn(BaseModel):
    """Ask which folder a prospective ComicVine volume would use before add."""
    root_folder_id: int
    title: str
    year: int | None = None
    alt_titles: list[str] = Field(default_factory=list)


class FolderPreviewOut(BaseModel):
    folder_name: str
    path: str
    exists: bool
    matched: bool  # an existing folder was adopted (vs a fresh default name)


class SeriesUpdateIn(BaseModel):
    monitored: bool | None = None
    root_folder_id: int | None = None
    folder_name: str | None = None


class IssueMonitorIn(BaseModel):
    issue_ids: list[int]
    monitored: bool


class MetadataResult(BaseModel):
    provider: str
    provider_id: str
    title: str
    alt_titles: list[str]
    description: str
    status: str
    publisher: str
    year: int | None
    cover_url: str
    genres: list[str]
    total_issues: int | None
    in_library: bool = False


class ReleaseOut(BaseModel):
    """Interactive-search result: a DDL release (post) or a torrent."""
    kind: str  # ddl | torrent
    source_name: str
    title: str
    issue_number: float | None = None
    issue_end: float | None = None  # last issue when the release is a bundle
    volume_number: int | None = None
    external_id: str = ""  # ddl: post URL
    url: str = ""
    magnet: str = ""
    size_text: str = ""
    year: int | None = None
    posted_at: datetime | None = None
    size_bytes: int = 0
    seeders: int = 0
    leechers: int = 0


class GrabIn(BaseModel):
    # DDL grab (issue_id optional: series-level packs import by filename)
    issue_id: int | None = None
    source_name: str | None = None
    external_id: str | None = None
    # torrent grab
    series_id: int | None = None
    magnet: str | None = None
    title: str | None = None


class QueueRemoveIn(BaseModel):
    ids: list[int]


class QueueRemoveOut(BaseModel):
    removed: int


class QueueItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    series_id: int | None
    issue_id: int | None
    kind: str
    status: str
    title: str
    source_name: str
    progress: float
    error: str
    error_code: str
    attempt_count: int
    next_retry_at: datetime | None
    blocked: bool
    created_at: datetime
    series_title: str = ""


class HistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    series_id: int | None
    event: str
    detail: str
    source_name: str
    created_at: datetime
    series_title: str = ""


class WantedItemOut(BaseModel):
    issue_id: int
    series_id: int
    series_title: str
    cover_url: str
    number: float
    display_number: str
    volume: int | None
    title: str
    released_at: datetime | None
    series_monitored: bool
    issue_monitored: bool


class QbtTestIn(BaseModel):
    url: str
    username: str
    password: str


class ComicVineTestIn(BaseModel):
    api_key: str


class MetronTestIn(BaseModel):
    username: str
    password: str


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    status: str
    series_id: int | None
    progress: float
    phase: str
    detail: str
    error: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime
    series_title: str = ""


class SystemStatus(BaseModel):
    version: str
    series_count: int
    issue_count: int
    downloaded_count: int
    queue_count: int


# ---- library import / scan / rename ----

class ScanResultOut(BaseModel):
    folder: str
    folder_exists: bool
    matched_issues: int
    volume_files: int
    cleared: int
    unmatched: list[str] = []


class RenameItemOut(BaseModel):
    issue_ids: list[int]
    current_path: str
    current_name: str
    new_path: str
    new_name: str
    conflict: bool = False


class RenameApplyIn(BaseModel):
    # optional subset; when omitted, apply all currently-planned renames
    issue_ids: list[int] | None = None


class RenameOutcomeOut(BaseModel):
    current_name: str
    new_name: str
    status: str
    detail: str = ""


class SeriesFileOut(BaseModel):
    path: str
    name: str
    is_dir: bool
    issue_number: float | None = None
    volume_number: int | None = None
    matched_issue_id: int | None = None
    covered_count: int = 0  # issues this file covers (N for a volume archive)


class FileMapIn(BaseModel):
    file_path: str
    issue_id: int


class FileMapRangeIn(BaseModel):
    file_path: str
    from_number: float
    to_number: float


class FileMapRangeOut(BaseModel):
    mapped: int
    volume: int | None


class CleanupFileOut(BaseModel):
    path: str
    name: str
    size: int
    referenced: bool
    keep: bool


class CleanupGroupOut(BaseModel):
    label: str
    files: list[CleanupFileOut]


class CleanupPlanOut(BaseModel):
    groups: list[CleanupGroupOut] = []
    orphans: list[CleanupFileOut] = []


class CleanupApplyIn(BaseModel):
    delete: list[str]


class CleanupResultOut(BaseModel):
    deleted: int
    repointed: int
    skipped: int
    freed_bytes: int


class SourceCandidateOut(BaseModel):
    source_name: str
    external_id: str
    title: str
    url: str = ""
    alt_titles: list[str] = []


class SourceLinkIn(BaseModel):
    source_name: str
    external_id: str
    external_title: str = ""
    external_url: str = ""


class ResyncOut(BaseModel):
    issues: int
    matched_issues: int


class SeriesFolderOut(BaseModel):
    id: int | None  # None for the primary folder
    path: str
    resolved: str
    primary: bool
    exists: bool


class SeriesFolderIn(BaseModel):
    path: str


class FilesystemEntryOut(BaseModel):
    name: str
    path: str


class FilesystemListOut(BaseModel):
    path: str
    parent: str | None
    entries: list[FilesystemEntryOut]
