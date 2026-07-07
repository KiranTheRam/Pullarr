import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { WantedItem } from "../api/types";
import { issueLabel, EmptyState, Spinner, Toolbar } from "../components/common";

export default function Wanted() {
  const { data, isLoading } = useQuery({
    queryKey: ["wanted"],
    queryFn: () => api.get<WantedItem[]>("/wanted"),
    refetchInterval: 15000,
  });

  return (
    <>
      <Toolbar title="Wanted" />
      <div className="content">
        {isLoading ? (
          <Spinner />
        ) : !data || data.length === 0 ? (
          <EmptyState
            icon="✔"
            title="Nothing wanted"
            hint="All monitored issues are downloaded."
          />
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ width: 280 }}>Series</th>
                <th style={{ width: 140 }}>Issue</th>
                <th>Title</th>
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
                  <td>{issueLabel(w.number, w.volume)}</td>
                  <td style={{ color: "var(--text-dim)" }}>{w.title || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
