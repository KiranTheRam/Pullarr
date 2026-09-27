"""Serialize filesystem work without blocking the API or sharing DB sessions."""

import asyncio

from starlette.concurrency import run_in_threadpool

from ..models import Issue

_filesystem_lock = asyncio.Lock()


async def run_library_work(function, *args, **kwargs):
    # Imports, rename and cleanup must not touch the same temporary/destination
    # files concurrently. The single-process app uses one shared worker lane.
    async with _filesystem_lock:
        return await run_in_threadpool(function, *args, **kwargs)


def copy_issues(issues: list[Issue]) -> list[Issue]:
    return [Issue(**{column.key: getattr(issue, column.key) for column in Issue.__table__.columns}) for issue in issues]


def apply_file_state(issues: list[Issue], copies: list[Issue]) -> None:
    for issue, copied in zip(issues, copies):
        issue.downloaded = copied.downloaded
        issue.file_path = copied.file_path
        issue.file_source = copied.file_source
        issue.file_download_id = copied.file_download_id
