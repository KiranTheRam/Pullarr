import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { RootFolder, Settings as SettingsType } from "../api/types";
import { FolderBrowser } from "../components/FolderBrowser";
import { Spinner, Toggle, Toolbar } from "../components/common";

function RootFolders() {
  const queryClient = useQueryClient();
  const [path, setPath] = useState("");
  const { data } = useQuery({
    queryKey: ["rootfolders"],
    queryFn: () => api.get<RootFolder[]>("/rootfolders"),
  });

  const add = useMutation({
    mutationFn: () => api.post("/rootfolders", { path }),
    onSuccess: () => {
      setPath("");
      queryClient.invalidateQueries({ queryKey: ["rootfolders"] });
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/rootfolders/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["rootfolders"] }),
  });

  return (
    <div className="settings-section">
      <h3>Root Folders</h3>
      <p className="section-hint">Library locations where series folders and CBZ files are created.</p>
      {data?.map((rf) => (
        <div className="form-row" key={rf.id}>
          <label style={{ width: "auto", flex: 1 }}>{rf.path}</label>
          <button className="btn icon-btn" onClick={() => remove.mutate(rf.id)}>
            ✕
          </button>
        </div>
      ))}
      {add.isError && <div className="error-banner">{(add.error as Error).message}</div>}
      <div className="form-row">
        <input
          type="text"
          placeholder="/data/comics"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          style={{ flex: 1, maxWidth: 380 }}
        />
        <button className="btn primary" disabled={!path || add.isPending} onClick={() => add.mutate()}>
          + Add
        </button>
      </div>
    </div>
  );
}

const SOURCE_LABELS: Record<string, string> = {
  getcomics: "GetComics",
};

const SOURCE_HINTS: Record<string, string> = {
  getcomics: "Main-server direct downloads, with Pixeldrain fallback when available.",
};

function SourcePriority({
  form,
  setForm,
}: {
  form: SettingsType;
  setForm: (f: SettingsType) => void;
}) {
  const known = Object.keys(form)
    .filter((k) => k.startsWith("source_") && k.endsWith("_enabled"))
    .map((k) => k.slice("source_".length, -"_enabled".length));
  const order = (form.source_priority ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter((s, i, arr) => s && known.includes(s) && arr.indexOf(s) === i);
  for (const source of known) if (!order.includes(source)) order.push(source);

  const move = (index: number, delta: number) => {
    const next = [...order];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    setForm({ ...form, source_priority: next.join(",") });
  };

  return (
    <div className="priority-list">
      {order.map((name, i) => {
        const enabled = form[`source_${name}_enabled`] === "true";
        return (
          <div className={`priority-row${enabled ? "" : " disabled"}`} key={name}>
            <span className="priority-rank">{i + 1}</span>
            <span className="priority-arrows">
              <button className="btn icon-btn" disabled={i === 0} onClick={() => move(i, -1)} title="Higher priority">
                ↑
              </button>
              <button
                className="btn icon-btn"
                disabled={i === order.length - 1}
                onClick={() => move(i, 1)}
                title="Lower priority"
              >
                ↓
              </button>
            </span>
            <span className="priority-name">{SOURCE_LABELS[name] ?? name}</span>
            {SOURCE_HINTS[name] && <span className="priority-hint">{SOURCE_HINTS[name]}</span>}
            <Toggle
              on={enabled}
              onChange={(v) => setForm({ ...form, [`source_${name}_enabled`]: v ? "true" : "false" })}
            />
          </div>
        );
      })}
    </div>
  );
}

export default function Settings() {
  const queryClient = useQueryClient();
  const { data: saved, isLoading } = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<SettingsType>("/settings"),
  });

  const [form, setForm] = useState<SettingsType>({});
  useEffect(() => {
    if (saved) setForm(saved);
  }, [saved]);

  const save = useMutation({
    mutationFn: () => api.put<SettingsType>("/settings", form),
    onSuccess: (data) => {
      queryClient.setQueryData(["settings"], data);
      setForm(data);
    },
  });

  const [qbtTest, setQbtTest] = useState<string | null>(null);
  const testQbt = useMutation({
    mutationFn: () =>
      api.post<{ version: string }>("/settings/qbittorrent/test", {
        url: form.qbittorrent_url,
        username: form.qbittorrent_username,
        password: form.qbittorrent_password,
      }),
    onSuccess: (d) => setQbtTest(`✔ Connected — qBittorrent ${d.version}`),
    onError: (e) => setQbtTest(`✖ ${(e as Error).message}`),
  });

  const [cvTest, setCvTest] = useState<string | null>(null);
  const testCv = useMutation({
    mutationFn: () =>
      api.post("/settings/comicvine/test", { api_key: form.comicvine_api_key }),
    onSuccess: () => setCvTest("✔ ComicVine key works"),
    onError: (e) => setCvTest(`✖ ${(e as Error).message}`),
  });
  const [browsingDdl, setBrowsingDdl] = useState(false);

  if (isLoading || !saved) {
    return (
      <>
        <Toolbar title="Settings" />
        <Spinner />
      </>
    );
  }

  const set = (key: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [key]: e.target.value });
  const setBool = (key: string) => (v: boolean) => setForm({ ...form, [key]: v ? "true" : "false" });

  const text = (key: string, secret = false) => (
    <input type={secret ? "password" : "text"} value={form[key] ?? ""} onChange={set(key)} />
  );

  return (
    <>
      <Toolbar title="Settings">
        {save.isSuccess && <span style={{ color: "var(--success)", fontSize: 13 }}>Saved</span>}
        <button className="btn primary" onClick={() => save.mutate()} disabled={save.isPending}>
          Save Changes
        </button>
      </Toolbar>
      <div className="content">
        <RootFolders />

        <div className="settings-section">
          <h3>Media Management</h3>
          <p className="section-hint">
            Naming uses {"{series}"}, {"{year}"}, {"{issue:03d}"} and {"{title}"} placeholders.
            Output is Komga/Kavita-friendly (CBZ/CBR kept as downloaded, ComicInfo.xml added to CBZ).
          </p>
          <div className="form-row">
            <label>Issue naming</label>
            {text("naming_template")}
          </div>
          <div className="form-row">
            <label>Monitor interval (minutes)</label>
            {text("monitor_interval_minutes")}
          </div>
        </div>

        <div className="settings-section">
          <h3>Metadata — ComicVine</h3>
          <p className="section-hint">
            Series and issue lists come from ComicVine. Get a free API key at
            comicvine.gamespot.com/api and paste it here — search won't work without it.
          </p>
          <div className="form-row">
            <label>API key</label>
            {text("comicvine_api_key", true)}
          </div>
          <div className="form-row">
            <label></label>
            <button className="btn" onClick={() => testCv.mutate()} disabled={testCv.isPending}>
              Test Key
            </button>
            {cvTest && (
              <span style={{ fontSize: 13, color: cvTest.startsWith("✔") ? "var(--success)" : "var(--danger)" }}>
                {cvTest}
              </span>
            )}
          </div>
        </div>

        <div className="settings-section">
          <h3>Sources</h3>
          <p className="section-hint">
            Releases are found and downloaded from the highest-priority enabled source.
          </p>
          <SourcePriority form={form} setForm={setForm} />
          <div className="form-row">
            <label>GetComics base URL</label>
            {text("getcomics_base_url")}
          </div>
          <div className="form-row">
            <label>HTTP proxy (optional)</label>
            {text("getcomics_proxy")}
            <span style={{ color: "var(--text-faint)", fontSize: 13 }}>
              Routes all GetComics traffic (searches + downloads) through this proxy, e.g. a
              VPN-side Privoxy such as http://gluetun:8118.
            </span>
          </div>
          <div className="form-row">
            <label>DDL staging directory</label>
            {text("ddl_directory")}
            <button className="btn" onClick={() => setBrowsingDdl(true)}>
              Browse...
            </button>
            <span style={{ color: "var(--text-faint)", fontSize: 13 }}>
              Where downloads land before import. Empty = &lt;data dir&gt;/ddl.
            </span>
          </div>
        </div>

        <div className="settings-section">
          <h3>Download Client — qBittorrent (optional)</h3>
          <p className="section-hint">
            Manual magnet grabs are sent to qBittorrent and imported when complete. Not needed
            for GetComics direct downloads.
          </p>
          <div className="form-row">
            <label>Enabled</label>
            <Toggle on={form.qbittorrent_enabled === "true"} onChange={setBool("qbittorrent_enabled")} />
          </div>
          <div className="form-row">
            <label>URL</label>
            {text("qbittorrent_url")}
          </div>
          <div className="form-row">
            <label>Username</label>
            {text("qbittorrent_username")}
          </div>
          <div className="form-row">
            <label>Password</label>
            {text("qbittorrent_password", true)}
          </div>
          <div className="form-row">
            <label>Category</label>
            {text("qbittorrent_category")}
          </div>
          <div className="form-row">
            <label></label>
            <button className="btn" onClick={() => testQbt.mutate()} disabled={testQbt.isPending}>
              Test Connection
            </button>
            {qbtTest && (
              <span style={{ fontSize: 13, color: qbtTest.startsWith("✔") ? "var(--success)" : "var(--danger)" }}>
                {qbtTest}
              </span>
            )}
          </div>
        </div>
      </div>
      {browsingDdl && (
        <FolderBrowser
          onPick={(path) => {
            setForm({ ...form, ddl_directory: path });
            setBrowsingDdl(false);
          }}
          onClose={() => setBrowsingDdl(false)}
        />
      )}
    </>
  );
}
