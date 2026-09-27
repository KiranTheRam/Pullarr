import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Series } from "../api/types";

const pages = [
  { name: "Library", path: "/" }, { name: "Add New", path: "/add" },
  { name: "Import Folders", path: "/library/import" }, { name: "Calendar", path: "/calendar" },
  { name: "Wanted", path: "/wanted" }, { name: "Activity", path: "/activity" },
  { name: "Settings", path: "/settings" },
];

export default function CommandPalette({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const series = useQuery({ queryKey: ["series"], queryFn: () => api.get<Series[]>("/series") });
  const matches = useMemo(() => {
    const q = query.trim().toLocaleLowerCase();
    const pageItems = pages.filter((page) => page.name.toLocaleLowerCase().includes(q)).map((page) => ({ label: page.name, path: page.path }));
    const seriesItems = (series.data ?? []).filter((item) => `${item.title}\n${item.alt_titles}`.toLocaleLowerCase().includes(q)).slice(0, 15).map((item) => ({ label: `${item.title}${item.year ? ` (${item.year})` : ""}`, path: `/series/${item.id}` }));
    return [...pageItems, ...seriesItems, ...(q ? [{ label: `Search ComicVine for “${query.trim()}”`, path: `/add?q=${encodeURIComponent(query.trim())}` }] : [])];
  }, [query, series.data]);
  useEffect(() => input.current?.focus(), []);
  const choose = (path: string) => { navigate(path); onClose(); };
  return <div className="palette-backdrop" onMouseDown={onClose}>
    <div className="command-palette" role="dialog" aria-modal="true" aria-label="Go to" onMouseDown={(event) => event.stopPropagation()}>
      <input ref={input} aria-label="Search pages and series" placeholder="Go to a page or series…" value={query} onChange={(event) => { setQuery(event.target.value); setActive(0); }} onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
        if (event.key === "ArrowDown") { event.preventDefault(); setActive((value) => Math.min(value + 1, matches.length - 1)); }
        if (event.key === "ArrowUp") { event.preventDefault(); setActive((value) => Math.max(value - 1, 0)); }
        if (event.key === "Enter" && matches[active]) choose(matches[active].path);
      }} />
      <div className="palette-list">{matches.map((match, index) => <button key={match.path} className={index === active ? "active" : ""} onMouseEnter={() => setActive(index)} onClick={() => choose(match.path)}>{match.label}</button>)}</div>
    </div>
  </div>;
}
