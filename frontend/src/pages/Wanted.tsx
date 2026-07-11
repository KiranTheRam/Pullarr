import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { WantedItem } from "../api/types";
import { issueLabel, EmptyState, QueryError, Spinner, Toolbar } from "../components/common";

export default function Wanted() {
  const [scope, setScope] = useState<"automatic" | "missing" | "future">("automatic");
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["wanted", scope],
    queryFn: () => api.get<WantedItem[]>(`/wanted?scope=${scope}`),
    refetchInterval: 15000,
  });

  return (
    <>
      <Toolbar title="Wanted">
        <button className={`btn${scope === "automatic" ? " primary" : ""}`} onClick={() => setScope("automatic")}>Automatic</button>
        <button className={`btn${scope === "missing" ? " primary" : ""}`} onClick={() => setScope("missing")}>All missing</button>
        <button className={`btn${scope === "future" ? " primary" : ""}`} onClick={() => setScope("future")}>Upcoming</button>
      </Toolbar>
      <div className="content">
        {isLoading ? (
          <Spinner />
        ) : isError ? (
          <QueryError error={error} retry={() => refetch()} />
        ) : !data || data.length === 0 ? (
          <EmptyState
            icon="✔"
            title={scope === "automatic" ? "Nothing queued for automation" : scope === "future" ? "No upcoming monitored issues" : "No missing issues"}
            hint={scope === "automatic" ? "All released, monitored issues in monitored series are downloaded." : undefined}
          />
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ width: 280 }}>Series</th>
                <th style={{ width: 140 }}>Issue</th>
                <th>Title</th>
                <th style={{ width: 150 }}>{scope === "future" ? "Release" : "Monitoring"}</th>
              </tr>
            </thead>
            <tbody>
              {data.map((w) => (
                <tr key={w.issue_id}>
                  <td>
                    <Link to={`/series/${w.series_id}`} style={{ color: "var(--info)" }}>
                      {w.series_title}
                    </Link>
                  </td>
                  <td>{issueLabel(w.number, w.volume, w.display_number)}</td>
                  <td style={{ color: "var(--text-dim)" }}>{w.title || "—"}</td>
                  <td>
                    {scope === "future" ? (
                      w.released_at ? new Date(w.released_at).toLocaleDateString() : "Unknown"
                    ) : (
                      <span className={`pill ${w.series_monitored && w.issue_monitored ? "blue" : "gray"}`}>
                        {w.series_monitored && w.issue_monitored ? "Automatic" : "Unmonitored"}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
