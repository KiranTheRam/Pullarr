import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { CalendarIssue } from "../api/types";
import { EmptyState, QueryError, Spinner, Toolbar } from "../components/common";

export default function Calendar() {
  const [month, setMonth] = useState(() => new Date(new Date().getFullYear(), new Date().getMonth(), 1));
  const start = new Date(Date.UTC(month.getFullYear(), month.getMonth(), 1));
  const end = new Date(Date.UTC(month.getFullYear(), month.getMonth() + 1, 1));
  const releases = useQuery({
    queryKey: ["calendar", start.toISOString()],
    queryFn: () => api.get<CalendarIssue[]>(`/calendar?start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}`),
  });
  const byDay = new Map<number, CalendarIssue[]>();
  for (const issue of releases.data ?? []) {
    const day = new Date(issue.released_at).getUTCDate();
    byDay.set(day, [...(byDay.get(day) ?? []), issue]);
  }
  const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
  const offset = start.getUTCDay();
  return <>
    <Toolbar title="Release Calendar">
      <button className="btn" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}>←</button>
      <strong>{month.toLocaleString(undefined, { month: "long", year: "numeric" })}</strong>
      <button className="btn" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}>→</button>
    </Toolbar>
    <div className="content">
      <p className="section-hint">Known ComicVine issue dates for series in your library. Dates can change as metadata is updated.</p>
      {releases.isLoading && <Spinner />}
      {releases.isError && <QueryError error={releases.error} retry={() => releases.refetch()} />}
      {releases.data?.length === 0 && <EmptyState icon="🗓" title="No dated issues this month" hint="Try another month or refresh a series." />}
      {releases.data && <div className="calendar-grid">
        {["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((day) => <div className="calendar-weekday" key={day}>{day}</div>)}
        {Array.from({ length: offset }, (_, index) => <div key={`blank-${index}`} className="calendar-day empty" />)}
        {Array.from({ length: days }, (_, index) => <div key={index} className="calendar-day">
          <strong>{index + 1}</strong>
          {(byDay.get(index + 1) ?? []).map((issue) => <Link key={issue.issue_id} to={`/series/${issue.series_id}`} className={`calendar-issue${issue.downloaded ? " downloaded" : ""}`} title={issue.issue_title}>
            {issue.series_title} #{issue.issue_number}
          </Link>)}
        </div>)}
      </div>}
    </div>
  </>;
}
