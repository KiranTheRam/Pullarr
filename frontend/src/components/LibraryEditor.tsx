import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { MonitorMode, RootFolder } from "../api/types";

interface EditResult {
  updated?: number;
  moved?: number;
  removed?: number;
  count?: number;
  problems?: { series_id: number; title?: string; detail: string }[];
}

async function inBatches(ids: number[], run: (batch: number[]) => Promise<EditResult>): Promise<EditResult> {
  const combined: EditResult = { problems: [] };
  for (let index = 0; index < ids.length; index += 200) {
    const part = await run(ids.slice(index, index + 200));
    for (const field of ["updated", "moved", "removed", "count"] as const) {
      if (part[field] !== undefined) combined[field] = (combined[field] ?? 0) + part[field];
    }
    combined.problems?.push(...(part.problems ?? []));
  }
  return combined;
}

export default function LibraryEditor({ ids, onDone }: { ids: number[]; onDone: () => void }) {
  const queryClient = useQueryClient();
  const [monitor, setMonitor] = useState("keep");
  const [mode, setMode] = useState<"keep" | MonitorMode>("keep");
  const [from, setFrom] = useState("");
  const [root, setRoot] = useState("");
  const [moveFiles, setMoveFiles] = useState(false);
  const [result, setResult] = useState<EditResult | null>(null);
  const roots = useQuery({ queryKey: ["rootfolders"], queryFn: () => api.get<RootFolder[]>("/rootfolders") });
  const edit = useMutation({
    mutationFn: () => inBatches(ids, (batch) => api.put<EditResult>("/series/editor", {
      series_ids: batch, monitored: monitor === "keep" ? null : monitor === "on",
      monitor_mode: mode === "keep" ? null : mode,
      monitor_from: mode === "from_issue" ? Number(from) : null,
      root_folder_id: root ? Number(root) : null, move_files: moveFiles,
    })),
    onSuccess: (value) => { setResult(value); queryClient.invalidateQueries({ queryKey: ["series"] }); },
  });
  const refresh = useMutation({
    mutationFn: (search_missing: boolean) => inBatches(ids, (batch) => api.post<EditResult>("/series/editor/refresh", { series_ids: batch, search_missing })),
    onSuccess: setResult,
  });
  const remove = useMutation({
    mutationFn: () => inBatches(ids, (batch) => api.post<EditResult>("/series/editor/delete", { series_ids: batch })),
    onSuccess: (value) => { setResult(value); queryClient.invalidateQueries({ queryKey: ["series"] }); if (!value.problems?.length) onDone(); },
  });
  const error = edit.error || refresh.error || remove.error;
  const busy = edit.isPending || refresh.isPending || remove.isPending;
  return (
    <div className="library-editor">
      <strong>{ids.length} selected</strong>
      <div className="editor-fields">
        <label>Monitoring
          <select value={monitor} onChange={(e) => setMonitor(e.target.value)}>
            <option value="keep">Keep current</option><option value="on">On</option><option value="off">Off</option>
          </select>
        </label>
        <label>Automatic grabs
          <select value={mode} onChange={(e) => setMode(e.target.value as "keep" | MonitorMode)}>
            <option value="keep">Keep current</option><option value="all">All released missing issues</option>
            <option value="future">Future issues only</option><option value="from_issue">From issue number</option>
          </select>
        </label>
        {mode === "from_issue" && <label>Starting issue <input type="number" min="0.01" step="any" value={from} onChange={(e) => setFrom(e.target.value)} /></label>}
        <label>Root folder
          <select value={root} onChange={(e) => setRoot(e.target.value)}>
            <option value="">Keep current</option>
            {roots.data?.map((folder) => <option key={folder.id} value={folder.id}>{folder.path}</option>)}
          </select>
        </label>
        {root && <label className="editor-checkbox"><input type="checkbox" checked={moveFiles} onChange={(e) => setMoveFiles(e.target.checked)} /> Move existing files</label>}
      </div>
      <div className="editor-actions">
        <button className="btn primary" disabled={busy || (monitor === "keep" && mode === "keep" && !root) || (mode === "from_issue" && !(Number(from) > 0))} onClick={() => edit.mutate()}>Apply edits</button>
        <button className="btn" disabled={busy} onClick={() => refresh.mutate(false)}>Refresh</button>
        <button className="btn" disabled={busy} onClick={() => refresh.mutate(true)}>Search monitored missing</button>
        <button className="btn danger" disabled={busy} onClick={() => { if (window.confirm(`Remove ${ids.length} series from Pullarr? Files on disk are kept.`)) remove.mutate(); }}>Remove</button>
      </div>
      {result && <div className="muted">{result.updated != null ? `${result.updated} updated` : result.removed != null ? `${result.removed} removed` : `${result.count ?? 0} queued`}{result.moved ? `, ${result.moved} folders moved` : ""}</div>}
      {result?.problems?.map((item) => <div className="error-banner" key={item.series_id}>{item.title ?? `Series #${item.series_id}`}: {item.detail}</div>)}
      {error && <div className="error-banner" role="alert">{(error as Error).message}</div>}
    </div>
  );
}
