import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { ImportFolder, ImportResult, MetadataResult, MonitorMode, RootFolder } from "../api/types";
import { EmptyState, QueryError, Spinner, Toolbar } from "../components/common";

function FolderMatch({ folder, selected, onChoose }: { folder: ImportFolder; selected?: MetadataResult; onChoose: (value: MetadataResult | null) => void }) {
  const [query, setQuery] = useState(folder.query);
  const [submitted, setSubmitted] = useState("");
  const matches = useQuery({
    queryKey: ["import-match", submitted],
    queryFn: () => api.get<MetadataResult[]>(`/search/metadata?q=${encodeURIComponent(submitted)}`),
    enabled: submitted.length > 1,
  });
  return <div className="import-folder">
    <div><strong>{folder.folder_name}</strong> <span className="muted">{folder.file_count} files</span></div>
    <form onSubmit={(event) => { event.preventDefault(); setSubmitted(query.trim()); }} className="editor-actions">
      <input aria-label={`Search match for ${folder.folder_name}`} value={query} onChange={(event) => setQuery(event.target.value)} />
      <button className="btn sm" type="submit" disabled={query.trim().length < 2}>Find match</button>
      {selected && <button className="btn sm" type="button" onClick={() => onChoose(null)}>Clear match</button>}
    </form>
    {selected && <div className="pill green">Matched: {selected.title} {selected.year ? `(${selected.year})` : ""} · {selected.publisher}</div>}
    {matches.isFetching && <span className="muted">Searching…</span>}
    {matches.isError && <div className="error-banner">{(matches.error as Error).message}</div>}
    {matches.data && !selected && <div className="import-matches">
      {matches.data.length === 0 && <span className="muted">No ComicVine matches. Try another title.</span>}
      {matches.data.map((candidate) => <button key={candidate.provider_id} className="import-match" disabled={candidate.in_library} onClick={() => onChoose(candidate)}>
        <strong>{candidate.title} {candidate.year ? `(${candidate.year})` : ""}</strong>
        <span className="muted">{candidate.publisher} · {candidate.total_issues ?? "?"} issues</span>
        {candidate.in_library && <span className="pill green">Already in library</span>}
      </button>)}
    </div>}
  </div>;
}

export default function LibraryImport() {
  const queryClient = useQueryClient();
  const roots = useQuery({ queryKey: ["rootfolders"], queryFn: () => api.get<RootFolder[]>("/rootfolders") });
  const [root, setRoot] = useState<number | null>(null);
  const [selected, setSelected] = useState<Record<string, MetadataResult>>({});
  const [monitor, setMonitor] = useState(true);
  const [mode, setMode] = useState<MonitorMode>("all");
  const [from, setFrom] = useState("");
  const [searchNow, setSearchNow] = useState(false);
  const [result, setResult] = useState<ImportResult[] | null>(null);
  useEffect(() => { if (root === null && roots.data?.length) setRoot(roots.data[0].id); }, [root, roots.data]);
  const folders = useQuery({
    queryKey: ["import-folders", root],
    queryFn: () => api.get<ImportFolder[]>(`/library/import/folders?root_folder_id=${root}`),
    enabled: root !== null,
  });
  const chosen = (folders.data ?? []).filter((folder) => selected[folder.folder_name]);
  const importBatch = useMutation({
    mutationFn: () => api.post<ImportResult[]>("/library/import", {
      root_folder_id: root,
      items: chosen.map((folder) => ({ folder_name: folder.folder_name, comicvine_id: Number(selected[folder.folder_name].provider_id) })),
      monitored: monitor, monitor_mode: mode,
      monitor_from: mode === "from_issue" ? Number(from) : null, search_now: searchNow,
    }),
    onSuccess: (value) => {
      setResult(value);
      setSelected({});
      queryClient.invalidateQueries({ queryKey: ["series"] });
      queryClient.invalidateQueries({ queryKey: ["import-folders", root] });
    },
  });
  return <>
    <Toolbar title="Import Existing Library"><Link className="btn" to="/">Back to Library</Link></Toolbar>
    <div className="content">
      <p className="section-hint">Find folders Pullarr does not yet track, match each to the right ComicVine series, then confirm the batch. Files stay where they are.</p>
      <div className="form-row"><label>Root folder</label><select value={root ?? ""} onChange={(event) => { setRoot(Number(event.target.value)); setSelected({}); setResult(null); }}>
        {roots.data?.map((folder) => <option key={folder.id} value={folder.id}>{folder.path}</option>)}
      </select></div>
      {roots.isError && <QueryError error={roots.error} retry={() => roots.refetch()} />}
      {folders.isLoading && <Spinner />}
      {folders.isError && <QueryError error={folders.error} retry={() => folders.refetch()} />}
      {folders.data?.length === 0 && <EmptyState icon="📂" title="No untracked folders found" hint="Check another root folder or add a series manually." />}
      {folders.data?.map((folder) => <FolderMatch key={folder.path} folder={folder} selected={selected[folder.folder_name]} onChoose={(value) => setSelected((current) => {
        const next = { ...current };
        if (value) next[folder.folder_name] = value; else delete next[folder.folder_name];
        return next;
      })} />)}
      {chosen.length > 0 && <div className="library-editor">
        <strong>Import {chosen.length} confirmed series</strong>
        {chosen.length > 200 && <div className="error-banner">Import up to 200 series per batch. Clear some matches to continue.</div>}
        <div className="editor-fields">
          <label className="editor-checkbox"><input type="checkbox" checked={monitor} onChange={(event) => setMonitor(event.target.checked)} /> Monitor</label>
          <label>Automatic grabs<select value={mode} onChange={(event) => setMode(event.target.value as MonitorMode)}>
            <option value="all">All released missing issues</option><option value="future">Future issues only</option><option value="from_issue">From issue number</option>
          </select></label>
          {mode === "from_issue" && <label>Starting issue<input type="number" min="0.01" step="any" value={from} onChange={(event) => setFrom(event.target.value)} /></label>}
          <label className="editor-checkbox"><input type="checkbox" checked={searchNow} onChange={(event) => setSearchNow(event.target.checked)} /> Search missing now</label>
        </div>
        <button className="btn primary" disabled={importBatch.isPending || chosen.length > 200 || (mode === "from_issue" && !(Number(from) > 0))} onClick={() => {
          if (window.confirm(`Add ${chosen.length} matched series to Pullarr? Existing files will be scanned in place.`)) importBatch.mutate();
        }}>Import selected</button>
      </div>}
      {importBatch.isError && <div className="error-banner" role="alert">{(importBatch.error as Error).message}</div>}
      {result && <div className="settings-section"><h3>Import results</h3>{result.map((item) => <div key={item.folder_name}>{item.folder_name}: {item.status}{item.detail ? ` — ${item.detail}` : ""}</div>)}</div>}
    </div>
  </>;
}
