import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import type {
  Issue,
  QueueItem,
  Release,
  ScanResult,
  SeriesDetail as SeriesDetailType,
} from "../api/types";
import {
  issueLabel,
  formatBytes,
  Modal,
  Spinner,
  statusPill,
  Toolbar,
} from "../components/common";
import {
  CleanupModal,
  FilesModal,
  FoldersPanel,
  RenameModal,
  SourcesModal,
} from "../components/LibraryTools";

function InteractiveSearch({
  seriesId,
  issueId,
  title,
  onClose,
}: {
  seriesId: number;
  issueId?: number;
  title: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const params = issueId != null ? `issue_id=${issueId}` : `series_id=${seriesId}`;
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["releases", params],
    queryFn: () => api.get<Release[]>(`/search/releases?${params}`),
  });

  const grab = useMutation({
    mutationFn: (release: Release) =>
      api.post("/queue/grab", release.kind === "ddl"
        ? {
            issue_id: issueId,
            series_id: seriesId,
            source_name: release.source_name,
            external_id: release.external_id,
            title: release.title,
          }
        : { series_id: seriesId, magnet: release.magnet, title: release.title }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["queue"] });
      onClose();
    },
  });

  return (
    <Modal title={`Search — ${title}`} onClose={onClose}>
      {isLoading ? (
        <Spinner />
      ) : isError ? (
        <div className="error-banner">{(error as Error).message}</div>
      ) : !data || data.length === 0 ? (
        <p style={{ color: "var(--text-dim)" }}>
          No releases found. Check source links and that sources are enabled in Settings.
        </p>
      ) : (
        <>
          {grab.isError && <div className="error-banner">{(grab.error as Error).message}</div>}
          <table className="data-table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Title</th>
                <th>Size</th>
                <th>Year / Peers</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.map((r, i) => (
                <tr key={i}>
                  <td>
                    <span className={`pill ${r.kind === "torrent" ? "orange" : "blue"}`}>
                      {r.source_name}
                    </span>
                  </td>
                  <td>
                    {r.url ? (
                      <a href={r.url} target="_blank" rel="noreferrer" style={{ color: "var(--info)" }}>
                        {r.title}
                      </a>
                    ) : (
                      r.title
                    )}
                  </td>
                  <td>{r.kind === "torrent" ? formatBytes(r.size_bytes) : r.size_text || "—"}</td>
                  <td>{r.kind === "torrent" ? `${r.seeders}/${r.leechers}` : r.year ?? "—"}</td>
                  <td>
                    <button
                      className="btn icon-btn"
                      title="Grab"
                      disabled={grab.isPending}
                      onClick={() => grab.mutate(r)}
                    >
                      ⇓
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Modal>
  );
}

function groupByVolume(issues: Issue[]): { volume: number | null; issues: Issue[] }[] {
  const byVolume = new Map<number | null, Issue[]>();
  for (const ch of issues) {
    const key = ch.volume;
    if (!byVolume.has(key)) byVolume.set(key, []);
    byVolume.get(key)!.push(ch);
  }
  // volume-less issues first (usually the newest, not yet collected),
  // then volumes descending — like Sonarr's latest-season-on-top
  return [...byVolume.entries()]
    .sort(([a], [b]) => (a === null ? -1 : b === null ? 1 : b - a))
    .map(([volume, chs]) => ({ volume, issues: chs.sort((a, b) => b.number - a.number) }));
}

export default function SeriesDetail() {
  const { id } = useParams();
  const seriesId = Number(id);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState<{ issueId?: number; title: string } | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
  const toggleReveal = (k: string) => setRevealed((r) => ({ ...r, [k]: !r[k] }));
  const [showRename, setShowRename] = useState(false);
  const [showFiles, setShowFiles] = useState(false);
  const [showSources, setShowSources] = useState(false);
  const [showCleanup, setShowCleanup] = useState(false);
  const [scanResult, setScanResult] = useState<ScanResult | null>(null);
  const [workNotice, setWorkNotice] = useState<string | null>(null);

  const showWorkNotice = (message: string, timeout = 9000) => {
    setWorkNotice(message);
    window.setTimeout(
      () => setWorkNotice((current) => (current === message ? null : current)),
      timeout,
    );
  };

  const { data: series, isLoading } = useQuery({
    queryKey: ["series", seriesId],
    queryFn: () => api.get<SeriesDetailType>(`/series/${seriesId}`),
    refetchInterval: 10000,
  });

  const { data: queue } = useQuery({
    queryKey: ["queue"],
    queryFn: () => api.get<QueueItem[]>("/queue"),
    refetchInterval: 2000,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["series", seriesId] });
    queryClient.invalidateQueries({ queryKey: ["series"] });
  };

  const toggleMonitor = useMutation({
    mutationFn: () => api.put(`/series/${seriesId}`, { monitored: !series?.monitored }),
    onSuccess: invalidate,
  });

  const refresh = useMutation({
    mutationFn: () => api.post(`/series/${seriesId}/refresh`),
    onSuccess: () => {
      showWorkNotice("Refreshing metadata, source links, and issues…");
      setTimeout(invalidate, 4000);
    },
  });

  const scan = useMutation({
    mutationFn: () => api.post<ScanResult>(`/series/${seriesId}/scan`),
    onSuccess: (res) => {
      setScanResult(res);
      invalidate();
    },
  });


  const deleteSeries = useMutation({
    mutationFn: () => api.del(`/series/${seriesId}`),
    onSuccess: () => {
      invalidate();
      navigate("/");
    },
  });

  const toggleIssue = useMutation({
    mutationFn: (args: { issueIds: number[]; monitored: boolean }) =>
      api.put(`/series/${seriesId}/issues/monitor`, {
        issue_ids: args.issueIds,
        monitored: args.monitored,
      }),
    onSuccess: invalidate,
  });

  if (isLoading || !series) {
    return (
      <>
        <Toolbar title="Series" />
        <Spinner />
      </>
    );
  }

  const hasVolumes = series.issues.some((c) => c.volume !== null);
  const groups = hasVolumes
    ? groupByVolume(series.issues)
    : [{ volume: null, issues: [...series.issues].sort((a, b) => b.number - a.number) }];

  // how many issues share each file — a file used by >1 issue is a
  // whole-volume archive (those issues have no individual file of their own)
  const fileCounts: Record<string, number> = {};
  for (const c of series.issues) {
    if (c.file_path) fileCounts[c.file_path] = (fileCounts[c.file_path] ?? 0) + 1;
  }
  const isVolumeArchive = (path: string) => (fileCounts[path] ?? 0) > 1;

  const activeDownloads = (queue ?? []).filter((item) => item.series_id === seriesId);
  const toolbarStatus =
    scan.isPending ? "Scanning disk" :
    refresh.isPending ? "Starting refresh" :
    deleteSeries.isPending ? "Removing series" :
    toggleMonitor.isPending ? "Updating monitoring" :
    workNotice;
  const hasTopBanners =
    Boolean(workNotice || scanResult) || activeDownloads.length > 0;

  const issueRows = (issues: Issue[]) => (
    <table className="data-table">
      <thead>
        <tr>
          <th style={{ width: 36 }}></th>
          <th style={{ width: 130 }}>Issue</th>
          <th>Title</th>
          <th style={{ width: 120 }}>Status</th>
          <th style={{ width: 90 }}></th>
        </tr>
      </thead>
      <tbody>
        {issues.map((ch) => (
          <tr key={ch.id}>
            <td>
              <button
                className={`monitor-toggle${ch.monitored ? " on" : ""}`}
                title={ch.monitored ? "Monitored" : "Unmonitored"}
                onClick={() =>
                  toggleIssue.mutate({ issueIds: [ch.id], monitored: !ch.monitored })
                }
              >
                {ch.monitored ? "🔖" : "◻"}
              </button>
            </td>
            <td>
              {ch.downloaded && ch.file_path && !isVolumeArchive(ch.file_path) ? (
                <button
                  className="link-text"
                  title="Show filename on disk"
                  onClick={() => toggleReveal(`c${ch.id}`)}
                >
                  {issueLabel(ch.number, ch.volume)}
                </button>
              ) : (
                issueLabel(ch.number, ch.volume)
              )}
              {revealed[`c${ch.id}`] && ch.file_path && (
                <div className="filepath">{ch.file_path}</div>
              )}
            </td>
            <td style={{ color: ch.title ? "inherit" : "var(--text-faint)" }}>
              {ch.title || "—"}
            </td>
            <td>
              {ch.downloaded ? (
                <span className="pill green" title={ch.file_path}>
                  Downloaded
                </span>
              ) : (
                <span className="pill gray">Missing</span>
              )}
            </td>
            <td>
              <button
                className="btn icon-btn"
                title="Interactive search"
                onClick={() =>
                  setSearch({
                    issueId: ch.id,
                    title: `${series.title} ${issueLabel(ch.number, ch.volume)}`,
                  })
                }
              >
                🔍
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  return (
    <>
      <Toolbar className="series-toolbar">
        <button
          className="btn"
          title="Refresh metadata, source links, issues, and library state"
          onClick={() => refresh.mutate()}
          disabled={refresh.isPending}
        >
          ⟳ Refresh
        </button>
        <button
          className="btn"
          title="Scan this series' folders and match files on disk"
          onClick={() => scan.mutate()}
          disabled={scan.isPending}
        >
          {scan.isPending ? "Scanning…" : "🗂 Scan Disk"}
        </button>
        <button
          className="btn"
          title="Browse detected files and manually map unmatched files (e.g. a TPB to an issue range)"
          onClick={() => setShowFiles(true)}
        >
          📄 Files
        </button>
        <button
          className="btn"
          title="Preview and apply the configured file naming pattern"
          onClick={() => setShowRename(true)}
        >
          ✏️ Rename
        </button>
        <button
          className="btn"
          title="Find duplicate or orphaned files that can be cleaned up"
          onClick={() => setShowCleanup(true)}
        >
          🧹 Clean up
        </button>
        <button
          className="btn"
          title="Search GetComics for every release of this series (issues, TPBs, packs)"
          onClick={() => setSearch({ title: `${series.title} (all releases)` })}
        >
          🔍 Search Releases
        </button>
        <button
          className="btn"
          title={
            series.monitored
              ? "Stop automatically grabbing new issues"
              : "Automatically grab new issues"
          }
          onClick={() => toggleMonitor.mutate()}
          disabled={toggleMonitor.isPending}
        >
          {series.monitored ? "🔖 Monitored" : "◻ Unmonitored"}
        </button>
        {toolbarStatus && (
          <span className="toolbar-activity" title={toolbarStatus}>
            <span className="mini-spinner" />
            {toolbarStatus}
          </span>
        )}
        <button
          className="btn danger"
          title="Remove this series from Pullarr without deleting files"
          disabled={deleteSeries.isPending}
          onClick={() => {
            if (confirm(`Remove "${series.title}" from library? Files on disk are kept.`))
              deleteSeries.mutate();
          }}
        >
          ✕
        </button>
      </Toolbar>
      <div className="content">
        {workNotice && (
          <div className="activity-banner">
            <span className="mini-spinner" />
            <strong>Working.</strong>
            <span>{workNotice}</span>
          </div>
        )}
        {activeDownloads.length > 0 && (
          <div className="activity-banner">
            <span className="mini-spinner" />
            <strong>Pulling issues.</strong>
            {activeDownloads.slice(0, 3).map((item) => (
              <span className="activity-chip" key={item.id} title={item.title || item.series_title}>
                {item.status} · {Math.round(item.progress * 100)}%
              </span>
            ))}
            {activeDownloads.length > 3 && (
              <span className="activity-chip">+{activeDownloads.length - 3} more</span>
            )}
          </div>
        )}
        {scanResult && (
          <div className="scan-banner" onClick={() => setScanResult(null)}>
            <strong>Scan complete.</strong> {scanResult.matched_issues} issue
            {scanResult.matched_issues === 1 ? "" : "s"} matched
            {scanResult.volume_files > 0 && `, ${scanResult.volume_files} volume file(s)`}
            {scanResult.unmatched.length > 0 &&
              `, ${scanResult.unmatched.length} unmatched`}
            {scanResult.cleared > 0 && `, ${scanResult.cleared} cleared (missing)`}
            {!scanResult.folder_exists && " — folder not found on disk"}
            {scanResult.unmatched.length > 0 && (
              <button className="btn sm" onClick={() => setShowFiles(true)}>
                Review files
              </button>
            )}
          </div>
        )}
        <div className={`series-header${hasTopBanners ? "" : " flush-top"}`}>
          {series.cover_url && <img className="cover" src={series.cover_url} alt="" />}
          <div>
            <h2>
              {series.title}{" "}
              {series.year && <span style={{ color: "var(--text-dim)" }}>({series.year})</span>}
            </h2>
            <div className="series-meta">
              <span className={`pill ${statusPill[series.status] ?? "gray"}`}>{series.status}</span>
              <span>
                {series.downloaded_count} / {series.issue_count} issues
              </span>
              {series.publisher && <span>{series.publisher}</span>}
            </div>
            <div style={{ marginBottom: 10 }}>
              {series.genres
                .split(",")
                .filter(Boolean)
                .map((g) => (
                  <span className="tag" key={g}>
                    {g}
                  </span>
                ))}
            </div>
            <div className="series-desc" dangerouslySetInnerHTML={{ __html: series.description }} />
            <div style={{ marginTop: 12, display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
              {series.source_links.map((sl) => (
                <span className="tag" key={sl.id} title={sl.external_title}>
                  🔗 {sl.source_name}
                </span>
              ))}
              <button className="btn sm" onClick={() => setShowSources(true)}>
                {series.source_links.length ? "Edit sources" : "Add sources"}
              </button>
            </div>
            <FoldersPanel seriesId={seriesId} onChanged={invalidate} />
          </div>
        </div>

        {series.issues.length === 0 ? (
          <p style={{ color: "var(--text-dim)" }}>
            No issues found yet — sources may still be syncing. Use Refresh to retry.
          </p>
        ) : !hasVolumes ? (
          issueRows(groups[0].issues)
        ) : (
          groups.map(({ volume, issues }) => {
            const key = volume === null ? "none" : String(volume);
            const isCollapsed = collapsed[key] ?? false;
            const downloaded = issues.filter((c) => c.downloaded).length;
            const allMonitored = issues.every((c) => c.monitored);
            // the single archive file backing this volume, if it is one
            const files = new Set(
              issues.filter((c) => c.downloaded && c.file_path).map((c) => c.file_path),
            );
            const archiveFile =
              files.size === 1 && isVolumeArchive([...files][0]) ? [...files][0] : null;
            return (
              <div className="volume-group" key={key}>
                <div
                  className="volume-header"
                  onClick={() => setCollapsed({ ...collapsed, [key]: !isCollapsed })}
                >
                  <span className="chevron">{isCollapsed ? "▸" : "▾"}</span>
                  <button
                    className={`monitor-toggle${allMonitored ? " on" : ""}`}
                    title={allMonitored ? "Unmonitor this volume" : "Monitor this volume"}
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleIssue.mutate({
                        issueIds: issues.map((c) => c.id),
                        monitored: !allMonitored,
                      });
                    }}
                  >
                    {allMonitored ? "🔖" : "◻"}
                  </button>
                  {archiveFile ? (
                    <h4
                      className="link-text"
                      title="Show volume filename on disk"
                      onClick={(e) => {
                        e.stopPropagation();
                        toggleReveal(`v${key}`);
                      }}
                    >
                      Volume {volume}
                    </h4>
                  ) : (
                    <h4>{volume === null ? "Issues without volume" : `Volume ${volume}`}</h4>
                  )}
                  <span
                    className={`pill ${downloaded === issues.length ? "green" : "gray"}`}
                  >
                    {downloaded} / {issues.length}
                  </span>
                </div>
                {revealed[`v${key}`] && archiveFile && (
                  <div className="filepath volume-filepath">{archiveFile}</div>
                )}
                {!isCollapsed && issueRows(issues)}
              </div>
            );
          })
        )}
      </div>

      {search && (
        <InteractiveSearch
          seriesId={seriesId}
          issueId={search.issueId}
          title={search.title}
          onClose={() => setSearch(null)}
        />
      )}
      {showRename && (
        <RenameModal seriesId={seriesId} onClose={() => setShowRename(false)} onDone={invalidate} />
      )}
      {showFiles && (
        <FilesModal
          seriesId={seriesId}
          issues={series.issues}
          onClose={() => setShowFiles(false)}
          onChanged={invalidate}
        />
      )}
      {showSources && (
        <SourcesModal
          seriesId={seriesId}
          links={series.source_links}
          onClose={() => setShowSources(false)}
          onChanged={invalidate}
        />
      )}
      {showCleanup && (
        <CleanupModal
          seriesId={seriesId}
          onClose={() => setShowCleanup(false)}
          onDone={invalidate}
        />
      )}
    </>
  );
}
