"""Persistence helpers for observable background work."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Job, JobKind, JobStatus, utcnow


async def create_job(
    session: AsyncSession,
    kind: JobKind,
    *,
    series_id: int | None = None,
    detail: str = "",
    payload: str = "",
) -> Job:
    job = Job(
        kind=kind,
        status=JobStatus.QUEUED,
        series_id=series_id,
        detail=detail,
        payload=payload,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def update_job(
    session: AsyncSession,
    job_id: int | None,
    *,
    status: JobStatus | None = None,
    phase: str | None = None,
    progress: float | None = None,
    detail: str | None = None,
    error: str | None = None,
) -> Job | None:
    if job_id is None:
        return None
    job = await session.get(Job, job_id)
    if job is None:
        return None
    if status is not None:
        job.status = status
        if status == JobStatus.RUNNING and job.started_at is None:
            job.started_at = utcnow()
        if status in (JobStatus.DONE, JobStatus.FAILED):
            job.finished_at = utcnow()
    if phase is not None:
        job.phase = phase
    if progress is not None:
        job.progress = max(0.0, min(progress, 1.0))
    if detail is not None:
        job.detail = detail
    if error is not None:
        job.error = error[:1000]
    await session.commit()
    return job


async def recover_interrupted_jobs(session: AsyncSession) -> int:
    """Close work that cannot safely survive a process restart."""
    result = await session.execute(
        select(Job).where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
    )
    jobs = result.scalars().all()
    for job in jobs:
        job.status = JobStatus.FAILED
        job.phase = "interrupted"
        job.error = "Pullarr restarted before this job completed; run it again"
        job.finished_at = utcnow()
    if jobs:
        await session.commit()
    return len(jobs)
