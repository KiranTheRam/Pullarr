"""Coordinate queue mutations with an import's full database/filesystem lifetime."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from weakref import WeakValueDictionary

_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
_importing_downloads: set[int] = set()


@asynccontextmanager
async def queue_mutation(ids):
    # Stable ordering also makes batch removals safe. Idle locks can be collected.
    async with AsyncExitStack() as stack:
        for download_id in sorted(set(ids)):
            lock = _locks.setdefault(download_id, asyncio.Lock())
            await stack.enter_async_context(lock)
        yield


@asynccontextmanager
async def importing_download(download_id):
    async with queue_mutation([download_id]):
        _importing_downloads.add(download_id)
        try:
            yield
        finally:
            _importing_downloads.discard(download_id)
