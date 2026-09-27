import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Series } from "../api/types";
import { EmptyState, QueryError, Spinner, Toolbar } from "../components/common";
import LibraryEditor from "../components/LibraryEditor";

interface Filters {
  monitored: "all" | "monitored" | "unmonitored";
  status: "all" | "ongoing" | "finished";
  content: "all" | "missing" | "complete";
}

const NO_FILTERS: Filters = { monitored: "all", status: "all", content: "all" };
const FILTERS_KEY = "library-filters";

function loadFilters(): Filters {
  try {
    return { ...NO_FILTERS, ...JSON.parse(localStorage.getItem(FILTERS_KEY) ?? "{}") };
  } catch {
    return NO_FILTERS;
  }
}

function matchesFilters(series: Series, f: Filters): boolean {
  if (f.monitored !== "all" && series.monitored !== (f.monitored === "monitored")) return false;
  // finished/cancelled mean no more issues are coming; everything else
  // (releasing, hiatus, not yet released, unknown) counts as ongoing
  const finished = series.status === "finished" || series.status === "cancelled";
  if (f.status !== "all" && finished !== (f.status === "finished")) return false;
  const missing = series.downloaded_count < series.issue_count;
  if (f.content !== "all" && missing !== (f.content === "missing")) return false;
  return true;
}

function PosterCard({ series, selecting, selected, onSelect }: { series: Series; selecting: boolean; selected: boolean; onSelect: (event: React.MouseEvent | React.KeyboardEvent) => void }) {
  const navigate = useNavigate();
  const pct =
    series.issue_count > 0 ? (series.downloaded_count / series.issue_count) * 100 : 0;
  return (
    <div
      className="poster-card"
      role={selecting ? "checkbox" : "link"}
      aria-checked={selecting ? selected : undefined}
      tabIndex={0}
      aria-label={`${series.title}, ${series.downloaded_count} of ${series.issue_count} issues`}
      onClick={(event) => selecting ? onSelect(event) : navigate(`/series/${series.id}`)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          if (selecting) onSelect(event);
          else navigate(`/series/${series.id}`);
        }
      }}
    >
      {selecting && <span className={`poster-select${selected ? " checked" : ""}`}>{selected ? "✓" : ""}</span>}
      {series.cover_url ? (
        <img src={series.cover_url} alt={series.title} loading="lazy" />
      ) : (
        <div className="no-cover">{series.title}</div>
      )}
      <div className={`poster-ribbon${series.monitored ? "" : " unmonitored"}`} />
      <div className="poster-label">
        {series.title}
        <div style={{ fontSize: 11, color: "#bbb", marginTop: 2 }}>
          {series.downloaded_count} / {series.issue_count || "?"}
        </div>
      </div>
      <div className="poster-progress">
        <div className={pct < 100 ? "partial" : ""} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

export default function Library() {
  const [search, setSearch] = useState("");
  const [filters, setFilters] = useState<Filters>(loadFilters);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [lastSelected, setLastSelected] = useState<number | null>(null);
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["series"],
    queryFn: () => api.get<Series[]>("/series"),
  });

  const setFilter = (key: keyof Filters) => (e: React.ChangeEvent<HTMLSelectElement>) => {
    const next = { ...filters, [key]: e.target.value };
    setFilters(next);
    localStorage.setItem(FILTERS_KEY, JSON.stringify(next));
  };

  const query = search.trim().toLowerCase();
  const visible = useMemo(() => (data ?? []).filter((series) => {
    if (query && !series.title.toLowerCase().includes(query)) return false;
    return matchesFilters(series, filters);
  }), [data, filters, query]);
  const filtering =
    Boolean(query) || (Object.keys(NO_FILTERS) as (keyof Filters)[]).some((k) => filters[k] !== "all");
  const visibleIds = visible.map((series) => series.id);
  const selectedVisible = visibleIds.filter((id) => selected.has(id));
  const selectAt = (index: number, shift: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (shift && lastSelected !== null) {
        for (const series of visible.slice(Math.min(index, lastSelected), Math.max(index, lastSelected) + 1)) next.add(series.id);
      } else if (next.has(visible[index].id)) next.delete(visible[index].id);
      else next.add(visible[index].id);
      return next;
    });
    setLastSelected(index);
  };

  return (
    <>
      <Toolbar title="Library">
        <button className="btn" onClick={() => { setSelecting(!selecting); setSelected(new Set()); setLastSelected(null); }}>{selecting ? "Done" : "Select"}</button>
        <Link to="/library/import" className="btn">Import folders</Link>
        <Link to="/add" className="btn primary">
          + Add Series
        </Link>
      </Toolbar>
      <div className="content">
        <div className="table-actions library-filters">
          <input aria-label="Search library" placeholder="Search library…" value={search} onChange={(e) => setSearch(e.target.value)} />
          <select aria-label="Filter by monitoring" value={filters.monitored} onChange={setFilter("monitored")}>
            <option value="all">All series</option>
            <option value="monitored">Monitored</option>
            <option value="unmonitored">Unmonitored</option>
          </select>
          <select aria-label="Filter by status" value={filters.status} onChange={setFilter("status")}>
            <option value="all">Any status</option>
            <option value="ongoing">Ongoing</option>
            <option value="finished">Finished</option>
          </select>
          <select aria-label="Filter by content" value={filters.content} onChange={setFilter("content")}>
            <option value="all">Any content</option>
            <option value="missing">Missing issues</option>
            <option value="complete">All downloaded</option>
          </select>
          {filtering && data && (
            <span style={{ fontSize: 12, color: "#999" }}>
              {visible.length} of {data.length}
            </span>
          )}
        </div>
        {selecting && <div className="editor-actions">
          <button className="btn sm" onClick={() => setSelected(new Set(visibleIds))}>Select all matching ({visibleIds.length})</button>
          <button className="btn sm" onClick={() => setSelected(new Set())}>Clear</button>
          <span className="muted">Only selected series matching the current filters are edited.</span>
        </div>}
        {selecting && selectedVisible.length > 0 && <LibraryEditor ids={selectedVisible} onDone={() => setSelected(new Set())} />}
        {isLoading ? (
          <Spinner />
        ) : isError ? (
          <QueryError error={error} retry={() => refetch()} />
        ) : !data || data.length === 0 ? (
          <EmptyState
            icon="📚"
            title="Your library is empty"
            hint="Add a series to start building your comic collection."
          />
        ) : visible.length === 0 ? (
          <EmptyState
            icon="🔍"
            title="No matches"
            hint={
              query
                ? `Nothing in your library matches “${search.trim()}”.`
                : "No series match the current filters."
            }
          />
        ) : (
          <div className="poster-grid">
            {visible.map((s, index) => (
              <PosterCard key={s.id} series={s} selecting={selecting} selected={selected.has(s.id)} onSelect={(event) => selectAt(index, event.shiftKey)} />
            ))}
          </div>
        )}
      </div>
    </>
  );
}
