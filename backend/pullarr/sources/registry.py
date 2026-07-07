"""Central access to configured sources and the metadata provider, honoring
settings (enabled flags, credentials, priority order)."""

from sqlalchemy.ext.asyncio import AsyncSession

from .. import settings_service
from ..metadata.comicvine import provider as comicvine_provider
from .base import DDLSource, TorrentIndexer
from .getcomics import source as getcomics_source

DDL_SOURCES: dict[str, DDLSource] = {
    getcomics_source.name: getcomics_source,
}
TORRENT_INDEXERS: dict[str, TorrentIndexer] = {}
ALL_SOURCE_NAMES = [*DDL_SOURCES, *TORRENT_INDEXERS]


async def apply_settings(session: AsyncSession) -> dict[str, str]:
    """Push runtime settings into source/provider instances; returns the
    settings dict."""
    values = await settings_service.get_all(session)
    comicvine_provider.configure(values["comicvine_api_key"])
    getcomics_source.configure(
        base_url=values["getcomics_base_url"],
        proxy=values["getcomics_proxy"],
    )
    return values


def enabled_ddl_sources(values: dict[str, str]) -> list[DDLSource]:
    order = [s.strip() for s in values["source_priority"].split(",") if s.strip()]
    sources = [
        DDL_SOURCES[name]
        for name in order
        if name in DDL_SOURCES and values.get(f"source_{name}_enabled") == "true"
    ]
    # include any enabled source missing from the priority string
    for name, src in DDL_SOURCES.items():
        if src not in sources and values.get(f"source_{name}_enabled") == "true":
            sources.append(src)
    return sources


def enabled_torrent_indexers(values: dict[str, str]) -> list[TorrentIndexer]:
    return [
        idx
        for name, idx in TORRENT_INDEXERS.items()
        if values.get(f"source_{name}_enabled") == "true"
    ]
