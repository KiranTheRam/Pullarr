"""Behavior that protects library files and per-issue choices."""

import asyncio
import zipfile
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr import settings_service
from pullarr.api import library_import as import_api
from pullarr.api.library import map_file, map_file_range, rename_apply, scan
from pullarr.api.library_editor import _change_root, _load
from pullarr.api.series import update_series
from pullarr.api.library_import import _candidate_dirs, _conflicts, _identity
from pullarr.jobs import tasks
from pullarr.jobs.tasks import _mark_imported
from pullarr.library.importer import ImportedFile
from pullarr.library.move import MoveError
from pullarr.library.scanner import scan_series
from pullarr.models import Base, Download, DownloadKind, Issue, RootFolder, Series
from pullarr.monitoring import apply_mode, issue_wanted, resolve_initial_future
from pullarr.schemas import FileMapIn, FileMapRangeIn, ImportItemIn, LibraryImportIn, RenameApplyIn, SeriesUpdateIn


@pytest.fixture
async def ownership_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ownership.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_choosing_folder_under_nested_root_updates_series_root(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    comics = tmp_path / "comics"
    marvel = comics / "Marvel"
    (marvel / "Scream").mkdir(parents=True)
    async with Session() as session:
        parent = RootFolder(path=str(comics))
        nested = RootFolder(path=str(marvel))
        series = Series(title="Absolute Carnage: Scream", folder_name="Marvel/Scream",
                        root_folder=parent)
        session.add_all([series, nested])
        await session.commit()
        await update_series(series.id, SeriesUpdateIn(folder_name=str(marvel / "Scream")), session)
        await session.refresh(series)
        assert series.root_folder_id == nested.id
        assert series.folder_name == "Scream"
    await engine.dispose()


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


async def test_a_late_download_does_not_replace_a_valid_manual_mapping(tmp_path, ownership_session):
    session = ownership_session
    preferred = tmp_path / "My issue.cbz"
    preferred.touch()
    issue = Issue(id=1, number=1, downloaded=True, file_mapped=True, file_path=str(preferred))
    series = Series(title="Batman", issues=[issue])
    session.add(series)
    await session.commit()
    dest = tmp_path / "Batman #001.cbz"
    imported = [ImportedFile(dest=dest, issue=issue, volume=None, covered=[issue])]
    assert await _mark_imported(session, series, imported) == 0
    assert issue.file_path == str(preferred) and issue.file_mapped

    preferred.unlink()
    assert await _mark_imported(session, series, imported) == 1
    assert issue.file_path == str(dest) and not issue.file_mapped


@pytest.mark.parametrize("same_file", [False, True])
async def test_mapping_saved_after_a_download_loads_the_issue_wins(tmp_path, ownership_session, same_file):
    session = ownership_session
    issue = Issue(number=1)
    series = Series(title="Batman", issues=[issue])
    session.add(series)
    await session.commit()
    dest = tmp_path / "Batman #001.cbz"
    preferred = dest if same_file else tmp_path / "My issue.cbz"
    preferred.touch()
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as other:
        await map_file(series.id, FileMapIn(file_path=str(preferred), issue_id=issue.id), other)
    imported = [ImportedFile(dest=dest, issue=issue, volume=None, covered=[issue])]
    assert await _mark_imported(session, series, imported) == int(same_file)
    await session.commit()
    await session.refresh(issue)
    assert issue.file_path == str(preferred) and issue.file_mapped


async def test_provenance_tracks_import_and_clears_on_disk_replacement(tmp_path, ownership_session):
    session = ownership_session
    folder = tmp_path / "Batman"
    folder.mkdir()
    first = folder / "Batman #001.cbz"
    with zipfile.ZipFile(first, "w") as archive:
        archive.writestr("001.png", b"image")
    issue = Issue(id=1, series_id=1, number=1, display_number="1")
    series = Series(id=1, title="Batman", alt_titles="", issues=[issue])
    session.add(series)
    await session.commit()
    download = Download(id=7, kind=DownloadKind.DIRECT, source_name="getcomics")
    await _mark_imported(session, series, [ImportedFile(dest=first, issue=issue, volume=None, covered=[issue])], download)
    assert (issue.file_source, issue.file_download_id) == ("getcomics", 7)
    later = Download(id=8, kind=DownloadKind.DIRECT, source_name="another")
    await _mark_imported(session, series, [ImportedFile(dest=first, issue=issue, volume=None,
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


async def test_scan_keeps_manual_pdf_range_until_an_exact_file_arrives(tmp_path, ownership_session):
    session = ownership_session
    folder = tmp_path / "Batman"
    folder.mkdir()
    pdf = folder / "Collected Stories #001-002.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    issues = [Issue(number=n, display_number=str(n)) for n in (1, 2)]
    series = Series(title="Batman", folder_name=folder.name, issues=issues,
                    root_folder=RootFolder(path=str(tmp_path)))
    session.add(series)
    await session.commit()
    await map_file_range(series.id, FileMapRangeIn(file_path=str(pdf), from_number=1, to_number=2), session)

    assert (await scan(series.id, session)).cleared == 0
    assert all(issue.file_path == str(pdf) and issue.downloaded for issue in issues)

    exact = folder / "[Batman] #002.pdf"
    exact.write_bytes(b"%PDF-1.7\n")
    assert (await scan(series.id, session)).cleared == 0
    assert [issue.file_path for issue in issues] == [str(pdf), str(exact)]
    assert not issues[1].file_confirmed
    assert pdf.exists()


async def test_rescan_keeps_a_pdf_renamed_with_a_custom_template(tmp_path, ownership_session):
    session = ownership_session
    folder = tmp_path / "Batman"
    folder.mkdir()
    pdf = folder / "Batman #001.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    issue = Issue(number=1, display_number="1", title="The Case Begins")
    series = Series(title="Batman", folder_name=folder.name, issues=[issue],
                    root_folder=RootFolder(path=str(tmp_path)))
    session.add(series)
    await session.commit()
    assert (await scan(series.id, session)).matched_issues == 1

    await settings_service.set_many(session, {"naming_template": "{title} #{issue:03d}"})
    assert [outcome.status for outcome in await rename_apply(series.id, RenameApplyIn(), session)] == ["renamed"]
    assert (await scan(series.id, session)).cleared == 0
    assert issue.downloaded and issue.file_path == str(folder / "The Case Begins #001.pdf")


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


def test_import_folder_discovery_offers_pdf_only_series(tmp_path):
    series = tmp_path / "Batman (2024)"
    extras = series / "Extras"
    extras.mkdir(parents=True)
    (series / "Batman #1.PDF").write_bytes(b"%PDF-1.7\n")
    (extras / "Sketchbook.pdf").write_bytes(b"%PDF-1.7\n")

    assert [(item.folder_name, item.file_count) for item in _candidate_dirs(tmp_path, set(), set())] == [
        ("Batman (2024)", 2),
    ]


def test_import_folder_discovery_offers_numbered_pdf_with_empty_subfolder(tmp_path):
    series = tmp_path / "Batman (2024)"
    (series / "Extras").mkdir(parents=True)
    comic = series / "001.pdf"
    comic.touch()

    assert _candidate_dirs(tmp_path, set(), set()) == []

    comic.write_bytes(b"%PDF-1.7\n")
    assert [item.folder_name for item in _candidate_dirs(tmp_path, set(), set())] == ["Batman (2024)"]


@pytest.mark.parametrize("claimed_child", [False, True])
@pytest.mark.parametrize("document", ["Reading Order.pdf", "DC.pdf"])
def test_publisher_pdf_does_not_hide_child_series(tmp_path, claimed_child, document):
    publisher = tmp_path / "DC"
    folders = [publisher / "Batman", publisher / "Superman"]
    for folder in folders:
        folder.mkdir(parents=True)
        (folder / f"{folder.name} #001.pdf").write_bytes(b"%PDF-1.7\n")
    (publisher / document).write_bytes(b"%PDF-1.7\n")
    claimed = {_identity(folders[0])} if claimed_child else set()
    ancestors = {_identity(p) for p in folders[0].parents} if claimed_child else set()

    expected = ["DC/Superman"] if claimed_child else ["DC/Batman", "DC/Superman"]
    assert [item.folder_name for item in _candidate_dirs(tmp_path, claimed, ancestors)] == expected


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
@pytest.mark.parametrize("suffix", [".cbz", ".pdf"])
async def test_explicit_import_scans_even_when_automatic_scans_are_disabled(tmp_path, monkeypatch, suffix):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    root_path = tmp_path / "comics"
    folder = root_path / "Batman"
    folder.mkdir(parents=True)
    comic_path = folder / f"Batman #1{suffix}"
    if suffix == ".cbz":
        with zipfile.ZipFile(comic_path, "w") as archive:
            archive.writestr("001.png", b"image")
    else:
        comic_path.write_bytes(b"%PDF-1.7\n")
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
        assert series.issues[0].file_path == str(comic_path)
    await engine.dispose()
