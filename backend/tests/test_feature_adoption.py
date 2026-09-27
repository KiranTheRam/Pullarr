"""Behavior that protects library files and per-issue choices."""

import zipfile

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr.api.library_editor import _change_root, _load
from pullarr.api.library_import import _candidate_dirs, _conflicts, _identity
from pullarr.jobs.tasks import _mark_imported
from pullarr.library.importer import ImportedFile
from pullarr.library.move import MoveError
from pullarr.library.scanner import scan_series
from pullarr.models import Base, Download, DownloadKind, Issue, RootFolder, Series
from pullarr.monitoring import apply_mode, issue_wanted, resolve_initial_future


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
