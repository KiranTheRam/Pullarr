"""Runtime-editable settings stored in the Settings table."""

from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Setting

DEFAULTS: dict[str, str] = {
    # Media management: {series}, {year}, {issue}, {title} placeholders
    "naming_template": "{series} #{issue:03d}",
    # Source priority: comma-separated source names, first = preferred
    "source_priority": "getcomics",
    # ComicVine metadata (free API key from comicvine.gamespot.com/api)
    "comicvine_api_key": "",
    # Optional richer metadata enrichment. Metron uses HTTP Basic auth.
    "metron_enabled": "false",
    "metron_username": "",
    "metron_password": "",
    "metron_issue_enrichment_limit": "5",
    "torznab_indexers": "[]",
    "torrent_query_templates": "{series} {issue}",
    # GetComics
    "getcomics_base_url": "https://getcomics.org",
    # Optional HTTP(S) proxy for all GetComics traffic (searches + file
    # downloads) — point it at a VPN-side proxy (e.g. Privoxy on a
    # qbittorrentvpn container) to route downloads through the VPN
    "getcomics_proxy": "",
    # Ordered download-host preference for links exposed by GetComics.
    "getcomics_service_preference": "main,pixeldrain,mediafire",
    # Where DDL files land before import; empty → <data dir>/ddl
    "ddl_directory": "",
    # Torrent download clients (optional)
    "torrent_download_client": "qbittorrent",
    "qbittorrent_url": "http://localhost:8080",
    "qbittorrent_username": "admin",
    "qbittorrent_password": "",
    "qbittorrent_category": "pullarr",
    "qbittorrent_enabled": "false",
    "transmission_url": "http://localhost:9091/transmission/rpc",
    "transmission_username": "",
    "transmission_password": "",
    "transmission_download_dir": "",
    "transmission_enabled": "false",
    # Sources on/off
    "source_getcomics_enabled": "true",
    # Jobs
    "monitor_interval_minutes": "60",
    "finished_series_check_days": "7",
    "download_retry_attempts": "4",
    # Library
    "library_scan_on_add": "true",  # adopt existing on-disk files on add/refresh
    # Outbound webhook fired when issues are imported (e.g. NextPanel's
    # /api/v1/webhooks/pullarr endpoint). Secret is sent as X-Webhook-Secret.
    "webhook_enabled": "false",
    "webhook_url": "",
    "webhook_secret": "",
    # Kavita: ask the reader to scan after imports so new issues appear
    # without waiting for its own scheduled scan.
    "kavita_enabled": "false",
    "kavita_url": "",
    "kavita_api_key": "",
    # "series" scans just the affected series when Kavita already knows it
    # (falling back to its library otherwise); "library" always scans whole.
    "kavita_scan_mode": "series",
    # JSON {"<root_folder_id>": <kavita library id>}; empty = match by path
    "kavita_library_map": "",
}

SECRET_KEYS = {
    "comicvine_api_key", "metron_password", "qbittorrent_password", "webhook_secret",
    "kavita_api_key", "transmission_password",
}


def parse_monitor_interval(value: str) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Monitor interval must be a whole number of minutes") from exc
    if minutes < 1:
        raise ValueError("Monitor interval must be at least 1 minute")
    if minutes > 10080:
        raise ValueError("Monitor interval must be no more than 10080 minutes")
    return minutes


def validate_updates(values: dict[str, str]) -> dict[str, str]:
    from .library.naming import validate_template

    cleaned: dict[str, str] = {}
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        value = "" if value is None else str(value)
        if key == "torznab_indexers":
            from .sources.torznab import validate_indexers
            value = validate_indexers(value)
        elif key == "torrent_query_templates":
            from .sources.queries import validate_templates
            validate_templates(value, required=True)
        elif key == "monitor_interval_minutes":
            value = str(parse_monitor_interval(value))
        elif key == "finished_series_check_days":
            try:
                days = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("Finished series check days must be a whole number") from exc
            if not 1 <= days <= 365:
                raise ValueError("Finished series check days must be between 1 and 365")
            value = str(days)
        elif key == "naming_template":
            try:
                validate_template(value)
            except ValueError as exc:
                raise ValueError(f"Invalid naming template: {exc}") from exc
        elif key == "torrent_download_client":
            from .download.clients import CLIENT_NAMES

            if value not in CLIENT_NAMES:
                raise ValueError("Choose qBittorrent or Transmission for new torrent downloads")
        elif key == "transmission_url":
            from .download.transmission import validate_rpc_url

            value = validate_rpc_url(value)
        elif key == "transmission_download_dir" and value:
            path = PurePosixPath(value)
            if not path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
                raise ValueError("Transmission download directory must be an absolute path")
        elif key == "qbittorrent_category" and not value.strip():
            raise ValueError("qBittorrent category cannot be empty")
        elif key in {"download_retry_attempts", "metron_issue_enrichment_limit"}:
            try:
                numeric = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key.replace('_', ' ').title()} must be a whole number") from exc
            maximum = 10 if key == "download_retry_attempts" else 25
            if not 0 <= numeric <= maximum:
                raise ValueError(f"{key.replace('_', ' ').title()} must be between 0 and {maximum}")
            value = str(numeric)
        elif key == "getcomics_service_preference":
            allowed = {"main", "pixeldrain", "mediafire"}
            order = [v.strip().lower() for v in value.split(",") if v.strip()]
            unknown = [v for v in order if v not in allowed]
            if unknown:
                raise ValueError(f"Unknown GetComics download service: {unknown[0]}")
            value = ",".join(dict.fromkeys(order)) or "main,pixeldrain,mediafire"
        cleaned[key] = value
    return cleaned


async def get_all(session: AsyncSession) -> dict[str, str]:
    rows = (await session.execute(select(Setting))).scalars().all()
    values = dict(DEFAULTS)
    values.update({r.key: r.value for r in rows if r.key in DEFAULTS})
    return values


async def get(session: AsyncSession, key: str) -> str:
    row = await session.get(Setting, key)
    if row is not None:
        return row.value
    return DEFAULTS.get(key, "")


async def set_many(session: AsyncSession, values: dict[str, str]) -> None:
    # Older installations can keep saving other settings until they edit
    # their existing template, even if it fails the newer validation.
    if "naming_template" in values and values["naming_template"] == await get(session, "naming_template"):
        values = {key: value for key, value in values.items() if key != "naming_template"}
    cleaned = validate_updates(values)
    if any(key.startswith("kavita_") for key in cleaned):
        # "enabled needs a URL and key" spans several settings, and the page
        # may submit only the one that changed — check the resulting state
        from .kavita import validate_settings as validate_kavita

        validate_kavita({**await get_all(session), **cleaned})
    for key, value in cleaned.items():
        row = await session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
    await session.commit()
