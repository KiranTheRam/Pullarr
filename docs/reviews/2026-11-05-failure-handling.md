# Pullarr failure handling

Implemented on `imported_updates` against baseline `ca3cb37`. Adapted the behavior reviewed in Mangarr [#46](https://github.com/KiranTheRam/Mangarr/pull/46), [#49](https://github.com/KiranTheRam/Mangarr/pull/49), and [#69](https://github.com/KiranTheRam/Mangarr/pull/69) to Pullarr's release-based DDL downloads and configured Torznab feeds.

## Behavior

- Typed network interruptions, timeouts, retryable HTTP 408/425/429/5xx responses, and wrapped transport failures retain automatic retries. Generic parser/runtime failures no longer receive network retries solely because they contain words such as "timeout". Wrapped DNS socket errors remain network failures.
- Exhausted transient release failures suppress the same source/payload for two hours. The third exhausted failure of that release on that source within 24 hours escalates to Pullarr's existing 24-hour suppression. Different releases and sources retain their own failure history.
- Existing unclassified failures and permanent source failures retain their 24-hour suppression. Explicit user blocks remain indefinite, and alternate releases from the same source remain eligible.
- Disabled sources, missing/deleted root folders, unavailable libraries, filesystem failures, invalid client URLs/headers, import collisions, local import configuration failures, cancellations, and interrupted local attempts do not suppress a release on its source. Explicit user blocks still take precedence. A series without a root folder is not grabbed repeatedly.
- A transient outage opens a shared ten-minute cooldown for the source. Linking, release listing, targeted searches, interactive issue lookup, and the direct worker respect it. One warning announces the outage; subsequent skips log at debug. Other sources continue. Torznab search failures cool the indexer; qBittorrent submission failures do not penalize a successful indexer.
- Workers defer queued downloads from a cooling source without consuming attempts or stopping work on other sources. Automatic retries preserve their error, attempt count, history, and next retry time in Activity. Manual retry clears the failure classification and block, while respecting an active source cooldown.
- The new `transient_error` column migrates idempotently with a false default for existing rows. Both automatic retries and terminal failures remain visible through the existing queue and failed-queue APIs; failed entries have no age-based visibility cutoff.
- Linking failures do not fabricate successful source links. Transient failures retry after the cooldown; broken searches retain a per-series 24-hour backoff.

## Validation

- Baseline: 635 backend tests passed, one Linux-only address-space-limit test skipped on macOS; clean frontend install and production build passed.
- Final local backend: 688 passed, one Linux-only test skipped. The new failure-handling module contributes 53 regression cases, covering real worker/monitor/queue paths, failure windows and escalation, source isolation, local recovery, and legacy migration. Existing monitor fixtures now provide valid temporary library roots.
- Frontend: `npm ci`, all 16 tests, TypeScript compilation, and production Vite build passed. No frontend or dependency manifests changed.
- Both full and production-only npm audits and the pinned runtime Python audit report no known vulnerabilities. The isolated test environment's initially old pip/setuptools were upgraded to pip 26.3/setuptools 84.2.1; its complete Python audit then reported no known vulnerabilities. The local editable application is covered by the tests rather than a PyPI advisory entry. Available nonsecurity package updates were recorded but were outside this feature change.
- Native Suika candidate: all 689 Linux backend tests passed. Docker build, empty-config first-run setup, and integrated stack verification passed against the final image. The fresh-config smoke exercises setup/login, session authentication, API-key access, Host checks, CSRF, saved-secret handling, password changes/recovery, logout, and throttling.
- Integration checks passed for all four containers, app endpoints, VPN/proxy egress, both qBittorrent connections, empty queues, both webhook deliveries, NextPanel's app connections, and root folders. Startup and scheduled queue/sync logs were clean.
- Pullarr read-only source smoke passed ComicVine (three results), Metron authentication, and GetComics through its configured VPN proxy (three series and 21 releases). No comic payload was resolved or downloaded.
- The shared source helper stopped on an outdated Mangarr source table (`KeyError: webtoons`). Its already-completed checks passed. An external temporary copy adding the two current source names was used to complete the same read-only contracts, with no change to Mangarr or the skill. All ten enabled Mangarr sources, both metadata providers, and Nyaa were covered, including one representative page for downloadable sources and metadata-only checks for VIZ/Wikipedia.

## Test environment and limits

Only Pullarr was replaced, using `arr-maintenance/pullarr:candidate` (amd64 image `sha256:dd211fd185295633ea2ca67f95c69d124470cbee28302db90aa226fb3ec41250`), in the isolated `/home/docker/arr-maintenance` stack on Suika. Mangarr, NextPanel, and qBittorrent retained their original container IDs. All branch application changes were synchronized from `backend/`, `frontend/`, and `docker/`, excluding user data, credentials, caches, generated output, and Git metadata. Production was not accessed or changed.

SQLite online backups of all three sanitized application databases were verified with `PRAGMA integrity_check` before each deployment. The final backup set is `/home/docker/arr-maintenance/backups/pullarr-failure-final-20261105T222344Z`; earlier known-good backups were retained. Pullarr's migrated database remains healthy with an empty active queue. SHA-256 comparisons confirmed that all six changed runtime modules in the running candidate match the local source. The final candidate is left running. Disposable fresh-setup containers were removed.

The local machine has no Docker CLI; container build and execution checks therefore ran natively on Suika (amd64/Python 3.12), in addition to local macOS/Python 3.11 tests. Failure injection uses deterministic temporary databases and simulated source failures; no live outage, real download, torrent addition, or production library mutation was induced. Source cooldowns and failed-link retry timers are intentionally in memory and reset on restart; release failure classifications and retry state persist in SQLite. The optional live Kavita integration was not configured or exercised; its existing unit tests passed.

The unrelated untracked `.claude/` directory was preserved. No push, publication, or production deployment was performed.
