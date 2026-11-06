"""Real file-backed SQLite contention and upgrade/rollback coverage."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import pullarr
from pullarr import db
from pullarr.jobs import tasks
from pullarr.models import Base, Download, DownloadKind, DownloadStatus, HistoryEvent, Issue, RootFolder, Series


_PROBE = """
import asyncio, json, sqlite3
from pullarr import db
from pullarr.config import config
async def main():
    await db.init_db()
    # Hold two connections simultaneously: both need the connection-local timeout.
    async with db.engine.connect() as a, db.engine.connect() as b:
        values = [{p: (await c.exec_driver_sql('PRAGMA ' + p)).scalar()
                   for p in ('journal_mode', 'busy_timeout', 'synchronous', 'foreign_keys')}
                  for c in (a, b)]
    await db.engine.dispose()
    with sqlite3.connect(config.db_path) as c:
        values.append({'file_mode': c.execute('PRAGMA journal_mode').fetchone()[0],
                       'integrity': c.execute('PRAGMA integrity_check').fetchone()[0],
                       'title': c.execute('SELECT title FROM series').fetchone()[0]})
    print(json.dumps(values))
asyncio.run(main())
"""


def probe(tmp_path, wal):
    env = dict(os.environ, PULLARR_DATA_DIR=str(tmp_path), PULLARR_SQLITE_WAL=str(wal).lower())
    env['PYTHONPATH'] = str(Path(pullarr.__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, '-c', _PROBE], env=env, cwd=tmp_path,
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def tuned_engine(path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{path}')
    event.listen(engine.sync_engine, 'connect', db._sqlite_pragmas)
    return engine


async def test_app_upgrade_repeated_start_and_rollback_keep_existing_data(tmp_path):
    # An existing rollback-journal install, not an empty database only.
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "pullarr.db"}')
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine)() as session:
            session.add(Series(title='Preserved comic', sort_title='preserved comic'))
            await session.commit()
    finally:
        await engine.dispose()
    for wal in (True, True, False, False, True):
        rows = await asyncio.to_thread(probe, tmp_path, wal)
        wanted = 'wal' if wal else 'delete'
        for row in rows[:2]:
            assert row['journal_mode'] == wanted
            assert row['busy_timeout'] == 30_000
            assert row['synchronous'] == 2  # FULL durability remains intact.
            assert row['foreign_keys'] == 0  # Existing deletion semantics retained.
        assert rows[2] == {'file_mode': wanted, 'integrity': 'ok', 'title': 'Preserved comic'}


async def test_memory_database_retains_supported_journal():
    engine = create_async_engine('sqlite+aiosqlite://')
    event.listen(engine.sync_engine, 'connect', db._sqlite_pragmas)
    try:
        async with engine.connect() as conn:
            assert (await conn.exec_driver_sql('PRAGMA journal_mode')).scalar() == 'memory'
            assert (await conn.exec_driver_sql('PRAGMA busy_timeout')).scalar() == 30_000
    finally:
        await engine.dispose()


async def test_reader_proceeds_and_second_writer_waits_for_commit(tmp_path):
    engine = tuned_engine(tmp_path / 'contention.db')
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql('CREATE TABLE events (id INTEGER PRIMARY KEY)')
            await conn.exec_driver_sql('INSERT INTO events VALUES (1)')
        async with engine.connect() as writer, engine.connect() as other:
            await writer.exec_driver_sql('INSERT INTO events VALUES (2)')
            # A newly opened pooled connection must also initialize during a write.
            async with engine.connect() as reader:
                rows = await asyncio.wait_for(reader.exec_driver_sql('SELECT id FROM events'), 2)
                assert rows.scalars().all() == [1]
            started = asyncio.Event()
            async def write():
                started.set()
                await other.exec_driver_sql('INSERT INTO events VALUES (3)')
                await other.commit()
            pending = asyncio.create_task(write())
            try:
                await started.wait()
                # The actual SQLite writer must wait instead of raising BUSY.
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(pending), 0.1)
                await writer.commit()
                await asyncio.wait_for(pending, 2)
            finally:
                await writer.rollback()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        async with engine.connect() as conn:
            assert (await conn.exec_driver_sql('SELECT id FROM events ORDER BY id')).scalars().all() == [1, 2, 3]
    finally:
        await engine.dispose()


@pytest.mark.parametrize('wal', [True, False])
async def test_throttled_progress_does_not_autoflush_across_a_download_wait(tmp_path, monkeypatch, wal):
    monkeypatch.setattr(db.config, 'sqlite_wal', wal)
    engine = tuned_engine(tmp_path / 'download.db')
    # A short timeout ensures a leaked lock fails quickly rather than hiding it.
    other_engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "download.db"}',
                                       connect_args={'timeout': 0.2})
    maker = async_sessionmaker(engine, expire_on_commit=False)
    other_maker = async_sessionmaker(other_engine, expire_on_commit=False)
    now = [100.0]
    monkeypatch.setattr(tasks, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    values = {'source_fake_enabled': 'true', 'ddl_directory': str(tmp_path / 'staging'),
              'naming_template': '{series} #{issue}'}
    async def apply(session):
        return values
    monkeypatch.setattr(tasks.registry, 'apply_settings', apply)
    monkeypatch.setitem(tasks.registry.DDL_SOURCES, 'fake', object())
    monkeypatch.setattr(tasks, '_source_cooldown_until', {})
    monkeypatch.setattr(tasks.notifications, 'notify_import', lambda *args: None)
    monkeypatch.setattr(tasks, '_notify_kavita', lambda *args: None)
    entered, release = asyncio.Event(), asyncio.Event()
    async def download(source, payload, staging, progress_cb, cancel_cb):
        await progress_cb(1, 4)  # committed
        now[0] += 0.1
        await progress_cb(2, 4)  # throttled
        assert not cancel_cb()
        # Pullarr's cancellation uses an in-memory event. Exercise an ORM read
        # as well to ensure uncommitted progress cannot acquire a write lock.
        await worker_session.execute(select(HistoryEvent))
        entered.set()
        await release.wait()
        await progress_cb(4, 4)  # completion must persist even inside throttle
        payload_dir = tmp_path / 'payload'
        payload_dir.mkdir()
        return payload_dir
    monkeypatch.setattr(tasks, 'download_release', download)
    async def import_payload(*args, **kwargs):
        return [SimpleNamespace(covered=[issue], volume=None, dest=tmp_path / 'comic.cbz', status='imported')]
    monkeypatch.setattr(tasks, '_import_download_payload', import_payload)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with maker() as worker_session:
            series = Series(title='Test', sort_title='test', folder_name='Test',
                            root_folder=RootFolder(path=str(tmp_path)))
            issue = Issue(number=1, monitored=True)
            series.issues.append(issue)
            worker_session.add(series)
            await worker_session.commit()
            dl = Download(series_id=series.id, issue_id=issue.id, kind=DownloadKind.DIRECT,
                          status=DownloadStatus.QUEUED, source_name='fake', payload='release', title='Test #1')
            worker_session.add(dl)
            await worker_session.commit()
            worker = asyncio.create_task(tasks._run_direct_download(worker_session, dl))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                async with other_maker() as other:
                    assert (await other.get(Download, dl.id)).progress == 0.25
                    other.add(HistoryEvent(event='concurrent', detail='settings/monitor writer'))
                    await asyncio.wait_for(other.commit(), 2)
            finally:
                release.set()
                await asyncio.wait_for(worker, 5)
            async with other_maker() as other:
                saved = await other.get(Download, dl.id)
                assert saved.status == DownloadStatus.DONE
                assert saved.progress == 1.0
                assert (await other.get(Issue, issue.id)).downloaded
    finally:
        await engine.dispose()
        await other_engine.dispose()
