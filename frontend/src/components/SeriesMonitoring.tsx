import { useEffect, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api/client";
import type { MonitorMode, SeriesDetail } from "../api/types";

export default function SeriesMonitoring({ series, onChanged }: { series: SeriesDetail; onChanged: () => void }) {
  const [mode, setMode] = useState<MonitorMode>(series.monitor_mode);
  const [from, setFrom] = useState(series.monitor_from?.toString() ?? "");
  useEffect(() => {
    setMode(series.monitor_mode);
    setFrom(series.monitor_from?.toString() ?? "");
  }, [series.monitor_mode, series.monitor_from]);
  const save = useMutation({
    mutationFn: () => api.put(`/series/${series.id}`, {
      monitor_mode: mode,
      monitor_from: mode === "from_issue" ? Number(from) : null,
    }),
    onSuccess: onChanged,
  });
  const dirty = mode !== series.monitor_mode || (mode === "from_issue" && Number(from) !== series.monitor_from);
  return (
    <div className="series-monitoring">
      <label htmlFor="series-monitor-mode">Automatic grabs</label>
      <select id="series-monitor-mode" value={mode} onChange={(e) => setMode(e.target.value as MonitorMode)}>
        <option value="all">All released missing issues</option>
        <option value="future">Future issues only</option>
        <option value="from_issue">From issue number</option>
      </select>
      {mode === "from_issue" && <input aria-label="Starting issue number" type="number" min="0.01" step="any" value={from} onChange={(e) => setFrom(e.target.value)} placeholder="Issue #" />}
      <button className="btn sm" disabled={!dirty || save.isPending || (mode === "from_issue" && !(Number(from) > 0))} onClick={() => save.mutate()}>Apply</button>
      {save.isError && <span className="error-banner" role="alert">{(save.error as Error).message}</span>}
      <span className="muted">Applying a mode resets individual issue monitoring choices. Later refreshes keep your changes.</span>
    </div>
  );
}
