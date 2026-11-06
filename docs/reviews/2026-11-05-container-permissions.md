# Optional container identity and permissions

Adapted Mangarr PR #75 using `gh pr view` and `gh pr diff`, preserving Pullarr's
existing default identity, inherited umask, listener settings, healthcheck,
custom commands, browser login and API authentication.

## Behavior

- Empty or omitted PUID/PGID/UMASK performs no ownership or identity changes.
- PUID and PGID must be supplied together as decimal Linux IDs; UMASK accepts
  octal permission masks independently. Invalid values stop before the command.
- Explicit IDs opt into ownership preparation only within dedicated appdata.
  Descriptor-based traversal skips symlinks, hardlinks, special files and nested
  mounts, including same-filesystem bind mounts. Root, system/media trees and
  symlinked appdata are refused. Ownership failures stop startup.
- Numeric IDs require no account creation. The command runs without supplementary
  groups, capabilities or privilege escalation and replaces startup as PID 1.
- Docker `--user` remains supported when PUID/PGID are omitted, including
  `--group-add` and a read-only root filesystem with writable appdata.
- Local password recovery started from a root console drops to the existing
  login file's owner before accessing SQLite or replacing private credentials.
  This prevents a normal Docker exec/Unraid recovery from leaving an unreadable
  root-owned login file after opting into a non-root app identity.

## Validation

- Baseline: 739 backend tests passed, one Linux-only skip; frontend 16 tests and
  production build passed; native amd64 Docker baseline built successfully.
- Final: 787 backend tests passed locally, one Linux-only skip; all 788 passed
  inside the native candidate. The 48 new identity/recovery tests exercise
  validation, failure paths, ownership exclusions, exec behavior and real umask.
- Final frontend: clean `npm ci`, 16 tests, TypeScript and production build pass.
  Repository Compose, isolated base/candidate Compose and Unraid XML validate.
- Python development/runtime inventories and both npm audits report no known
  vulnerabilities. Dependency versions and lockfiles were not changed.
- The candidate was built natively on Suika. Docker is unavailable on the local
  Mac, so every Docker test used the isolated Suika environment.

The disposable runtime matrix checked the previous image, unchanged defaults,
empty values, UMASK alone, Unraid `99:100`, new `12345:23456` IDs, Docker
`--user` plus supplementary groups and a read-only root filesystem, and custom
appdata/listener/port with Basic authentication, a read-only root filesystem and
unreachable proxy environment variables.

| Mode | UID:GID | Mask | New library directories | New CBZ archives |
| --- | --- | --- | --- | --- |
| Previous image / defaults / empty variables | 0:0 | 022 | 755 | 644 |
| UMASK alone | 0:0 | 002 | 775 | 664 |
| Unraid | 99:100 | 002 | 775 | 664 |
| New numeric IDs | 12345:23456 | 027 | 750 | 640 |
| Docker user / group-add / read-only root | 12345:23456 | 002 | 775 | 664 |
| Custom appdata / listener / Basic / proxy | 99:100 | 077 | 700 | 600 |

Directories were created through the actual root-folder API; archives through
Pullarr's CBZ writer using the image entrypoint. Every mode checked real
`/proc/1/status`, health, unauthorized and API-key requests, PID 1, graceful
shutdown, and unchanged existing library files/hardlinks/nested mounts. Graceful
stops took less than one second. Both identity modes passed empty-config browser
setup, CSRF, sessions, password change/logout/throttling and interactive recovery;
credential files retained mode 600 and their non-root owner. Migration of existing
root-owned appdata preserved the API key, login, database/WAL and restart behavior.
An unprepared external library refused non-root writes without ownership changes.

Bad/missing/overflow IDs, invalid umask, conflicting Docker user, unsafe appdata
paths and denied chown capability were rejected before the command. Suika's LXC
filesystem rejects the largest theoretical Linux UID at chown with EINVAL; this
fails safely. IDs must also be supported by the host filesystem/user mapping.

## Isolated candidate deployment

Five SQLite online backups (the three active application databases and two
existing test/backup databases) passed `PRAGMA quick_check` under
`/home/docker/arr-maintenance/backups/pullarr-permissions-20261106T005250Z`.
Only Pullarr was recreated using the existing base and candidate Compose files.
Mangarr, NextPanel and qBittorrent container IDs remained unchanged.

Pullarr remains healthy as `arr-maintenance/pullarr:candidate`, image
`sha256:2e374c48f66cd926e8c65ddd139484e6d9438235974138183e9ec0e4cda0ea9b`,
with the default root identity/022 mask preserved for the integrated stack.
The opt-in identities were verified in disposable containers using the same image.

`check_suika.py` passed all endpoints, VPN/proxy egress, both qBittorrent
connections, empty queues, both app-to-NextPanel webhooks, both NextPanel-to-app
connections and root folders. Candidate Pullarr source checks passed ComicVine
(3 results), Metron, and GetComics (3 series, 21 releases). Startup and integration
logs contained no tracebacks or error-level messages.

The shared `source_smoke.py` still aborts its Mangarr portion on its pre-existing
missing `webtoons` query entry. The initial Mangarr metadata and five source
contracts passed before that helper error. Remaining source contracts were
checked separately without changing Mangarr or bypassing provider controls:
Webtoons, MangaFire, Atsumaru, VIZ, Wikipedia and Nyaa all passed. Only one
representative page per downloadable source was fetched; no comic was downloaded.

Evidence remains in `/tmp/pullarr-permissions-*` locally and
`/home/docker/arr-maintenance/pullarr-permissions-*` on Suika, with disposable
runtime data/scripts under its `smoke-tests` directory. No production changes,
image publication, push or queue additions were performed. The Unraid template
was parsed, but no production Unraid/SMB installation was changed or tested.
