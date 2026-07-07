export interface RootFolder {
  id: number;
  path: string;
}

export interface SourceLink {
  id: number;
  source_name: string;
  external_id: string;
  external_title: string;
  external_url: string;
}

export interface Issue {
  id: number;
  number: number;
  volume: number | null;
  title: string;
  monitored: boolean;
  downloaded: boolean;
  file_path: string;
  released_at: string | null;
}

export interface Series {
  id: number;
  comicvine_id: number | null;
  title: string;
  description: string;
  status: string;
  publisher: string;
  year: number | null;
  cover_url: string;
  genres: string;
  monitored: boolean;
  root_folder_id: number | null;
  folder_name: string;
  total_issues: number | null;
  added_at: string;
  issue_count: number;
  downloaded_count: number;
}

export interface SeriesDetail extends Series {
  issues: Issue[];
  source_links: SourceLink[];
}

export interface MetadataResult {
  provider: string;
  provider_id: string;
  title: string;
  alt_titles: string[];
  description: string;
  status: string;
  publisher: string;
  year: number | null;
  cover_url: string;
  genres: string[];
  total_issues: number | null;
  in_library: boolean;
}

export interface Release {
  kind: "ddl" | "torrent";
  source_name: string;
  title: string;
  issue_number: number | null;
  issue_end: number | null;
  volume_number: number | null;
  external_id: string;
  url: string;
  magnet: string;
  size_text: string;
  year: number | null;
  posted_at: string | null;
  size_bytes: number;
  seeders: number;
  leechers: number;
}

export interface QueueItem {
  id: number;
  series_id: number | null;
  issue_id: number | null;
  kind: string;
  status: string;
  title: string;
  source_name: string;
  progress: number;
  error: string;
  created_at: string;
  series_title: string;
}

export interface HistoryItem {
  id: number;
  series_id: number | null;
  event: string;
  detail: string;
  source_name: string;
  created_at: string;
  series_title: string;
}

export interface WantedItem {
  issue_id: number;
  series_id: number;
  series_title: string;
  cover_url: string;
  number: number;
  volume: number | null;
  title: string;
  released_at: string | null;
}

export interface SystemStatus {
  version: string;
  series_count: number;
  issue_count: number;
  downloaded_count: number;
  queue_count: number;
}

export type Settings = Record<string, string>;

export interface ScanResult {
  folder: string;
  folder_exists: boolean;
  matched_issues: number;
  volume_files: number;
  cleared: number;
  unmatched: string[];
}

export interface RenameItem {
  issue_ids: number[];
  current_path: string;
  current_name: string;
  new_path: string;
  new_name: string;
  conflict: boolean;
}

export interface RenameOutcome {
  current_name: string;
  new_name: string;
  status: string;
  detail: string;
}

export interface SeriesFile {
  covered_count: number;
  path: string;
  name: string;
  is_dir: boolean;
  issue_number: number | null;
  volume_number: number | null;
  matched_issue_id: number | null;
}

export interface CleanupFile {
  path: string;
  name: string;
  size: number;
  referenced: boolean;
  keep: boolean;
}

export interface CleanupGroup {
  label: string;
  files: CleanupFile[];
}

export interface CleanupPlan {
  groups: CleanupGroup[];
  orphans: CleanupFile[];
}

export interface SourceCandidate {
  source_name: string;
  external_id: string;
  title: string;
  url: string;
  alt_titles: string[];
}

export interface SeriesFolder {
  id: number | null;
  path: string;
  resolved: string;
  primary: boolean;
  exists: boolean;
}

export interface FilesystemEntry {
  name: string;
  path: string;
}

export interface FilesystemList {
  path: string;
  parent: string | null;
  entries: FilesystemEntry[];
}
