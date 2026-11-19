"""Serialize filesystem work without blocking the API or sharing DB sessions."""

import asyncio

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from starlette.concurrency import run_in_threadpool

from ..models import Issue

_filesystem_lock = asyncio.Lock()
_FILE_FIELDS = ("downloaded", "file_path", "file_source", "file_download_id", "file_mapped", "file_confirmed")


async def run_library_work(function, *args, **kwargs):
    # Imports, rename and cleanup must not touch the same temporary/destination
    # files concurrently. The single-process app uses one shared worker lane.
    async with _filesystem_lock:
        return await run_in_threadpool(function, *args, **kwargs)


def copy_issues(issues: list[Issue]) -> list[Issue]:
    return [Issue(**{column.key: getattr(issue, column.key) for column in Issue.__table__.columns}) for issue in issues]


def file_state(issue: Issue) -> dict:
    return {key: getattr(issue, key) for key in _FILE_FIELDS}


def apply_file_state(issues: list[Issue], copies: list[Issue]) -> None:
    for issue, copied in zip(issues, copies):
        for key, value in file_state(copied).items():
            setattr(issue, key, value)


async def update_file_state(session: AsyncSession, issue: Issue, **values) -> bool:
    """Apply automatic ownership only while the recorded file state still agrees."""
    original = file_state(issue)
    if all(value == original[key] for key, value in values.items()):
        return True
    result = await session.execute(
        update(Issue)
        .where(Issue.id == issue.id, *(getattr(Issue, key) == value for key, value in original.items()))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    changed = result.rowcount == 1
    if not changed:
        # A concurrent manual mapping or another ownership change won the race.
        current = await session.execute(
            select(*(getattr(Issue, key) for key in _FILE_FIELDS)).where(Issue.id == issue.id)
        )
        values = current.mappings().one_or_none() or {}
    for key, value in values.items():
        set_committed_value(issue, key, value)
    return changed
