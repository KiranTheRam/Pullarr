import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ApiKey, RootFolder, Settings as SettingsType } from "../api/types";
import { FolderBrowser } from "../components/FolderBrowser";
import { QueryError, Spinner, Toggle, Toolbar } from "../components/common";

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
          <button className="btn icon-btn" title="Remove root folder" aria-label={`Remove ${rf.path}`} onClick={() => remove.mutate(rf.id)}>
            ✕
          </button>
        </div>
      ))}
      {add.isError && <div className="error-banner">{(add.error as Error).message}</div>}
      {remove.isError && <div className="error-banner">{(remove.error as Error).message}</div>}
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

function ApiKeyRow({ apiKey, onRemove }: { apiKey: ApiKey; onRemove: () => void }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(apiKey.key);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard unavailable */
    }
  };
  const used = apiKey.last_used_at
    ? `last used ${new Date(apiKey.last_used_at).toLocaleString()}`
    : "never used";
  return (
    <div className="form-row" style={{ alignItems: "center" }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontWeight: 600 }}>{apiKey.name}</div>
        <code
          style={{
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            fontSize: 13,
            color: "var(--text-dim)",
            wordBreak: "break-all",
          }}
        >
          {apiKey.key}
        </code>
        <div style={{ fontSize: 12, color: "var(--text-faint)" }}>
          Added {new Date(apiKey.created_at).toLocaleDateString()} · {used}
        </div>
      </div>
      <button className="btn" onClick={copy}>
        {copied ? "Copied" : "Copy"}
      </button>
      <button className="btn icon-btn" title="Revoke key" aria-label={`Revoke ${apiKey.name}`} onClick={onRemove}>
        ✕
      </button>
    </div>
  );
}

function ApiKeys() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const { data } = useQuery({
    queryKey: ["apikeys"],
    queryFn: () => api.get<ApiKey[]>("/apikeys"),
  });

  const add = useMutation({
    mutationFn: () => api.post<ApiKey>("/apikeys", { name: name.trim() }),
    onSuccess: () => {
      setName("");
      queryClient.invalidateQueries({ queryKey: ["apikeys"] });
    },
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/apikeys/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["apikeys"] }),
  });

  return (
    <div className="settings-section">
      <h3>API Keys</h3>
      <p className="section-hint">
        Create keys for external clients (e.g. NextPanel or scripts) to access the API. Send a
        key as the <code>X-Api-Key</code> header. Any key here grants full access — revoke ones
        you no longer use.
      </p>
      {data?.map((k) => (
        <ApiKeyRow key={k.id} apiKey={k} onRemove={() => remove.mutate(k.id)} />
      ))}
      {data && data.length === 0 && (
        <p style={{ color: "var(--text-faint)", fontSize: 13 }}>No API keys yet.</p>
      )}
      {add.isError && <div className="error-banner">{(add.error as Error).message}</div>}
      {remove.isError && <div className="error-banner">{(remove.error as Error).message}</div>}
      <div className="form-row">
        <input
          type="text"
          placeholder="Key name (e.g. NextPanel)"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && name.trim() && !add.isPending) add.mutate();
          }}
          style={{ flex: 1, maxWidth: 380 }}
        />
        <button className="btn primary" disabled={!name.trim() || add.isPending} onClick={() => add.mutate()}>
          + Generate Key
        </button>
      </div>
    </div>
  );
}

const SOURCE_LABELS: Record<string, string> = {
  getcomics: "GetComics",
};

const SOURCE_HINTS: Record<string, string> = {
  getcomics: "Main-server direct downloads, with ordered Pixeldrain and MediaFire fallback.",
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
  const { data: saved, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<SettingsType>("/settings"),
  });

  const [form, setForm] = useState<SettingsType>({});
  useEffect(() => {
    if (saved) setForm(saved);
  }, [saved]);
  const dirty = Boolean(saved) && JSON.stringify(form) !== JSON.stringify(saved);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirty) event.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

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
  const [metronTest, setMetronTest] = useState<string | null>(null);
  const testMetron = useMutation({
    mutationFn: () => api.post<{ results: number }>("/settings/metron/test", {
      username: form.metron_username,
      password: form.metron_password,
    }),
    onSuccess: (d) => setMetronTest(`✔ Connected — ${d.results} matching test results`),
    onError: (e) => setMetronTest(`✖ ${(e as Error).message}`),
  });
  const [webhookTest, setWebhookTest] = useState<string | null>(null);
  const testWebhook = useMutation({
    mutationFn: () =>
      api.post("/settings/webhook/test", {
        url: form.webhook_url,
        secret: form.webhook_secret,
      }),
    onSuccess: () => setWebhookTest("✔ Webhook delivered"),
    onError: (e) => setWebhookTest(`✖ ${(e as Error).message}`),
  });
  const [browsingDdl, setBrowsingDdl] = useState(false);

  if (isError) {
    return <><Toolbar title="Settings" /><div className="content"><QueryError error={error} retry={() => refetch()} /></div></>;
  }
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
        {dirty && <span style={{ color: "var(--warning)", fontSize: 13 }}>Unsaved changes</span>}
        {save.isSuccess && <span style={{ color: "var(--success)", fontSize: 13 }}>Saved</span>}
        <button className="btn primary" onClick={() => save.mutate()} disabled={save.isPending || !dirty}>
          Save Changes
        </button>
      </Toolbar>
      <div className="content">
        <RootFolders />
        <ApiKeys />
        {save.isError && <div className="error-banner" role="alert">{(save.error as Error).message}</div>}

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
          <div className="form-row">
            <label>Download retries</label>
            {text("download_retry_attempts")}
            <span style={{ color: "var(--text-faint)", fontSize: 13 }}>Retries transient failures with exponential backoff.</span>
          </div>
        </div>

        <div className="settings-section">
          <h3>Metadata enrichment — Metron (optional)</h3>
          <p className="section-hint">Adds issue summaries, creators, arcs, page counts, status, and collected-edition reprint mappings using ComicVine ID cross-references.</p>
          <div className="form-row"><label>Enabled</label><Toggle on={form.metron_enabled === "true"} onChange={setBool("metron_enabled")} /></div>
          <div className="form-row"><label>Username</label>{text("metron_username")}</div>
          <div className="form-row"><label>Password</label>{text("metron_password", true)}</div>
          <div className="form-row"><label>Issues per refresh</label>{text("metron_issue_enrichment_limit")}
            <span style={{ color: "var(--text-faint)", fontSize: 13 }}>Gradually enriches large series while respecting Metron rate limits.</span>
          </div>
          <div className="form-row"><label></label><button className="btn" onClick={() => testMetron.mutate()} disabled={testMetron.isPending}>Test Metron</button>
            {metronTest && <span style={{ fontSize: 13, color: metronTest.startsWith("✔") ? "var(--success)" : "var(--danger)" }}>{metronTest}</span>}
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
            <label>Download service order</label>
            {text("getcomics_service_preference")}
            <span style={{ color: "var(--text-faint)", fontSize: 13 }}>Comma-separated: main, pixeldrain, mediafire.</span>
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

        <div className="settings-section">
          <h3>Connect — Webhook</h3>
          <p className="section-hint">
            Notify a request manager (e.g. NextPanel) whenever issues are imported, so requests
            flip to Available instantly. Point the URL at NextPanel's
            /api/v1/webhooks/pullarr endpoint and paste the same webhook secret configured there.
          </p>
          <div className="form-row">
            <label>Enabled</label>
            <Toggle on={form.webhook_enabled === "true"} onChange={setBool("webhook_enabled")} />
          </div>
          <div className="form-row">
            <label>Webhook URL</label>
            {text("webhook_url")}
          </div>
          <div className="form-row">
            <label>Secret</label>
            {text("webhook_secret", true)}
          </div>
          <div className="form-row">
            <label></label>
            <button className="btn" onClick={() => testWebhook.mutate()} disabled={testWebhook.isPending || !form.webhook_url}>
              Send Test Event
            </button>
            {webhookTest && (
              <span style={{ fontSize: 13, color: webhookTest.startsWith("✔") ? "var(--success)" : "var(--danger)" }}>
                {webhookTest}
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
