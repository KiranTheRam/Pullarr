import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import type { FilesystemList } from "../api/types";
import { Modal, Spinner } from "./common";

/** Sonarr-style folder picker: starts at configured root folders and can browse
 *  the whole container filesystem for mounted libraries or staging paths. */
export function FolderBrowser({
  onPick,
  onClose,
}: {
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const [path, setPath] = useState("");
  const [search, setSearch] = useState("");
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["filesystem", path],
    queryFn: () =>
      api.get<FilesystemList>(`/filesystem${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  });

  const query = search.trim().toLocaleLowerCase();
  const entries = data?.entries.filter((entry) =>
    entry.name.toLocaleLowerCase().includes(query),
  );
  const navigate = (nextPath: string) => {
    setPath(nextPath);
    setSearch("");
  };

  return (
    <Modal title="Choose a folder" onClose={onClose}>
      <div className="path-bar">
        <code>{data?.path || "Root folders"}</code>
        {data?.path && (
          <button className="btn primary sm" onClick={() => onPick(data.path)}>
            Use this folder
          </button>
        )}
      </div>
      <div className="form-row">
        <label htmlFor="folder-search">Search folders</label>
        <input
          id="folder-search"
          type="text"
          placeholder="Filter folders in this directory…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        {search && (
          <button className="btn sm" onClick={() => setSearch("")}>
            Clear search
          </button>
        )}
      </div>
      {isError && <div className="error-banner">{(error as Error).message}</div>}
      {isLoading ? (
        <Spinner />
      ) : (
        <ul className="fs-list">
          {data?.parent != null && (
            <li className="fs-row up" onClick={() => navigate(data.parent!)}>
              ⬑ ..
            </li>
          )}
          {data?.entries.length === 0 && (
            <li className="fs-row muted">No sub-folders</li>
          )}
          {!!data?.entries.length && entries?.length === 0 && (
            <li className="fs-row muted">No folders match your search</li>
          )}
          {entries?.map((e) => (
            <li key={e.path} className="fs-row" onClick={() => navigate(e.path)}>
              📁 {e.name}
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}
