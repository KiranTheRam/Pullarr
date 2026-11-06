"""Completed torrents must never import their neighbours or overwrite another issue."""

import zipfile
from contextlib import asynccontextmanager

import pytest
import respx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr.jobs import tasks
from pullarr.download.archive import ImportValidationError
from pullarr.library.importer import import_payload
from pullarr.library.naming import DEFAULT_TEMPLATE
from pullarr.models import Base, Download, DownloadKind, DownloadStatus, Issue, RootFolder, Series

BASE = "http://qbt:8080"
HASH = "c" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
VALUES = {
    "qbittorrent_enabled": "true",
    "qbittorrent_url": BASE,
    "qbittorrent_username": "admin",
    "qbittorrent_password": "pw",
    "naming_template": DEFAULT_TEMPLATE,
}


def cbz(path, tag=b"A"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", PNG + tag)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "library"
    root.mkdir()
    async with maker() as session:
        series = Series(title="Test Series", sort_title="test series",
                        root_folder=RootFolder(path=str(root)), folder_name="Test Series")
        series.issues = [Issue(number=float(n), downloaded=False) for n in (1, 2, 3, 12, 12.5)]
        session.add(series)
        await session.commit()
        dl = Download(series_id=series.id, kind=DownloadKind.TORRENT,
                      status=DownloadStatus.DOWNLOADING, title="Test Series #3", torrent_hash=HASH)
        session.add(dl)
        await session.commit()

        @asynccontextmanager
        async def scope():
            yield session

        async def apply_settings(session):
            return dict(VALUES)

        monkeypatch.setattr(tasks, "session_scope", scope)
        monkeypatch.setattr(tasks.registry, "apply_settings", apply_settings)
        monkeypatch.setattr(tasks.notifications, "notify_import", lambda *a: None)
        monkeypatch.setattr(tasks, "_notify_kavita", lambda *a: None)
        yield session, series, dl, root / "Test Series"
    await engine.dispose()


async def sync(env, content_path, save_path, files, *, files_status=200):
    session, series, dl, library = env
    with respx.mock(base_url=f"{BASE}/api/v2") as qbt:
        qbt.post("/auth/login").respond(200, text="Ok.")
        qbt.get("/torrents/info", params={"hashes": HASH}).respond(json=[{
            "hash": HASH, "name": dl.title, "progress": 1.0, "state": "stalledUP",
            "category": "pullarr", "content_path": str(content_path), "save_path": str(save_path),
        }])
        qbt.get("/torrents/files", params={"hash": HASH}).respond(files_status, json=[
            {"name": name, "priority": priority} for name, priority in files
        ])
        await tasks.sync_qbittorrent()
    await session.refresh(dl)
    await session.refresh(series, ["issues"])
    return {issue.number for issue in series.issues if issue.downloaded}


async def test_no_subfolder_imports_only_torrent_archives(env, tmp_path):
    category = tmp_path / "downloads"
    cbz(category / "Other Series" / "Other Series #1.cbz")
    cbz(category / "Other Series #2.cbz")
    cbz(category / "Test Series #3.cbz")
    numbers = await sync(env, category, category, [
        ("Test Series #3.cbz", 1), ("Test Series #12.cbz", 0),
    ])
    assert numbers == {3.0}
    assert env[2].status == DownloadStatus.DONE, env[2].error
    assert [p.name for p in env[3].iterdir()] == ["Test Series #003.cbz"]
    assert (category / "Test Series #3.cbz").exists()  # retain seeding payload


async def test_shared_loose_image_folder_packs_only_selected_pages(env, tmp_path):
    category = tmp_path / "downloads"
    pages = category / "Test Series #3"
    pages.mkdir(parents=True)
    for name in ("01.png", "02.png", "neighbour.png", "skipped.png"):
        (pages / name).write_bytes(PNG)
    numbers = await sync(env, category, category, [
        ("Test Series #3/01.png", 1), ("Test Series #3/02.png", 1),
        ("Test Series #3/skipped.png", 0),
    ])
    assert numbers == {3.0}
    with zipfile.ZipFile(env[3] / "Test Series #003.cbz") as archive:
        assert set(archive.namelist()) == {"01.png", "02.png", "ComicInfo.xml"}
    # The existing-destination preflight must also compare only the selected pages.
    env[2].status = DownloadStatus.DOWNLOADING
    await env[0].commit()
    assert await sync(env, category, category, [
        ("Test Series #3/01.png", 1), ("Test Series #3/02.png", 1),
    ]) == {3.0}
    assert env[2].status == DownloadStatus.DONE, env[2].error


async def test_loose_pages_at_category_root_exclude_neighbour_pages(env, tmp_path):
    category = tmp_path / "downloads"
    category.mkdir()
    (category / "01.png").write_bytes(PNG)
    (category / "neighbour.png").write_bytes(PNG + b"other torrent")
    env[2].issue_id = next(issue.id for issue in env[1].issues if issue.number == 3.0)
    await env[0].commit()
    assert await sync(env, category, category, [("01.png", 1)]) == {3.0}
    with zipfile.ZipFile(env[3] / "Test Series #003.cbz") as archive:
        assert set(archive.namelist()) == {"01.png", "ComicInfo.xml"}


@pytest.mark.parametrize("subfolder", [False, True])
async def test_single_file_and_subfolder_layouts(env, tmp_path, subfolder):
    category = tmp_path / "downloads"
    name = "pack/Test Series #3.cbz" if subfolder else "Test Series #3.cbz"
    cbz(category / name)
    content = category / "pack" if subfolder else category / name
    assert await sync(env, content, category, [(name, 1)]) == {3.0}
    assert env[2].status == DownloadStatus.DONE, env[2].error


async def test_missing_listed_media_waits_and_imports_nothing(env, tmp_path):
    category = tmp_path / "downloads"
    cbz(category / "Other Series #1.cbz")
    assert await sync(env, category, category, [("Test Series #3.cbz", 1)]) == set()
    assert env[2].status == DownloadStatus.DOWNLOADING
    assert "content moved during import; retrying" in env[2].error
    assert not list(env[3].glob("*.cbz"))


async def test_missing_sidecar_does_not_block_media(env, tmp_path):
    category = tmp_path / "downloads"
    cbz(category / "Test Series #3.cbz")
    assert await sync(env, category, category, [
        ("Test Series #3.cbz", 1), ("deleted.nfo", 1),
    ]) == {3.0}
    assert env[2].status == DownloadStatus.DONE, env[2].error


@pytest.mark.parametrize("files", [[], [("Test Series #3.cbz", 0)]])
async def test_empty_selected_list_fails_without_walking_folder(env, tmp_path, files):
    category = tmp_path / "downloads"
    cbz(category / "Other Series #1.cbz")
    assert await sync(env, category, category, files) == set()
    assert env[2].status == DownloadStatus.FAILED
    assert "no files" in env[2].error
    assert not env[3].exists()


async def test_file_list_api_failure_waits_without_walking_folder(env, tmp_path):
    category = tmp_path / "downloads"
    cbz(category / "Other Series #1.cbz")
    assert await sync(env, category, category, [], files_status=503) == set()
    assert env[2].status == DownloadStatus.DOWNLOADING
    assert "waiting for torrent file list" in env[2].error
    assert not env[3].exists()


@pytest.mark.parametrize("name", ["../outside.cbz", "/outside.cbz", r"C:\outside.cbz"])
async def test_unsafe_file_list_fails_closed(env, tmp_path, name):
    category = tmp_path / "downloads"
    cbz(category / "Other Series #1.cbz")
    assert await sync(env, category, category, [(name, 1)]) == set()
    assert env[2].status == DownloadStatus.FAILED
    assert "unsafe path" in env[2].error
    assert not env[3].exists()


def test_file_list_cannot_escape_through_symlink(tmp_path):
    category = tmp_path / "downloads"
    category.mkdir()
    outside = tmp_path / "outside.cbz"
    cbz(outside)
    (category / "linked.cbz").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        tasks._torrent_files(str(category), ["linked.cbz"])


def test_file_list_requires_save_path(tmp_path):
    with pytest.raises(ValueError, match="absolute save path"):
        tasks._torrent_files("", ["Test Series #3.cbz"])


async def test_lossy_template_refuses_entire_torrent_before_placement(env, tmp_path, monkeypatch):
    category = tmp_path / "downloads"
    cbz(category / "Test Series #12.cbz", b"A")
    cbz(category / "Test Series #12.5.cbz", b"B")
    monkeypatch.setitem(VALUES, "naming_template", "{series} #{issue:03.0f}")
    assert await sync(env, category, category, [
        ("Test Series #12.cbz", 1), ("Test Series #12.5.cbz", 1),
    ]) == set()
    assert env[2].status == DownloadStatus.FAILED
    assert env[2].error_code == "import_collision"
    assert "would both be saved as" in env[2].error
    assert list(env[3].iterdir()) == []
    assert len(list(category.glob("*.cbz"))) == 2


async def test_recorded_issue_owner_is_checked_even_with_identical_content(env, tmp_path, monkeypatch):
    session, series, dl, library = env
    existing = library / "Test Series #012.cbz"
    cbz(existing)
    owner = next(issue for issue in series.issues if issue.number == 12.0)
    owner.downloaded, owner.file_path = True, str(existing)
    await session.commit()
    category = tmp_path / "downloads"
    cbz(category / "Test Series #12.5.cbz")  # same bytes cannot justify another issue's path
    monkeypatch.setitem(VALUES, "naming_template", "{series} #{issue:03.0f}")
    assert await sync(env, category, category, [("Test Series #12.5.cbz", 1)]) == {12.0}
    assert dl.status == DownloadStatus.FAILED
    assert "already holds issue 12" in dl.error
    assert [p.name for p in library.iterdir()] == [existing.name]
    assert all(not issue.file_path for issue in series.issues if issue is not owner)


async def test_later_existing_content_collision_leaves_all_sources_in_place(env, tmp_path):
    session, series, dl, library = env
    existing = library / "Test Series #002.cbz"
    cbz(existing, b"old")
    old_bytes = existing.read_bytes()
    category = tmp_path / "downloads"
    cbz(category / "Test Series #1.cbz", b"first")
    cbz(category / "Test Series #2.cbz", b"new")
    with pytest.raises(ImportValidationError, match="different content"):
        import_payload(category, series, list(series.issues), library.parent,
                       DEFAULT_TEMPLATE, move=True)
    assert list(library.iterdir()) == [existing]
    assert existing.read_bytes() == old_bytes
    assert len(list(category.glob("*.cbz"))) == 2


async def test_same_issue_reimport_preserves_existing_destination(env, tmp_path):
    category = tmp_path / "downloads"
    cbz(category / "Test Series #3.cbz")
    assert await sync(env, category, category, [("Test Series #3.cbz", 1)]) == {3.0}
    dest = env[3] / "Test Series #003.cbz"
    contents, mtime = dest.read_bytes(), dest.stat().st_mtime_ns
    env[2].status = DownloadStatus.DOWNLOADING
    await env[0].commit()
    assert await sync(env, category, category, [("Test Series #3.cbz", 1)]) == {3.0}
    assert env[2].status == DownloadStatus.DONE, env[2].error
    assert (dest.read_bytes(), dest.stat().st_mtime_ns) == (contents, mtime)
