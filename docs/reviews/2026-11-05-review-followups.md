# Review follow-ups

Implemented the three follow-ups from the independent review of `imported_updates` against `github/main`.

## Changes

- Library availability checks now infer expected primary-root content from the recorded paths of downloaded files. An absolute extra folder can hold a series' entire collection without making an empty primary root look unmounted. Neighboring series use the same rule. Unknown legacy paths remain conservative, and a missing file recorded inside the root still protects an unexpectedly empty mount. Path comparisons do not access the share.
- Unmatched imports now use a byte-budgeted filename helper. It preserves the archive extension, reserves room for atomic-write suffixes, and retains a digest when shortening names so extras with a shared long prefix remain distinct.
- Kavita's quiet-period debounce now has a one-minute limit for continuous imports. Subsequent scans retain the existing five-minute floor, including imports arriving during a slow delivery. README timing guidance was updated.

Six regression cases cover external-only ownership in the current series and a neighbor, mixed internal/external ownership, conservative legacy ownership, long ASCII and multibyte unmatched imports, and continuous Kavita imports spanning the first and subsequent scan windows. Existing quiet-burst, connection-change, slow-delivery, mount-loss, collision, and archive safety tests continue to pass.

## Verification

- Clean temporary Python environment baseline: 797 passed, one Linux-only test skipped on macOS. Final local backend: 803 passed, one skipped. Focused root, generated-file, and Kavita tests: 119 passed.
- The repository virtual environment initially produced three decoder-fixture failures because its interpreter path contains a space. The clean environment under `/tmp` avoids that pre-existing shebang limitation; no unrelated test or decoder change was needed.
- Frontend: clean `npm ci`, all 16 tests, TypeScript compilation, and production Vite build passed. Full and production-only npm audits found no known vulnerabilities. No dependency manifests or lockfiles changed; available nonsecurity upgrades remain outside these fixes.
- The temporary environment's bootstrap pip/setuptools were upgraded before its final audit; the complete environment then had no known vulnerabilities. The Suika test image, including the constrained runtime dependencies, also passed `pip-audit`. Pullarr itself is a local package covered by tests rather than a PyPI advisory entry.
- Native Suika candidate: all 804 Linux backend tests passed. The first test run emitted only a pytest cache warning because its source mount was read-only. The Docker baseline and candidate builds and both Compose configurations passed.
- Disposable empty-config startup passed browser setup/login, session and API-key authentication, Host and CSRF checks, saved-secret protection, password change/logout, sign-in throttling, and interactive password recovery. PUID 99 / PGID 100, private login-file permissions, the healthcheck, clean startup logs, and graceful shutdown passed.
- Full candidate integration passed for all four containers, VPN and proxy egress, both qBittorrent connections, empty queues, both webhooks, NextPanel's app connections, and root folders. Pullarr's candidate logs, zero restart count, database integrity, and running source-file hashes were checked.
- Pullarr's read-only source smoke passed ComicVine (three results), Metron authentication, and GetComics through its configured proxy (three series and 21 releases). No comic payload was resolved or downloaded.
- The shared source helper stopped on its outdated Mangarr source table (`KeyError: webtoons`). A temporary external copy added the two current names and skipped successful checks, completing coverage without repeating successful page fetches. All ten enabled Mangarr sources, both metadata providers, and Nyaa passed. Downloadable sources fetched one representative page each; VIZ and Wikipedia used metadata-only checks. Neither Mangarr nor the skill was changed.

## Isolation and limits

Only Pullarr was replaced in `/home/docker/arr-maintenance` on Suika. Mangarr, NextPanel, and qBittorrent retained their container IDs. SQLite online backups of all three sanitized application databases passed integrity checks before replacement; the initial backup set is `/home/docker/arr-maintenance/backups/pullarr-review-followups-20261106T014710Z`. The final deployed image ID and source revision are included in the delivery report.

The optional live Kavita service was not configured, so its batching behavior was exercised with simulated delivery and the existing client fixtures. No production host, provider download queue, real torrent, or production library was changed. The unrelated `.claude/` directory was preserved. No push or image publication was performed.
