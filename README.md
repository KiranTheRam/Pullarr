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
  Choose all issues, future issues, or a starting issue for automatic grabs.
  The library editor can change monitoring or root folders for several series
  at once, refresh or search them, and remove records while keeping their files.
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
  unmatched TPB archives can be mapped to issue ranges by hand. The import page
  discovers untracked folders, lets you confirm ComicVine matches, and adds
  several series without moving files. Root changes can optionally move a
  series folder after collision and in-progress work checks.
- **Finding releases** — jump to a series or ComicVine search with Ctrl/Cmd+K,
  and browse known issue dates on the release calendar. Downloaded files show
  their recorded source when Pullarr imported them; older files have no source
  label. Completed series are checked less often, using a Settings interval.
- **Output** — `Series Title (Year)/Series Title #012.cbz`, with existing
  `ComicInfo.xml` safely merged/refreshed in CBZs (CBRs left untouched).
- **Recoverable automation** — persisted background jobs, validated atomic
  imports, transient-download retry/backoff, failed-download retry/block
  controls, and collision quarantine.
- **Kavita scans** — optionally tell Kavita to scan after an import, so new
  issues appear in the reader immediately. Scans the affected series when
  Kavita already knows it, and the whole library when it does not (a series
  pullarr just created, or a title Kavita holds more than once).
- ***arr-style API** — everything under `/api/v1` with `X-Api-Key` auth.

## Quick start (Docker)

```bash
git clone <this repo> pullarr && cd pullarr
docker compose up -d
```

The default compose file runs the `kirantheram/pullarr:latest` Docker Hub image
and does not include a VPN or download-client sidecar. Open
<http://localhost:6997> after the container starts.
The image checks the public, secret-free `GET /ping` route and shows as `healthy`
once Pullarr is serving, including when `PULLARR_HOST` or `PULLARR_PORT` is set.
The probe bypasses HTTP proxies; browser login and API authentication still apply
to their normal routes, and Host validation also applies to `/ping`. This is a
liveness check, so it does not test the database or external services.

First-run checklist, in the pullarr UI:

1. **Settings → Root Folders**: add `/comics` (mapped to `./data/comics`).
2. **Settings → Metadata**: paste your ComicVine API key, *Test Key*.
3. **Settings → Sources**: optionally configure an HTTP proxy for GetComics
   traffic. Leave it blank for a direct connection.
4. **Add New**: search a title, pick a root folder, add. Issues appear after
   the automatic ComicVine sync (a few seconds).
5. **Optional Metron**: create an account at <https://metron.cloud>, enter the
   username/password under Settings → Metadata enrichment, and test it.

### Container file ownership (PUID / PGID / UMASK)

The image preserves its existing identity and inherited umask when these variables
are unset or empty (normally root and `022`). To create files as your host user:

| Variable | Example | Meaning |
| --- | --- | --- |
| `PUID` | `99` | Decimal user ID; set together with PGID |
| `PGID` | `100` | Decimal primary group ID; set together with PUID |
| `UMASK` | `002` | Octal mask for new files and directories; works independently |

Unraid commonly uses `99:100` (`nobody:users`). `UMASK=002` gives ordinary new
files mode `664` and directories `775`; browser credential files remain `600`.
Existing file modes, copied files and hardlinks retain their existing permissions.
IDs must be in `0..4294967294`, and masks in `000..777` (an optional leading zero
is accepted). IDs must also be supported by the host filesystem and Docker/LXC
user mapping; unsupported ownership changes fail before the command starts.
Use a nonzero user ID to run without root identity.

Setting PUID and PGID opts into preparing **only the dedicated appdata directory**
(`PULLARR_DATA_DIR`, `/config` by default). Startup changes its ownership and that
of regular files and directories inside it, failing if ownership cannot be set.
Keep libraries and downloads outside appdata. Symlinks, multiply linked files,
special files and nested mounts are skipped; symlinked appdata and system/media
paths are refused. No library or external download folder is automatically chowned.
For an existing installation, stop Pullarr, back up appdata, and give the selected
user access to the specific library and download folders on the host yourself.
Use compatible ownership and group permissions for qBittorrent and shared mounts.

PUID/PGID require root at startup; the command then runs with the chosen numeric
IDs, no supplementary groups or Linux capabilities, and no privilege escalation.
No user accounts are created. Alternatively use Docker `--user UID:GID` (Compose
`user:`), omit PUID/PGID, and prepare writable appdata and media paths yourself.
Docker `--group-add` groups are preserved in this mode. UMASK works with either
mode, including a read-only root filesystem when the mounted appdata is writable.

The supplied command replaces startup as PID 1, so custom commands, listener
settings, healthchecks and stop signals work as before. Docker exec does not run
the entrypoint: use `--user UID:GID` for other maintenance commands that write app
data. The existing password recovery command works from a root console too: it
drops to the saved login file's owner before opening the database or replacing
credentials. Login and recovery remain managed by Pullarr.

### Triggering Kavita scans

Kavita only picks up new issue files when it scans. To have imports show up in
the reader immediately:

1. In Kavita, go to **Settings → Account** and copy the **API Key**.
2. In Pullarr, go to **Settings → Connect — Kavita**, turn it on, enter
   Kavita's URL (e.g. `http://kavita:5000`) and the API key, then select
   **Test Connection** — this also loads Kavita's libraries.
3. Check the **Library mapping** rows. Pullarr matches a root folder to a
   Kavita library by folder name, so `/comics/DC` finds a library at
   `/data/comics/DC` even when the containers mount it differently. Pick a
   library explicitly if the two paths have nothing in common or multiple
   libraries share the same matching folder name. Ambiguous paths scan nothing
   until mapped, which protects separate publisher libraries.

**Scan scope** decides how much Kavita re-reads. *Series* scans only the
affected series folder, which is much cheaper on a large library. Pullarr tries
the year-qualified name first (`Batman (2016)`), because a bare title Kavita
holds more than once is ambiguous — rather than guess between reboots it falls
back to a library scan, as it also does for a series Kavita has never indexed.
Alternate titles are excluded because they can name an unrelated existing
series. *Whole library every time* skips the lookup. Each import restarts a
15-second quiet period for its series; later imports within five minutes of
its last scan wait for one trailing scan. Other series can scan independently,
and a library scan covers all series targets in the same batch. Pending scans
are held in memory and are lost on restart. A Kavita that is down or
misconfigured never fails a download.

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

Install `bsdtar` (`libarchive-tools` on Debian/Ubuntu, `libarchive` on macOS)
for RAR/7z validation. The Docker image includes it. Invalid, encrypted or
unsupported archives fail validation instead of being marked downloaded.

Archive validation, metadata refresh and duplicate comparison stream page data.
ZIP directories are checked before loading them (16 MiB and 20,000 entries),
with a 2 GiB limit per ZIP member and a 10 GiB cumulative expanded-data limit.
Containers are limited to 12 GiB on disk, leaving space for compression overhead.
An individual container must also stay below 100 times its compressed size,
with a 512 MiB allowance for small, highly compressible comics. Nested transport
packs allow 1,000 comic archives per level and at most two nesting levels;
all levels share the expanded-data, entry-count and 120-second decoding budget.
Loose-image imports use the same page-count and expanded-data limits. Unsafe ZIP
paths, links, special files, encryption and unsupported codecs are rejected.
ZIP STORED, DEFLATE, BZIP2 and LZMA pages remain supported; BZIP2/LZMA and RAR/7z
codecs run in disposable subprocesses. Linux enforces a 1 GiB decoder address-space
limit and a 120-second CPU limit; macOS monitors decoder resident memory instead
because it does not support the Linux memory limit. Decoded output and wall time
are bounded on both platforms. Validation failure leaves an existing archive
intact; failed rewrites remove their partial output.

ComicInfo reads and writes are limited to 1 MiB. Matching ignores metadata with
DTDs, more than 10,000 XML elements, or nesting deeper than 64 elements; metadata
refresh replaces such content while preserving ordinary external fields.

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
# From frontend/: npm test
```

`npm run build` writes the production bundle to `backend/static/`, which the
FastAPI app serves when present.

The Python 3.12 Linux image installs with `backend/constraints.txt`, using the
existing pip toolchain to pin runtime dependencies. To refresh it, resolve the
dependencies in a clean Python 3.12 Linux environment, update the exact versions,
then run backend tests, `pip-audit`, the image build and integration checks.
The base-image tags and OS packages still follow upstream updates; use immutable
image digests when deploying a validated release.

The sidebar, API, startup log, and outgoing `User-Agent` report the installed
Python package version. Local installs and Docker builds default to
`0.2.0+local`; reinstall the backend after changing its version metadata.
The frontend package version is a development manifest, not the running app's
version.

For a release image, pass the Git tag as `PULLARR_BUILD_VERSION` (for example
`--build-arg PULLARR_BUILD_VERSION=v0.2.1`). Python normalizes it to `0.2.1`.
For an untagged build, pass `0.2.0+g<full-commit-sha>` instead. The existing
Forgejo image workflow supplies the tag for a tag-selected manual run, or the
commit version for main-branch builds; its publication triggers are unchanged.
Set `PULLARR_BUILD_REVISION` to the full commit SHA to record it in the image's
`org.opencontainers.image.revision` label. These are build-time values; runtime
environment variables cannot change the installed version. Custom or dirty
builds must supply their own distinguishing suffix if `+local` is insufficient.
Release tags must be valid Python versions. Patch numbers are not incremented
automatically.

### Recovering downloads

After a restart, interrupted direct downloads appear under **Activity → Failed**
with an explanation and a Retry action. Existing library files are preserved;
retry handles identical files as duplicates. Staged leftovers from an interrupted
process are retained rather than deleting potentially recoverable data.

**Block release** excludes that release only; alternatives remain eligible.
For **needs attention** items, use **Map files** to associate downloaded files with
issues, then **Mark resolved** to acknowledge the repair without downloading again.
Resolving an item does not itself mark any issues as downloaded.

Heavy archive, import, scan, rename and cleanup work runs outside the API event
loop. Filesystem operations share a worker lane to avoid concurrent file writes.

## Configuration

Environment variables (all optional):

| Variable            | Default | Description                          |
| ------------------- | ------- | ------------------------------------ |
| `PULLARR_HOST`      | `0.0.0.0` | HTTP bind address (IPv4, IPv6, or a local DNS name) |
| `PULLARR_PORT`      | `6997`  | HTTP port                            |
| `PULLARR_DATA_DIR`  | `data`  | SQLite DB, API key, DDL staging      |
| `PULLARR_SQLITE_WAL` | `true` | Use WAL on local database storage; see below |
| `PULLARR_AUTH_USERNAME` | *(empty)* | Optional environment-managed HTTP Basic login username |
| `PULLARR_AUTH_PASSWORD` | *(empty)* | Browser login password; set together with the username |
| `PULLARR_ALLOW_UNAUTHENTICATED_BROWSER` | `false` | Explicitly disable first-run login for an externally authenticated deployment |
| `PULLARR_ALLOWED_HOSTS` | `[]` | Additional exact DNS names as a JSON array (no scheme, port or wildcard) |

**Database storage and backups.** SQLite uses WAL so reads can continue while
another connection writes, with a 30-second busy timeout for competing writers.
Keep the config/data directory on local storage: WAL does not support NFS/SMB.
If that directory uses network storage, set `PULLARR_SQLITE_WAL=false` before
starting; Pullarr switches the database back to the DELETE rollback journal.
This avoids WAL's shared-memory requirement; the filesystem must still provide
reliable SQLite locking. Prefer local storage for the database even when comics
live on a network share. The directory must be writable for SQLite's sidecar files.

WAL creates `pullarr.db-wal` and `pullarr.db-shm` beside the database; committed
data may still be in the WAL. Use SQLite's online backup API or
`sqlite3 pullarr.db ".backup backup.db"` for a live backup. For a filesystem
backup, stop every process accessing the database first and copy the database
and any remaining sidecars together. Never copy just a live `pullarr.db`.

The journal change requires no schema migration. To roll back WAL, stop all
Pullarr processes, keep a verified backup, then restart with
`PULLARR_SQLITE_WAL=false` (or run `PRAGMA journal_mode=DELETE` with SQLite
while the app is stopped). Confirm the returned mode is `delete` before moving
the database to network storage or running an older image. Rolling back this
change leaves the existing schema and download records intact.

Everything else (ComicVine key, GetComics base URL/proxy, naming template,
qBittorrent, monitor interval) lives in the UI under Settings and is stored
in the DB.

Existing bootstrap API keys at `<data dir>/api_key` keep working through
`X-Api-Key`. Create independently revocable keys under **Settings → API Keys**
for new integrations.

### Access and VPN boundaries

On first use, including upgrades without configured credentials, open Pullarr on
a trusted local network and create a username and password. The first visitor to
submit setup creates the login. Passwords must contain 12–128 printable
characters. Setup signs you in automatically; passwords are hashed with scrypt
in a private `<data dir>/browser_login.json` file. Keep this file with the config
volume across container upgrades. Existing library data, settings, and API keys
are unchanged; no database migration is required.

The browser uses a seven-day HttpOnly session cookie, without receiving the
bootstrap API key. **Settings → Browser Security** provides password changes and
sign-out. Changing or locally resetting the password invalidates every browser
session while API keys keep working. Failed sign-ins are limited to five per
client address per minute; concurrent password checks and tracked clients are
bounded. Forwarded client addresses are ignored by default; configure
`FORWARDED_ALLOW_IPS` only for a trusted reverse proxy. Clients behind that proxy
share a limit unless trusted forwarding is configured.

For forgotten credentials, the sign-in page shows local recovery instructions.
Run `docker compose exec pullarr python -m pullarr.reset_password`, or in the
Unraid container console run `python -m pullarr.reset_password`. For a direct
Python installation, run the same module as the service user with the same data
directory. The interactive command displays the retained username and prompts
for a new password without echoing it. Do not delete the login file to reset a
password: deletion reopens first-visitor setup.

Existing environment-managed logins remain supported: set both
`PULLARR_AUTH_USERNAME` and `PULLARR_AUTH_PASSWORD`, then restart. Setting only
one stops startup. Recover those credentials by updating the environment and
restarting. Use HTTPS on a trusted reverse proxy when credentials cross the
network. An externally authenticated deployment can explicitly set
`PULLARR_ALLOW_UNAUTHENTICATED_BROWSER=true` to skip setup when no login exists;
anyone who reaches such an instance can obtain API access, and startup warns.
This option does not override an existing app-managed or environment login.

Host validation is always on, including for API clients. IP addresses and
`localhost` work by default. **Before upgrading**, add every DNS name used by
browsers, reverse proxies and integrations (such as NextPanel reaching Pullarr by
its container name) to `PULLARR_ALLOWED_HOSTS`, for example
`'["pullarr", "comics.example.com"]'`. Names match exactly, ignoring case. An
unknown Host gets HTTP 400 with instructions, and direct IP access stays available
for recovery. Proxies must preserve an allowed Host or set one explicitly.
Host validation limits DNS rebinding; it is not a login.

`X-Api-Key` clients and named API keys keep working without browser credentials.
App-managed browser sessions authenticate API requests with same-origin checks
for writes. Environment-managed Basic credentials alone do not authenticate API
calls. API keys do not unlock the browser UI.

Saved integration secrets are bound to the exact saved URL, including scheme, port
and path, and the qBittorrent password also to the username. The Metron password
is bound to the saved Metron username. After changing one of those, re-enter the secret before **Test** or **Save**. Torznab API keys are bound
to their indexer's URL the same way. HTTP client request logs are suppressed, even
at `DEBUG`, and integration errors omit URLs and response bodies so they cannot
expose credentials. ComicInfo metadata larger than 1 MiB, or compressed with
anything other than ZIP STORED or DEFLATE, is ignored during matching and replaced
on import; the archive's filename matching still applies.

When VPN routing is required, keep the GetComics proxy configured for both
searches and downloads. Mirror fallback uses the same proxy-aware client; proxy
failures never trigger a direct-connection retry. qBittorrent's VPN and kill switch
remain the responsibility of its container/network configuration.

Please be a good citizen: keep the honest User-Agent and don't lower the
rate limits — GetComics is a small site and ComicVine caps free keys at 200
requests/resource/hour.

## Roadmap

- Weekly-pack ingestion (GetComics "0-Day" weekly packs) filtered to
  monitored series.
- Mirror fallback (Pixeldrain API) when the main server is down.
- Notifications (Discord/webhooks) on grab/import.

### Torznab / Prowlarr

In Settings, add one or more **Torznab indexers** with a name, API URL, and API key.
For Prowlarr, use the Torznab URL copied from the indexer, or an aggregate endpoint
such as `http://prowlarr:9696/all/api`. The default category is comics (`7030`);
you can enter other category IDs separated by commas. Use `search`, or `book` if
supported by your indexer. API keys are masked after saving.

Enable and configure qBittorrent to search and grab torrents. Search Releases
combines GetComics and torrent results, with source labels, size, and seeder/leecher
counts. Both magnets and torrent-file links are supported. Completed downloads use
the existing library importer; qBittorrent's completed paths must be accessible to
Pullarr. Torrent data is copied on import so qBittorrent can keep seeding.

Default torrent query templates live in Settings. Under **Torrent search options**
on a series page, override the search title (for example, a French title) and/or
provide templates, one per line:

```text
{series} T{issue:02d}
{series} Tome {issue}
{series} #{issue:03d} {year}
```

Variants are tried in order until an indexer returns results. `{year}` uses the
issue's release year, falling back to the series year. A series-wide search uses
the search title alone. The release-search dialog also accepts a manual query;
leave it blank to return to automatic queries. Scheduled and Search Missing jobs
try Torznab after GetComics, require seeders and a matching rendered title/issue
query, and skip active or blocked downloads. Broad templates without both
`{series}` and `{issue}` are available for interactive searches only.
