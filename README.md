<p align="center">
  <img src="assets/pullarr-icon.svg" alt="Pullarr" width="160" height="160">
</p>

# pullarr

Radarr/Sonarr-style automation for **western comics**. Monitor series, grab new
issues from [GetComics](https://getcomics.org) automatically, and organize
everything as CBZ/CBR files with `ComicInfo.xml` — ready for
[Komga](https://komga.org) or [Kavita](https://www.kavitareader.com). Pullarr
has no built-in reader by design; it is the automation half of your comic
stack. It is mangarr's western-comics sibling and shares its architecture.

![stack](https://img.shields.io/badge/backend-FastAPI-009688) ![stack](https://img.shields.io/badge/frontend-React-61dafb) ![stack](https://img.shields.io/badge/db-SQLite-003b57)

## Features

- **Library management** — add series via ComicVine metadata search (covers,
  descriptions, publishers, full issue lists with release dates), poster-grid
  library, per-series issue tables with monitor toggles, wanted/missing view.
- **Metadata: ComicVine + optional Metron enrichment** — a ComicVine "volume" (e.g. *Batman (2016)*) is a
  pullarr series; its issues drive what gets hunted. Needs a free API key from
  <https://comicvine.gamespot.com/api> (Settings → Metadata). Optional Metron
  credentials add creators, summaries, arcs, page counts, status, and reprint
  mappings by exact ComicVine ID cross-reference.
- **Source: GetComics** — the monitor searches getcomics.org for monitored,
  missing, released issues and downloads the **main-server direct-download**
  file (CBR/CBZ as posted), with ordered Pixeldrain and MediaFire fallback.
  Packs/TPBs found via interactive search import
  with per-file matching. An optional **HTTP proxy setting** routes all
  GetComics traffic (searches *and* file downloads) through e.g. a VPN-side
  Privoxy.
- **qBittorrent (optional)** — manual magnet grabs go to qBittorrent under a
  `pullarr` category and are imported when complete.
- **Existing libraries** — point a series at a folder you already have: scan
  adopts files in place (never re-downloads), rename previews/applies the
  naming convention format-preservingly, cleanup finds duplicate files, and
  unmatched TPB archives can be mapped to issue ranges by hand.
- **Output** — `Series Title (Year)/Series Title #012.cbz`, with existing
  `ComicInfo.xml` safely merged/refreshed in CBZs (CBRs left untouched).
- **Recoverable automation** — persisted background jobs, validated atomic
  imports, transient-download retry/backoff, failed-download retry/block
  controls, and collision quarantine.
- ***arr-style API** — everything under `/api/v1` with `X-Api-Key` auth.

## Quick start (Docker)

```bash
git clone <this repo> pullarr && cd pullarr
docker compose up -d
```

The default compose file runs the `kirantheram/mangarr:latest` Docker Hub image
and does not include a VPN or download-client sidecar. Open
<http://localhost:6997> after the container starts.

First-run checklist, in the pullarr UI:

1. **Settings → Root Folders**: add `/comics` (mapped to `./data/comics`).
2. **Settings → Metadata**: paste your ComicVine API key, *Test Key*.
3. **Settings → Sources**: optionally configure an HTTP proxy for GetComics
   traffic. Leave it blank for a direct connection.
4. **Add New**: search a title, pick a root folder, add. Issues appear after
   the automatic ComicVine sync (a few seconds).
5. **Optional Metron**: create an account at <https://metron.cloud>, enter the
   username/password under Settings → Metadata enrichment, and test it.

### Optional Gluetun proxy

Pullarr can use the HTTP proxy from an existing
[Gluetun](https://github.com/qdm12/gluetun) container without routing the
Pullarr web UI or ComicVine traffic through the VPN. Enable Gluetun's HTTP
proxy (`HTTPPROXY=on`; port `8888` by default), then attach both containers to
the same user-defined Docker network. For example, if the containers are named
`gluetun` and `pullarr`:

```bash
docker network create vpn-proxy
docker network connect vpn-proxy gluetun
docker network connect vpn-proxy pullarr
```

If that network already exists, skip the first command. In Pullarr, set
**Settings → Sources → HTTP proxy** to `http://gluetun:8888`. Only GetComics
searches and downloads use this application-level proxy; Pullarr remains
available at port `6997`. Add the shared external network to both Compose files
if you want the connection to persist automatically when containers are
recreated.

## How grabbing works

1. When you add a series, pullarr syncs the full issue list from ComicVine and
   links the series to GetComics by search term (usually the title — editable
   under *Edit sources* on the series page when the site names it
   differently).
2. The monitor job (default: hourly) diffs GetComics posts against monitored,
   missing, **released** issues (future cover dates are left alone). Recent
   issues come from a series-wide search; older stragglers get targeted
   per-issue searches, a few per pass.
3. A grab resolves the post's *DOWNLOAD NOW* main-server link, streams the
   file to a staging directory, then imports it: filename-matched to issues,
   renamed to convention, `ComicInfo.xml` injected (CBZ only). Packs import
   every file they contain; single-issue grabs that can't be filename-matched
   are trusted to be the grabbed issue.

## Local development

Backend (Python ≥3.11):

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/uvicorn pullarr.main:app --port 6997 --reload
```

Frontend (Node ≥20):

```bash
cd frontend
npm install
npm run dev        # Vite dev server on :5173, proxies API to :6997
```

Tests:

```bash
cd backend && .venv/bin/python -m pytest
```

`npm run build` writes the production bundle to `backend/static/`, which the
FastAPI app serves when present.

## Configuration

Environment variables (all optional):

| Variable            | Default | Description                          |
| ------------------- | ------- | ------------------------------------ |
| `PULLARR_PORT`      | `6997`  | HTTP port                            |
| `PULLARR_DATA_DIR`  | `data`  | SQLite DB, API key, DDL staging      |

Everything else (ComicVine key, GetComics base URL/proxy, naming template,
qBittorrent, monitor interval) lives in the UI under Settings and is stored
in the DB.

The API key is generated on first start at `<data dir>/api_key` and shown by
`GET /initialize.json`.

Please be a good citizen: keep the honest User-Agent and don't lower the
rate limits — GetComics is a small site and ComicVine caps free keys at 200
requests/resource/hour.

## Roadmap

- Weekly-pack ingestion (GetComics "0-Day" weekly packs) filtered to
  monitored series.
- Mirror fallback (Pixeldrain API) when the main server is down.
- Notifications (Discord/webhooks) on grab/import.
