"""Behavior that protects library files and per-issue choices."""

import asyncio
import zipfile
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr.api import library_import as import_api
from pullarr.api.library_editor import _change_root, _load
from pullarr.api.library_import import _candidate_dirs, _conflicts, _identity
from pullarr.jobs import tasks
from pullarr.jobs.tasks import _mark_imported
from pullarr.library.importer import ImportedFile
from pullarr.library.move import MoveError
from pullarr.library.scanner import scan_series
from pullarr.models import Base, Download, DownloadKind, Issue, RootFolder, Series
from pullarr.monitoring import apply_mode, issue_wanted, resolve_initial_future
from pullarr.schemas import ImportItemIn, LibraryImportIn


def test_future_mode_resolves_once_and_keeps_manual_issue_choices():
    series = Series(title="Batman", monitored=True, monitor_mode="future")
    series.issues = [Issue(number=1), Issue(number=2)]
    resolve_initial_future(series, series.issues)
    assert series.monitor_from == 2
    assert [issue.monitored for issue in series.issues] == [False, False]
    series.issues[0].monitored = True  # an explicit per-issue override
    assert issue_wanted(series, 3)
    assert series.issues[0].monitored
    series.monitor_mode = "from_issue"
    series.monitor_from = 2
    apply_mode(series, series.issues)
    assert [issue.monitored for issue in series.issues] == [False, True]


def test_provenance_tracks_import_and_clears_on_disk_replacement(tmp_path):
    folder = tmp_path / "Batman"
    folder.mkdir()
    first = folder / "Batman #001.cbz"
    with zipfile.ZipFile(first, "w") as archive:
        archive.writestr("001.png", b"image")
    issue = Issue(id=1, series_id=1, number=1, display_number="1")
    series = Series(id=1, title="Batman", alt_titles="", issues=[issue])
    download = Download(id=7, kind=DownloadKind.DIRECT, source_name="getcomics")
    _mark_imported(series, [ImportedFile(dest=first, issue=issue, volume=None, covered=[issue])], download)
    assert (issue.file_source, issue.file_download_id) == ("getcomics", 7)
    later = Download(id=8, kind=DownloadKind.DIRECT, source_name="another")
    _mark_imported(series, [ImportedFile(dest=first, issue=issue, volume=None,
                                         covered=[issue], status="duplicate")], later)
    assert (issue.file_source, issue.file_download_id) == ("getcomics", 7)
    scan_series(series, [issue], [folder])
    assert issue.file_source == "getcomics"  # same file
    first.unlink()
    second = folder / "Batman #1.cbz"
    with zipfile.ZipFile(second, "w") as archive:
        archive.writestr("001.png", b"image")
    scan_series(series, [issue], [folder])
    assert issue.file_path == str(second)
    assert (issue.file_source, issue.file_download_id) == ("", None)


@pytest.mark.asyncio
async def test_root_move_repoints_issue_and_keeps_external_extra_folder(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(); new.mkdir()
    source = old / "Batman (2024)"
    source.mkdir()
    media = source / "Batman #1.cbz"
    media.write_bytes(b"content")
    external = old / "Extras"
    external.mkdir()
    async with Session() as session:
        old_root, new_root = RootFolder(path=str(old)), RootFolder(path=str(new))
        series = Series(title="Batman", folder_name=source.name, root_folder=old_root,
                        extra_folders=[], issues=[Issue(number=1, display_number="1",
                        downloaded=True, file_path=str(media), file_source="getcomics")])
        from pullarr.models import SeriesFolder
        series.extra_folders.append(SeriesFolder(path="Extras"))
        session.add_all([series, new_root])
        await session.commit()
        loaded = await _load(session, series.id)
        assert await _change_root(session, loaded, new_root, True)
        assert not source.exists()
        assert (new / source.name / media.name).exists()
        assert loaded.issues[0].file_path == str(new / source.name / media.name)
        assert loaded.issues[0].file_source == "getcomics"
        assert loaded.extra_folders[0].path == str(external)
    await engine.dispose()


@pytest.mark.asyncio
async def test_root_move_collision_leaves_source_and_database_unchanged(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(); new.mkdir()
    (old / "Batman").mkdir(); (new / "Batman").mkdir()
    async with Session() as session:
        series = Series(title="Batman", folder_name="Batman", root_folder=RootFolder(path=str(old)))
        new_root = RootFolder(path=str(new))
        session.add_all([series, new_root])
        await session.commit()
        loaded = await _load(session, series.id)
        with pytest.raises(MoveError):
            await _change_root(session, loaded, new_root, True)
        assert loaded.root_folder_id != new_root.id
        assert (old / "Batman").exists()
    await engine.dispose()


def test_import_folder_identity_excludes_alternate_mount_and_claimed_descendants(tmp_path):
    root = tmp_path / "comics"
    root.mkdir()
    series = root / "Batman"
    series.mkdir()
    with zipfile.ZipFile(series / "Batman #1.cbz", "w") as archive:
        archive.writestr("001.png", b"image")
    alias = tmp_path / "alias"
    alias.symlink_to(series, target_is_directory=True)
    claimed = {_identity(series)}
    assert _conflicts(alias, claimed, set())
    assert _candidate_dirs(root, claimed, set()) == []


def test_import_folder_discovery_distinguishes_series_and_publisher_subfolders(tmp_path):
    root = tmp_path / "comics"
    issue_folder = root / "Batman (2024)" / "Issue 001"
    issue_folder.mkdir(parents=True)
    publisher_folder = root / "Marvel" / "X-Men (2025)"
    publisher_folder.mkdir(parents=True)
    for folder in (issue_folder, publisher_folder):
        with zipfile.ZipFile(folder / "Issue #1.cbz", "w") as archive:
            archive.writestr("001.png", b"image")
    names = {item.folder_name for item in _candidate_dirs(root, set(), set())}
    assert names == {"Batman (2024)", "Marvel/X-Men (2025)"}


def test_import_folder_discovery_keeps_untracked_publisher_siblings(tmp_path):
    root = tmp_path / "comics"
    tracked = root / "Marvel" / "Batman"
    untracked = root / "Marvel" / "X-Men"
    tracked.mkdir(parents=True)
    untracked.mkdir()
    for folder in (tracked, untracked):
        with zipfile.ZipFile(folder / "Issue #1.cbz", "w") as archive:
            archive.writestr("001.png", b"image")
    claimed = {_identity(tracked)}
    ancestors = {_identity(parent) for parent in tracked.parents}

    names = {item.folder_name for item in _candidate_dirs(root, claimed, ancestors)}

    assert names == {"Marvel/X-Men"}


@pytest.mark.asyncio
async def test_import_batch_continues_after_stale_folder_and_starts_prior_jobs(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    root_path = tmp_path / "comics"
    root_path.mkdir()
    for name in ("Batman", "Superman"):
        (root_path / name).mkdir()
    scheduled = []

    async def fake_create_series(body, session):
        series = Series(comicvine_id=body.comicvine_id, title=body.folder_name,
                        root_folder_id=body.root_folder_id, folder_name=body.folder_name)
        session.add(series)
        await session.commit()
        return series

    async def fake_refresh(series_id, *, grab_missing, job_id, force_scan):
        scheduled.append((series_id, force_scan))

    monkeypatch.setattr(import_api, "create_series_record", fake_create_series)
    monkeypatch.setattr(import_api, "refresh_series_full", fake_refresh)
    async with Session() as session:
        root = RootFolder(path=str(root_path))
        session.add(root)
        await session.commit()
        body = LibraryImportIn(root_folder_id=root.id, items=[
            ImportItemIn(folder_name="Batman", comicvine_id=1),
            ImportItemIn(folder_name="Gone", comicvine_id=2),
            ImportItemIn(folder_name="Superman", comicvine_id=3),
        ])
        results = await import_api.import_library(body, session)
        await asyncio.sleep(0)
        series_ids = (await session.execute(select(Series.id).order_by(Series.id))).scalars().all()

    assert [result.status for result in results] == ["added", "failed", "added"]
    assert [series_id for series_id, _ in scheduled] == series_ids
    assert all(force_scan for _, force_scan in scheduled)
    await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_import_scans_even_when_automatic_scans_are_disabled(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    root_path = tmp_path / "comics"
    folder = root_path / "Batman"
    folder.mkdir(parents=True)
    archive_path = folder / "Batman #1.cbz"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("001.png", b"image")
    async with Session() as session:
        series = Series(title="Batman", alt_titles="", folder_name="Batman",
                        root_folder=RootFolder(path=str(root_path)),
                        issues=[Issue(number=1, display_number="1")])
        session.add(series)
        await session.commit()

        @asynccontextmanager
        async def use_session():
            yield session

        async def no_op(*args, **kwargs):
            pass

        async def settings(_session):
            return {"library_scan_on_add": "false"}

        async def grab(_session, loaded, _values, **kwargs):
            assert loaded.issues[0].downloaded
            return 0

        monkeypatch.setattr(tasks, "session_scope", use_session)
        monkeypatch.setattr(tasks.registry, "apply_settings", settings)
        monkeypatch.setattr(tasks, "refresh_series_metadata", no_op)
        monkeypatch.setattr(tasks, "update_issues", no_op)
        monkeypatch.setattr(tasks, "link_sources", no_op)
        monkeypatch.setattr(tasks, "grab_missing_issues", grab)

        await tasks.refresh_series_full(series.id, grab_missing=True, force_scan=True)
        await session.refresh(series.issues[0])
        assert series.issues[0].downloaded
        assert series.issues[0].file_path == str(archive_path)
    await engine.dispose()
