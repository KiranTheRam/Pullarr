"""Runtime-editable settings stored in the Settings table."""

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
    # GetComics
    "getcomics_base_url": "https://getcomics.org",
    # Optional HTTP(S) proxy for all GetComics traffic (searches + file
    # downloads) — point it at a VPN-side proxy (e.g. Privoxy on a
    # qbittorrentvpn container) to route downloads through the VPN
    "getcomics_proxy": "",
    # Where DDL files land before import; empty → <data dir>/ddl
    "ddl_directory": "",
    # qBittorrent (optional, for manual magnet grabs)
    "qbittorrent_url": "http://localhost:8080",
    "qbittorrent_username": "admin",
    "qbittorrent_password": "",
    "qbittorrent_category": "pullarr",
    "qbittorrent_enabled": "false",
    # Sources on/off
    "source_getcomics_enabled": "true",
    # Jobs
    "monitor_interval_minutes": "60",
    # Library
    "library_scan_on_add": "true",  # adopt existing on-disk files on add/refresh
}

SECRET_KEYS = {"comicvine_api_key", "qbittorrent_password"}


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
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        row = await session.get(Setting, key)
        if row is None:
            session.add(Setting(key=key, value=value))
        else:
            row.value = value
    await session.commit()
