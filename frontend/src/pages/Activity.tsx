import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { HistoryItem, JobItem, QueueItem } from "../api/types";
import { EmptyState, QueryError, Spinner, statusPill, Toolbar } from "../components/common";

function Queue() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["queue"],
    queryFn: () => api.get<QueueItem[]>("/queue"),
    refetchInterval: 2000,
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/queue/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["queue"] }),
  });

  const removeSelected = useMutation({
    mutationFn: (ids: number[]) => api.post("/queue/remove", { ids }),
    onSuccess: () => {
      setSelected(new Set());
      queryClient.invalidateQueries({ queryKey: ["queue"] });
    },
  });

  if (isLoading) return <Spinner />;
  if (isError) return <QueryError error={error} retry={() => refetch()} />;
  if (!data || data.length === 0)
    return <EmptyState icon="⇅" title="Queue is empty" hint="Grabbed releases will appear here." />;

  const selectedVisible = data.filter((item) => selected.has(item.id)).map((item) => item.id);
  const allSelected = selectedVisible.length === data.length;

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <>
      <div className="table-actions" style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 10 }}>
        <button
          className="btn"
          onClick={() => setSelected(allSelected ? new Set() : new Set(data.map((i) => i.id)))}
        >
          {allSelected ? "Clear selected" : "Select all"}
        </button>
        <span>{selectedVisible.length} selected</span>
        <button
          className="btn danger"
          disabled={selectedVisible.length === 0 || removeSelected.isPending}
          onClick={() => removeSelected.mutate(selectedVisible)}
        >
          {removeSelected.isPending ? "Removing..." : "Remove selected"}
        </button>
      </div>
      <table className="data-table">
        <thead>
          <tr>
            <th style={{ width: 34 }}>
              <input
                type="checkbox"
                checked={allSelected}
                onChange={() => setSelected(allSelected ? new Set() : new Set(data.map((i) => i.id)))}
              />
            </th>
            <th>Title</th>
            <th style={{ width: 110 }}>Source</th>
            <th style={{ width: 90 }}>Type</th>
            <th style={{ width: 110 }}>Status</th>
            <th style={{ width: 180 }}>Progress</th>
            <th style={{ width: 60 }}></th>
          </tr>
        </thead>
        <tbody>
          {data.map((item) => (
            <tr key={item.id}>
              <td>
                <input
                  type="checkbox"
                  checked={selected.has(item.id)}
                  onChange={() => toggle(item.id)}
                />
              </td>
              <td>
                {item.title || item.series_title}
                {item.error && <div className="filepath">{item.error}</div>}
              </td>
              <td>{item.source_name}</td>
              <td>
                <span className={`pill ${item.kind === "torrent" ? "orange" : "blue"}`}>
                  {item.kind}
                </span>
              </td>
              <td>
                <span className={`pill ${statusPill[item.status] ?? "gray"}`}>{item.status}</span>
              </td>
              <td>
                <div className="progress-bar">
                  <div style={{ width: `${Math.round(item.progress * 100)}%` }} />
                  <span>{Math.round(item.progress * 100)}%</span>
                </div>
              </td>
              <td>
                <button
                  className="btn icon-btn"
                  title="Remove"
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(item.id)}
                >
                  X
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function History() {
  const [eventFilter, setEventFilter] = useState("");
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["history", eventFilter],
    queryFn: () => api.get<HistoryItem[]>(`/history${eventFilter ? `?event=${eventFilter}` : ""}`),
    refetchInterval: 5000,
  });

  if (isLoading) return <Spinner />;
  if (isError) return <QueryError error={error} retry={() => refetch()} />;
  if (!data || data.length === 0) return <EmptyState icon="🕘" title="No history yet" />;

  return (
    <><div className="table-actions issue-filters">
      {["", "failed", "retrying", "imported", "needs_attention"].map((value) => (
        <button key={value || "all"} className={`btn sm${eventFilter === value ? " primary" : ""}`} onClick={() => setEventFilter(value)}>{value ? value.replaceAll("_", " ") : "all"}</button>
      ))}
    </div><table className="data-table">
      <thead>
        <tr>
          <th style={{ width: 100 }}>Event</th>
          <th style={{ width: 220 }}>Series</th>
          <th>Detail</th>
          <th style={{ width: 110 }}>Source</th>
          <th style={{ width: 170 }}>Date</th>
        </tr>
      </thead>
      <tbody>
        {data.map((ev) => (
          <tr key={ev.id}>
            <td>
              <span className={`pill ${statusPill[ev.event] ?? "gray"}`}>{ev.event}</span>
            </td>
            <td>{ev.series_title}</td>
            <td style={{ color: "var(--text-dim)", wordBreak: "break-all" }}>{ev.detail}</td>
            <td>{ev.source_name}</td>
            <td style={{ color: "var(--text-dim)" }}>
              {new Date(ev.created_at).toLocaleString()}
            </td>
          </tr>
        ))}
      </tbody>
    </table></>
  );
}

function FailedDownloads() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["queue", "failed"],
    queryFn: () => api.get<QueueItem[]>("/queue/failed"),
    refetchInterval: 10000,
  });
  const retry = useMutation({
    mutationFn: (id: number) => api.post(`/queue/${id}/retry`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["queue"] });
      queryClient.invalidateQueries({ queryKey: ["queue", "failed"] });
    },
  });
  const block = useMutation({
    mutationFn: (id: number) => api.post(`/queue/${id}/block`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["queue", "failed"] }),
  });
  if (isLoading) return <Spinner />;
  if (isError) return <QueryError error={error} retry={() => refetch()} />;
  if (!data?.length) return <EmptyState icon="✔" title="No failed downloads" />;
  return (
    <>{(retry.isError || block.isError) && <div className="error-banner">{String((retry.error || block.error) as Error)}</div>}<table className="data-table">
      <thead><tr><th>Release</th><th>Failure</th><th>Attempts</th><th>Date</th><th></th></tr></thead>
      <tbody>{data.map((item) => (
        <tr key={item.id}>
          <td>{item.title || item.series_title}<div className="filepath">{item.source_name}</div></td>
          <td><span className="pill red">{item.error_code || "failed"}</span> {item.error}</td>
          <td>{item.attempt_count}</td>
          <td>{new Date(item.created_at).toLocaleString()}</td>
          <td style={{ whiteSpace: "nowrap" }}>
            <button className="btn sm" disabled={retry.isPending || item.kind !== "direct"} title={item.kind === "direct" ? "Retry download" : "Re-grab torrents from interactive search"} onClick={() => retry.mutate(item.id)}>Retry</button>{" "}
            <button className="btn sm" disabled={item.blocked || block.isPending} onClick={() => block.mutate(item.id)}>{item.blocked ? "Blocked" : "Block"}</button>
          </td>
        </tr>
      ))}</tbody>
    </table></>
  );
}

function Jobs() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.get<JobItem[]>("/jobs"),
    refetchInterval: 3000,
  });
  if (isLoading) return <Spinner />;
  if (isError) return <QueryError error={error} retry={() => refetch()} />;
  if (!data?.length) return <EmptyState icon="⚙" title="No background jobs yet" />;
  return (
    <table className="data-table">
      <thead><tr><th>Job</th><th>Series</th><th>Status</th><th>Phase</th><th>Progress</th><th>Detail</th></tr></thead>
      <tbody>{data.map((job) => (
        <tr key={job.id}>
          <td>{job.kind.replaceAll("_", " ")}</td><td>{job.series_title || "—"}</td>
          <td><span className={`pill ${statusPill[job.status] ?? "gray"}`}>{job.status}</span></td>
          <td>{job.phase}</td><td>{Math.round(job.progress * 100)}%</td>
          <td style={{ color: job.error ? "var(--danger)" : "var(--text-dim)" }}>{job.error || job.detail || "—"}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

export default function Activity() {
  const [tab, setTab] = useState<"queue" | "failed" | "jobs" | "history">("queue");
  return (
    <>
      <Toolbar title="Activity">
        <button className={`btn${tab === "queue" ? " primary" : ""}`} onClick={() => setTab("queue")}>
          Queue
        </button>
        <button
          className={`btn${tab === "failed" ? " primary" : ""}`}
          onClick={() => setTab("failed")}
        >
          Failed
        </button>
        <button
          className={`btn${tab === "jobs" ? " primary" : ""}`}
          onClick={() => setTab("jobs")}
        >
          Jobs
        </button>
        <button
          className={`btn${tab === "history" ? " primary" : ""}`}
          onClick={() => setTab("history")}
        >
          History
        </button>
      </Toolbar>
      <div className="content">
        {tab === "queue" ? <Queue /> : tab === "failed" ? <FailedDownloads /> : tab === "jobs" ? <Jobs /> : <History />}
      </div>
    </>
  );
}
