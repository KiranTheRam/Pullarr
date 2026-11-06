"""Unavailable libraries must retain ownership and never become download targets."""

import io
import shutil
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr.api import library
from pullarr.jobs import tasks
from pullarr.library import importer, scanner
from pullarr.library.naming import DEFAULT_TEMPLATE
from pullarr.models import Base, Download, DownloadKind, DownloadStatus, Issue, RootFolder, Series, SeriesFolder, SeriesSourceLink
from pullarr.sources.base import DDLSource, SourceRelease


def comic_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("001.png", b"page")
    return output.getvalue()


def owned(root):
    return Issue(id=1, number=1, downloaded=True, file_path=str(root / "Series" / "Series #001.cbz"),
                 file_source="fake", file_download_id=10)


@pytest.mark.parametrize("state", ["missing", "empty", "file"])
def test_unavailable_scan_preserves_all_file_state(tmp_path, state):
    root = tmp_path / "library"
    if state == "empty":
        root.mkdir()
    elif state == "file":
        root.write_text("not a directory")
    issue = owned(root)
    result = scanner.scan_series(Series(title="Series"), [issue], [root / "Series"], root=root)
    assert result.root_unavailable
    assert result.cleared == 0
    assert issue.downloaded
    assert issue.file_source == "fake" and issue.file_download_id == 10
    assert issue.file_path.endswith("Series #001.cbz")


def test_brand_new_empty_root_is_valid(tmp_path):
    assert scanner.root_unavailable(tmp_path, expect_content=False) == ""
    assert "empty" in scanner.root_unavailable(tmp_path, expect_content=True)


def test_unreadable_root_is_unavailable_even_when_new(tmp_path, monkeypatch):
    def fail(self):
        raise PermissionError("share unavailable")
    monkeypatch.setattr(Path, "iterdir", fail)
    assert "can't be read" in scanner.root_unavailable(tmp_path, expect_content=False)


def test_deleted_series_under_populated_root_is_reconciled(tmp_path):
    (tmp_path / "Other Series").mkdir()
    issue = owned(tmp_path)
    result = scanner.scan_series(Series(title="Series"), [issue], [tmp_path / "Series"], root=tmp_path)
    assert result.cleared == 1
    assert not issue.downloaded and not issue.file_path
    assert not issue.file_source and issue.file_download_id is None


def test_share_lost_during_scan_does_not_apply_partial_results(tmp_path, monkeypatch):
    folder = tmp_path / "Series"
    folder.mkdir()
    issue = owned(tmp_path)
    def disappear(path):
        folder.rmdir()
        return []
    monkeypatch.setattr(scanner, "find_media_files", disappear)
    result = scanner.scan_series(Series(title="Series"), [issue], [folder], root=tmp_path)
    assert result.root_unavailable
    assert issue.downloaded and issue.file_source == "fake"


class FakeSource(DDLSource):
    name = "fake"

    async def search_series(self, query):
        return []

    async def list_releases(self, external_id):
        return [SourceRelease(source_name=self.name, external_id="release", title="Series #2", issue_number=2)]

    async def search_releases(self, query):
        return []

    async def resolve_downloads(self, external_id):
        return [["https://download.test/Series%20%232.cbz"]]


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


async def make_series(session, root, with_owned=True):
    series = Series(title="Series", sort_title="series", folder_name="Series", root_folder=RootFolder(path=str(root)))
    series.source_links.append(SeriesSourceLink(source_name="fake", external_id="Series"))
    if with_owned:
        issue = owned(root)
        issue.id = None
        issue.file_download_id = None
        series.issues.append(issue)
    series.issues.append(Issue(number=2, monitored=True))
    session.add(series)
    await session.commit()
    return await tasks._load_series(session, series.id)


async def queue_download(session, series):
    dl = Download(series_id=series.id, issue_id=series.issues[-1].id, kind=DownloadKind.DIRECT,
                  status=DownloadStatus.QUEUED, source_name="fake", title="Series #2", payload="release")
    session.add(dl)
    await session.commit()
    return dl


@pytest.fixture
async def source(monkeypatch):
    source = FakeSource()
    source.client = httpx.AsyncClient()
    monkeypatch.setitem(tasks.registry.DDL_SOURCES, "fake", source)
    yield source
    await source.client.aclose()


async def settings(monkeypatch, tmp_path):
    values = {"naming_template": DEFAULT_TEMPLATE, "ddl_directory": str(tmp_path / "staging"),
              "source_fake_enabled": "true", "download_retry_attempts": "0",
              "source_priority": "fake"}
    async def apply(session):
        return values
    monkeypatch.setattr(tasks.registry, "apply_settings", apply)
    return values


@pytest.mark.parametrize("state", ["missing", "empty"])
async def test_reconcile_and_monitor_preserve_unavailable_library(session, tmp_path, state):
    root = tmp_path / "library"
    if state == "empty":
        root.mkdir()
    series = await make_series(session, root)
    assert await tasks.reconcile_downloaded_files(session, series) == 0
    assert series.issues[0].downloaded
    assert await tasks.grab_missing_issues(session, series, {}) == 0
    with pytest.raises(scanner.LibraryUnavailableError):
        await tasks.scan_series_folder(session, series)
    assert series.issues[0].downloaded


async def test_new_series_in_neighbours_empty_root_is_blocked(session, tmp_path):
    existing = await make_series(session, tmp_path)
    newcomer = Series(title="New", root_folder=existing.root_folder)
    newcomer.issues = [Issue(number=1)]
    session.add(newcomer)
    await session.commit()
    newcomer = await tasks._load_series(session, newcomer.id)
    assert "empty" in await tasks.library_root_unavailable(session, newcomer)
    with pytest.raises(HTTPException) as error:
        await library.scan(newcomer.id, session)
    assert error.value.status_code == 409


async def test_external_extra_folder_does_not_make_empty_primary_root_unavailable(session, tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    extra = tmp_path / "other" / "Series"
    extra.mkdir(parents=True)
    external = extra / "Series #001.cbz"
    external.write_bytes(comic_bytes())
    series = await make_series(session, root)
    series.issues[0].file_path = str(external)
    series.extra_folders.append(SeriesFolder(path=str(extra)))
    await session.commit()

    assert not await tasks.root_expects_content(session, series)
    assert await tasks.library_root_unavailable(session, series) == ""
    result = await library.scan(series.id, session)
    assert result.cleared == 0
    assert series.issues[0].downloaded and series.issues[0].file_path == str(external)
    assert await tasks.reconcile_downloaded_files(session, series) == 0
    assert not list(root.iterdir())

    # The direct scanner/importer defaults must use the same path-aware rule.
    result = scanner.scan_series(series, list(series.issues), [extra], root=root)
    assert not result.root_unavailable
    payload = tmp_path / "Series #002.cbz"
    payload.write_bytes(comic_bytes())
    placed = importer.import_payload(payload, series, list(series.issues), root, DEFAULT_TEMPLATE)
    assert placed[0].covered == [series.issues[1]]
    assert external.exists() and placed[0].dest.is_file()


async def test_external_only_neighbour_does_not_block_new_series(session, tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    existing = await make_series(session, root)
    extra = tmp_path / "library-other" / "Series #001.cbz"
    extra.parent.mkdir()
    extra.write_bytes(comic_bytes())
    existing.issues[0].file_path = str(extra)
    newcomer = Series(title="New", root_folder=existing.root_folder, issues=[Issue(number=1)])
    session.add(newcomer)
    await session.commit()
    newcomer = await tasks._load_series(session, newcomer.id)
    assert not await tasks.root_expects_content(session, newcomer)
    assert await tasks.library_root_unavailable(session, newcomer) == ""
    await library.scan(newcomer.id, session)

    # A missing file recorded beneath the root still proves a lost mount,
    # even when an external-only neighbour was returned by the query first.
    internal = Series(title="Internal", root_folder=existing.root_folder,
                      issues=[Issue(number=1, downloaded=True, file_path=str(root / "missing.cbz"))])
    session.add(internal)
    await session.commit()
    assert await tasks.root_expects_content(session, newcomer)
    assert "empty" in await tasks.library_root_unavailable(session, newcomer)


def test_unknown_owned_path_retains_conservative_root_check(tmp_path):
    issue = Issue(number=1, downloaded=True, file_path="")
    result = scanner.scan_series(Series(title="Series"), [issue], [], root=tmp_path)
    assert result.root_unavailable
    assert issue.downloaded


async def test_resync_rejects_before_deleting_owned_rows(session, tmp_path):
    series = await make_series(session, tmp_path / "missing")
    ids = [i.id for i in series.issues]
    with pytest.raises(HTTPException) as error:
        await library.resync_issues(series.id, session)
    assert error.value.status_code == 409
    assert [i.id for i in (await session.execute(select(Issue))).scalars()] == ids


@pytest.mark.parametrize("state", ["missing", "empty"])
async def test_direct_worker_never_calls_source_for_unavailable_root(session, tmp_path, monkeypatch, source, state):
    root = tmp_path / "library"
    if state == "empty":
        root.mkdir()
    series = await make_series(session, root)
    dl = await queue_download(session, series)
    values = await settings(monkeypatch, tmp_path)
    async def unexpected(*args, **kwargs):
        pytest.fail("source should not be reached")
    monkeypatch.setattr(tasks, "download_release", unexpected)
    await tasks._run_direct_download(session, dl)
    assert dl.status == DownloadStatus.FAILED
    assert dl.error_code == "library_unavailable"
    assert not (root / "Series").exists()
    # Restore the share: failures caused by the library don't suppress source grabs.
    (root / "Series").mkdir(parents=True)
    (root / "Series" / "Series #001.cbz").write_bytes(comic_bytes())
    assert await tasks.grab_missing_issues(session, series, values) == 1


@pytest.mark.parametrize("state", ["missing", "empty"])
async def test_root_lost_during_stream_never_receives_import(session, tmp_path, monkeypatch, source, state):
    root = tmp_path / "library"
    (root / "Series").mkdir(parents=True)
    (root / "Series" / "Series #001.cbz").write_bytes(comic_bytes())
    series = await make_series(session, root)
    dl = await queue_download(session, series)
    await settings(monkeypatch, tmp_path)
    class DropShare(httpx.AsyncByteStream):
        async def __aiter__(self):
            shutil.rmtree(root if state == "missing" else root / "Series")
            yield comic_bytes()
    await source.client.aclose()
    source.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=DropShare())))
    await tasks._run_direct_download(session, dl)
    assert dl.status == DownloadStatus.FAILED
    assert dl.error_code == "library_unavailable"
    assert not series.issues[-1].downloaded
    assert not root.exists() if state == "missing" else list(root.iterdir()) == []
    assert not list((tmp_path / "staging").rglob("*.partial"))


async def test_root_lost_after_download_before_import_is_blocked(session, tmp_path, monkeypatch, source):
    root = tmp_path / "library"
    (root / "Series").mkdir(parents=True)
    series = await make_series(session, root)
    dl = await queue_download(session, series)
    await settings(monkeypatch, tmp_path)
    async def downloaded(*args, **kwargs):
        payload = tmp_path / "payload"
        payload.mkdir()
        (payload / "Series #2.cbz").write_bytes(comic_bytes())
        shutil.rmtree(root)
        return payload
    monkeypatch.setattr(tasks, "download_release", downloaded)
    await tasks._run_direct_download(session, dl)
    assert dl.error_code == "library_unavailable"
    assert not root.exists()


async def test_completed_torrent_waits_for_root_then_imports(session, tmp_path):
    root = tmp_path / "library"
    series = await make_series(session, root, with_owned=False)
    dl = await queue_download(session, series)
    dl.kind = DownloadKind.TORRENT
    payload = tmp_path / "Series #2.cbz"
    payload.write_bytes(comic_bytes())
    values = {"naming_template": DEFAULT_TEMPLATE}
    await tasks._import_torrent(session, dl, payload, values)
    assert dl.status == DownloadStatus.DOWNLOADING
    assert dl.error_code == "library_unavailable"
    assert payload.exists() and not root.exists()
    root.mkdir()
    await tasks._import_torrent(session, dl, payload, values)
    assert dl.status == DownloadStatus.DONE
    assert dl.error == "" and dl.error_code == ""
    assert series.issues[0].downloaded


def test_importer_does_not_create_missing_root(tmp_path):
    source = tmp_path / "Series #1.cbz"
    source.write_bytes(comic_bytes())
    root = tmp_path / "missing"
    with pytest.raises(scanner.LibraryUnavailableError):
        importer.import_payload(source, Series(title="Series", folder_name="Series"),
                                [Issue(number=1)], root, DEFAULT_TEMPLATE)
    assert not root.exists() and source.exists()


def test_importer_checks_before_finalizing_copy(tmp_path, monkeypatch):
    source = tmp_path / "Series #1.cbz"
    source.write_bytes(comic_bytes())
    root = tmp_path / "library"
    root.mkdir()
    copy = shutil.copy2
    def copy_then_drop(src, dest):
        copy(src, dest)
        shutil.rmtree(root)
    monkeypatch.setattr(importer.shutil, "copy2", copy_then_drop)
    with pytest.raises(scanner.LibraryUnavailableError):
        importer.import_payload(source, Series(title="Series", folder_name="Series"),
                                [Issue(number=1)], root, DEFAULT_TEMPLATE)
    assert source.exists() and not root.exists()


@pytest.mark.parametrize("kind", [DownloadKind.DIRECT, DownloadKind.TORRENT])
async def test_io_error_when_mount_disappears_is_classified_as_library_failure(
    session, tmp_path, monkeypatch, source, kind,
):
    root = tmp_path / "library"
    (root / "Series").mkdir(parents=True)
    series = await make_series(session, root)
    dl = await queue_download(session, series)
    dl.kind = kind
    values = await settings(monkeypatch, tmp_path)
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "Series #2.cbz").write_bytes(comic_bytes())
    async def downloaded(*args, **kwargs):
        return payload
    def fail(*args, **kwargs):
        shutil.rmtree(root)
        raise OSError("Input/output error")
    monkeypatch.setattr(tasks, "download_release", downloaded)
    monkeypatch.setattr(importer.shutil, "copy2", fail)
    monkeypatch.setattr(importer.shutil, "move", fail)
    if kind == DownloadKind.DIRECT:
        await tasks._run_direct_download(session, dl)
        assert dl.status == DownloadStatus.FAILED
    else:
        await tasks._import_torrent(session, dl, payload, values)
        assert dl.status == DownloadStatus.DOWNLOADING
    assert dl.error_code == "library_unavailable"
    assert not root.exists()


async def test_source_failure_retains_its_cooldown(session, tmp_path, monkeypatch, source):
    root = tmp_path / "library"
    (root / "Series").mkdir(parents=True)
    series = await make_series(session, root)
    dl = await queue_download(session, series)
    values = await settings(monkeypatch, tmp_path)
    async def fail(*args, **kwargs):
        raise ConnectionRefusedError("source refused the connection")
    monkeypatch.setattr(tasks, "download_release", fail)
    await tasks._run_direct_download(session, dl)
    assert dl.status == DownloadStatus.FAILED
    assert dl.error_code == "network_error"
    assert await tasks.grab_missing_issues(session, series, values) == 0


@pytest.mark.parametrize("destination", ["external", "root"])
def test_import_preserves_supported_primary_destinations(tmp_path, destination):
    root = tmp_path / "library"
    root.mkdir()
    folder = tmp_path / "external" if destination == "external" else root
    folder.mkdir(exist_ok=True)
    source = tmp_path / "Series #2.cbz"
    source.write_bytes(comic_bytes())
    series = Series(title="Series", folder_name=str(folder) if destination == "external" else ".")
    issue = Issue(number=2, downloaded=False)
    result = importer.import_payload(source, series, [issue], root, DEFAULT_TEMPLATE)
    assert len(result) == 1 and result[0].dest.parent == folder
    assert result[0].dest.is_file()


@pytest.mark.parametrize("state", ["missing", "empty_owned", "lost_during_copy"])
def test_external_primary_is_an_availability_boundary(tmp_path, monkeypatch, state):
    root = tmp_path / "library"
    root.mkdir()
    external = tmp_path / "external"
    if state != "missing":
        external.mkdir()
    source = tmp_path / "Series #2.cbz"
    source.write_bytes(comic_bytes())
    series = Series(title="Series", folder_name=str(external))
    issues = [Issue(number=2, downloaded=False)]
    if state == "empty_owned":
        issues.append(owned(external))
    if state == "lost_during_copy":
        original = importer.shutil.copy2

        def lose_destination(*args, **kwargs):
            result = original(*args, **kwargs)
            shutil.rmtree(external)
            return result

        monkeypatch.setattr(importer.shutil, "copy2", lose_destination)
    with pytest.raises(scanner.LibraryUnavailableError):
        importer.import_payload(source, series, issues, root, DEFAULT_TEMPLATE)
    assert source.exists()
    assert not list(root.rglob("*.cbz"))
    assert not list(external.glob("*.cbz"))
    if state == "missing":
        assert not external.exists()
