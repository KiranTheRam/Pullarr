import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Series } from "../api/types";
import { EmptyState, QueryError, Spinner, Toolbar } from "../components/common";

function PosterCard({ series }: { series: Series }) {
  const navigate = useNavigate();
  const pct =
    series.issue_count > 0 ? (series.downloaded_count / series.issue_count) * 100 : 0;
  return (
    <div
      className="poster-card"
      role="link"
      tabIndex={0}
      aria-label={`${series.title}, ${series.downloaded_count} of ${series.issue_count} issues`}
      onClick={() => navigate(`/series/${series.id}`)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") navigate(`/series/${series.id}`);
      }}
    >
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
  const [filter, setFilter] = useState<"all" | "incomplete" | "monitored" | "unmonitored">("all");
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["series"],
    queryFn: () => api.get<Series[]>("/series"),
  });
  const visible = useMemo(() => (data ?? []).filter((series) => {
    if (search && !series.title.toLowerCase().includes(search.toLowerCase())) return false;
    if (filter === "incomplete") return series.downloaded_count < series.issue_count;
    if (filter === "monitored") return series.monitored;
    if (filter === "unmonitored") return !series.monitored;
    return true;
  }), [data, filter, search]);

  return (
    <>
      <Toolbar title="Library">
        <Link to="/add" className="btn primary">
          + Add Series
        </Link>
      </Toolbar>
      <div className="content">
        <div className="table-actions library-filters">
          <input aria-label="Search library" placeholder="Search library…" value={search} onChange={(e) => setSearch(e.target.value)} />
          <select aria-label="Filter library" value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)}>
            <option value="all">All series</option>
            <option value="incomplete">Incomplete</option>
            <option value="monitored">Monitored</option>
            <option value="unmonitored">Unmonitored</option>
          </select>
        </div>
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
        ) : (
          <div className="poster-grid">
            {visible.map((s) => (
              <PosterCard key={s.id} series={s} />
            ))}
            {visible.length === 0 && <EmptyState icon="🔍" title="No series match these filters" />}
          </div>
        )}
      </div>
    </>
  );
}
