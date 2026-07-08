import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..db import session_scope
from .. import settings_service
from .tasks import monitor_all, process_direct_queue, sync_qbittorrent

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def start() -> None:
    async with session_scope() as session:
        raw_interval = await settings_service.get(session, "monitor_interval_minutes") or "60"
        try:
            interval = settings_service.parse_monitor_interval(raw_interval)
        except ValueError:
            interval = settings_service.parse_monitor_interval(
                settings_service.DEFAULTS["monitor_interval_minutes"]
            )
            log.warning("Invalid monitor interval %r; using %d min", raw_interval, interval)

    scheduler.add_job(
        process_direct_queue, "interval", seconds=10,
        id="direct_queue", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        sync_qbittorrent, "interval", seconds=20,
        id="qbt_sync", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        monitor_all, "interval", minutes=interval,
        id="monitor", max_instances=1, coalesce=True,
    )
    scheduler.start()
    log.info("Scheduler started (monitor every %d min)", interval)


def reschedule_monitor(minutes: int) -> None:
    """Apply a new monitor interval without restarting the app."""
    if scheduler.running and scheduler.get_job("monitor"):
        scheduler.reschedule_job("monitor", trigger="interval", minutes=max(1, minutes))
        log.info("Monitor rescheduled to every %d min", minutes)


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
