import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { HistoryItem, JobItem, QueueItem } from "../api/types";
import { EmptyState, QueryError, Spinner, statusPill, Toolbar } from "../components/common";

const PAGE_SIZE = 50;

function PageControls({ offset, next, pending, setOffset }: {
  offset: number; next: boolean; pending: boolean; setOffset: (offset: number) => void;
}) {
  return <div className="table-actions" style={{ display: "flex", gap: 12, marginBlock: 12 }}>
    <button className="btn" disabled={offset === 0 || pending} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
    <span>Page {Math.floor(offset / PAGE_SIZE) + 1}</span>
    <button className="btn" disabled={!next || pending} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
  </div>;
}

function RetryStatus({ item }: { item: QueueItem }) {
  const seconds = item.next_retry_at ? Math.max(0, Math.ceil((Date.parse(item.next_retry_at) - Date.now()) / 1000)) : null;
  return <div className="filepath">
    {item.attempt_count > 0 && `Attempt ${item.attempt_count}`}
    {seconds !== null && ` · ${seconds > 0 ? `Retry in ${Math.ceil(seconds / 60)}m` : "Retry ready"}`}
  </div>;
}

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

  const resolve = useMutation({
    mutationFn: (id: number) => api.post(`/queue/${id}/resolve`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["queue"] });
      queryClient.invalidateQueries({ queryKey: ["history"] });
    },
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
      {(remove.isError || removeSelected.isError || resolve.isError) && <div className="error-banner" role="alert">{String(remove.error || removeSelected.error || resolve.error)}</div>}
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
                aria-label="Select all downloads"
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
                  aria-label={`Select ${item.title || item.series_title}`}
                  checked={selected.has(item.id)}
                  onChange={() => toggle(item.id)}
                />
              </td>
              <td>
                {item.series_id ? <Link to={`/series/${item.series_id}`}>{item.title || item.series_title}</Link> : item.title || item.series_title}
                {item.status === "needs_attention" && <div className="filepath">Map the downloaded files, then mark this item resolved.</div>}
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
                <RetryStatus item={item} />
                <div className="progress-bar">
                  <div style={{ width: `${Math.round(item.progress * 100)}%` }} />
                  <span>{Math.round(item.progress * 100)}%</span>
                </div>
              </td>
              <td>
                <button
                  className="btn icon-btn"
                  title="Remove"
                  aria-label={`Remove ${item.title || item.series_title}`}
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(item.id)}
                >
                  X
                </button>
                {item.status === "needs_attention" && <>
                  {item.series_id && <Link className="btn sm" to={`/series/${item.series_id}?files=1`}>Map files</Link>}
                  <button className="btn sm" disabled={resolve.isPending} onClick={() => resolve.mutate(item.id)}>Mark resolved</button>
                </>}
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
  const [offset, setOffset] = useState(0);
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["history", eventFilter, offset],
    queryFn: () => api.get<HistoryItem[]>(`/history?limit=${PAGE_SIZE + 1}&offset=${offset}${eventFilter ? `&event=${eventFilter}` : ""}`),
    refetchInterval: 5000,
  });
  const items = data?.slice(0, PAGE_SIZE) ?? [];
  return <>
    <div className="table-actions issue-filters">
      {["", "failed", "retrying", "imported", "needs_attention", "resolved"].map((value) => (
        <button key={value || "all"} className={`btn sm${eventFilter === value ? " primary" : ""}`} onClick={() => { setEventFilter(value); setOffset(0); }}>{value ? value.replaceAll("_", " ") : "all"}</button>
      ))}
    </div>
    {isLoading ? <Spinner /> : isError ? <QueryError error={error} retry={() => refetch()} /> : !items.length ? <>
      <EmptyState icon="🕘" title={eventFilter ? "No events match this filter" : offset ? "No more history" : "No history yet"} />
      {eventFilter && <button className="btn" onClick={() => { setEventFilter(""); setOffset(0); }}>Clear filter</button>}
    </> : <table className="data-table">
      <thead><tr><th>Event</th><th>Series</th><th>Detail</th><th>Source</th><th>Date</th></tr></thead>
      <tbody>{items.map((ev) => <tr key={ev.id}>
        <td><span className={`pill ${statusPill[ev.event] ?? "gray"}`}>{ev.event}</span></td>
        <td>{ev.series_id ? <Link to={`/series/${ev.series_id}`}>{ev.series_title}</Link> : ev.series_title}</td>
        <td style={{ color: "var(--text-dim)", overflowWrap: "anywhere" }}>{ev.detail}</td>
        <td>{ev.source_name}</td><td>{new Date(ev.created_at).toLocaleString()}</td>
      </tr>)}</tbody>
    </table>}
    <PageControls offset={offset} next={(data?.length ?? 0) > PAGE_SIZE} pending={isFetching} setOffset={setOffset} />
  </>;
}

function FailedDownloads() {
  const queryClient = useQueryClient();
  const [offset, setOffset] = useState(0);
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["queue", "failed", offset],
    queryFn: () => api.get<QueueItem[]>(`/queue/failed?limit=${PAGE_SIZE + 1}&offset=${offset}`),
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
  if (!data?.length) return <><EmptyState icon="✔" title={offset ? "No more failed downloads" : "No failed downloads"} /><PageControls offset={offset} next={false} pending={false} setOffset={setOffset} /></>;
  return (
    <>{(retry.isError || block.isError) && <div className="error-banner">{String((retry.error || block.error) as Error)}</div>}<table className="data-table">
      <thead><tr><th>Release</th><th>Failure</th><th>Attempts</th><th>Date</th><th></th></tr></thead>
      <tbody>{data.slice(0, PAGE_SIZE).map((item) => (
        <tr key={item.id}>
          <td>{item.title || item.series_title}<div className="filepath">{item.source_name}</div></td>
          <td><span className="pill red">{item.error_code || "failed"}</span> {item.error}</td>
          <td>{item.attempt_count}</td>
          <td>{new Date(item.created_at).toLocaleString()}</td>
          <td style={{ whiteSpace: "nowrap" }}>
            <button className="btn sm" disabled={retry.isPending} title="Retry download" aria-label={`Retry ${item.title || item.series_title}`} onClick={() => retry.mutate(item.id)}>Retry</button>{" "}
            <button className="btn sm" disabled={item.blocked || block.isPending} onClick={() => block.mutate(item.id)} title="Exclude this release only; other releases remain eligible">{item.blocked ? "Release blocked" : "Block release"}</button>
          </td>
        </tr>
      ))}</tbody>
    </table><PageControls offset={offset} next={data.length > PAGE_SIZE} pending={retry.isPending || block.isPending} setOffset={setOffset} /></>
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
