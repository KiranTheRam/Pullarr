import { Icon, type IconName } from "./Icon";
import { NavLink } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, appVersion } from "../api/client";
import type { QueueItem } from "../api/types";

const items = [
  { to: "/", label: "Library", icon: "library" },
  { to: "/add", label: "Add New", icon: "add" },
  { to: "/calendar", label: "Calendar", icon: "calendar" },
  { to: "/activity", label: "Activity", icon: "activity" },
  { to: "/wanted", label: "Wanted", icon: "wanted" },
  { to: "/settings", label: "Settings", icon: "settings" },
] satisfies { to: string; label: string; icon: IconName }[];

export default function Sidebar({ onGoTo }: { onGoTo: () => void }) {
  const { data: queue } = useQuery({
    queryKey: ["queue"],
    queryFn: () => api.get<QueueItem[]>("/queue"),
    refetchInterval: 5000,
  });

  return (
    <div className="sidebar">
      <div className="sidebar-logo">
        <img className="logo-mark" src="/pullarr-icon.svg" alt="" /> pullarr
      </div>
      <nav>
        <button className="nav-item go-to" onClick={onGoTo}><span className="icon"><Icon name="search" /></span> Go to… <span className="shortcut">⌘K</span></button>
        {items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
          >
            <span className="icon"><Icon name={item.icon} /></span>
            {item.label}
            {item.to === "/activity" && queue && queue.length > 0 && (
              <span className="nav-badge">{queue.length}</span>
            )}
          </NavLink>
        ))}
      </nav>
      <div className="sidebar-footer">v{appVersion()}</div>
    </div>
  );
}
