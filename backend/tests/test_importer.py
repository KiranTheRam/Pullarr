import io
import zipfile

import pytest

from pullarr.library.importer import import_payload
from pullarr.library.naming import DEFAULT_TEMPLATE
from pullarr.models import Issue, Series

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def series():
    return Series(id=1, title="Absolute Batman", year=2024, publisher="DC Comics",
                  folder_name="", description="")


@pytest.fixture
def issues():
    return [
        Issue(id=n, series_id=1, number=float(n), volume=None, title="")
        for n in range(1, 6)
    ]


def run_import(content_path, series, issues, root, **kwargs):
    return import_payload(content_path, series, issues, root, DEFAULT_TEMPLATE, **kwargs)


def make_cbz(path, pages=2):
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(pages):
            zf.writestr(f"{i:03d}.png", PNG)


def cbz_bytes(pages=2):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zf:
        for i in range(pages):
            zf.writestr(f"{i:03d}.png", PNG)
    return output.getvalue()


class TestImportPayload:
    def test_single_cbz_matched_to_issue(self, tmp_path, series, issues):
        src = tmp_path / "payload" / "Absolute Batman 002 (2025) (Webrip).cbz"
        src.parent.mkdir()
        make_cbz(src)

        imported = run_import(src, series, issues, tmp_path / "lib")

        assert len(imported) == 1
        r = imported[0]
        assert r.dest.name == "Absolute Batman #002.cbz"
        assert r.dest.parent.name == "Absolute Batman (2024)"
        assert r.dest.exists()
        assert r.issue is issues[1]
        assert r.covered == [issues[1]]
        assert r.volume is None

    def test_comicinfo_injected_into_cbz(self, tmp_path, series, issues):
        src = tmp_path / "payload" / "Absolute Batman 002.cbz"
        src.parent.mkdir()
        make_cbz(src)

        imported = run_import(src, series, issues, tmp_path / "lib")

        with zipfile.ZipFile(imported[0].dest) as zf:
            xml = zf.read("ComicInfo.xml").decode()
        assert "<Series>Absolute Batman</Series>" in xml
        assert "<Number>2</Number>" in xml
        assert "<Publisher>DC Comics</Publisher>" in xml

    def test_existing_comicinfo_merged_and_refreshed(self, tmp_path, series, issues):
        src = tmp_path / "payload" / "Absolute Batman 002.cbz"
        src.parent.mkdir()
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("001.png", PNG)
            zf.writestr(
                "ComicInfo.xml",
                "<ComicInfo><Series>Original</Series>"
                "<ScanInformation>Keep me</ScanInformation></ComicInfo>",
            )

        imported = run_import(src, series, issues, tmp_path / "lib")

        with zipfile.ZipFile(imported[0].dest) as zf:
            xml = zf.read("ComicInfo.xml").decode()
        assert "Absolute Batman" in xml
        assert "Keep me" in xml

    def test_mixed_archives_and_loose_image_dirs(self, tmp_path, series, issues):
        payload = tmp_path / "Absolute Batman (Digital)"
        payload.mkdir()
        make_cbz(payload / "Absolute Batman 002.cbz")
        loose = payload / "Absolute Batman - Issue 3"
        loose.mkdir()
        (loose / "p01.png").write_bytes(PNG)
        (loose / "p02.png").write_bytes(PNG)

        imported = run_import(payload, series, issues, tmp_path / "lib")

        by_issue = {r.issue.number: r.dest for r in imported if r.issue is not None}
        assert set(by_issue) == {2.0, 3.0}
        with zipfile.ZipFile(by_issue[3.0]) as zf:
            assert "p01.png" in zf.namelist() and "p02.png" in zf.namelist()

    def test_multi_issue_bundle_covers_its_span(self, tmp_path, series, issues):
        # one file holding issues 1-3 (the "Absolute Carnage #1-3" case)
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "Absolute Batman 001-003 (2024) (Digital) (Group).cbz")

        imported = run_import(payload, series, issues, tmp_path / "lib",
                              force_issue=issues[0])

        assert len(imported) == 1
        r = imported[0]
        assert r.issue is None
        assert r.dest.name == "Absolute Batman #001-003.cbz"
        assert sorted(i.number for i in r.covered) == [1.0, 2.0, 3.0]

    def test_outer_zip_pack_imports_nested_archives(self, tmp_path, series, issues):
        payload = tmp_path / "payload"
        payload.mkdir()
        pack = payload / "Absolute Batman (001-003+) (2024).zip"
        with zipfile.ZipFile(pack, "w") as zf:
            # The bonus preview also parses as #1; the real series archive
            # must win that match instead of causing an import collision.
            zf.writestr("House of Comics - Free Previews 01.cbz", cbz_bytes())
            for number in range(1, 4):
                zf.writestr(
                    f"Absolute Batman {number:02d} (of 03).cbz", cbz_bytes()
                )

        imported = run_import(
            payload, series, issues, tmp_path / "lib", force_issue=issues[0]
        )

        by_issue = {item.issue.number: item for item in imported if item.issue is not None}
        assert set(by_issue) == {1.0, 2.0, 3.0}
        assert all(item.dest.exists() for item in by_issue.values())
        extras = [item for item in imported if item.status == "unmatched"]
        assert len(extras) == 1
        assert "Free Previews" in extras[0].dest.name

    def test_hash_range_title_covers_span(self, tmp_path, series, issues):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "Absolute Batman #2 - 4.cbz")

        imported = run_import(payload, series, issues, tmp_path / "lib")

        r = imported[0]
        assert sorted(i.number for i in r.covered) == [2.0, 3.0, 4.0]
        assert r.dest.name == "Absolute Batman #002-004.cbz"

    def test_volume_archive_without_issue_match(self, tmp_path, series, issues):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "Absolute Batman Vol. 07.zip")

        imported = run_import(payload, series, issues, tmp_path / "lib")

        assert len(imported) == 1
        r = imported[0]
        assert r.issue is None
        assert r.volume == 7
        assert r.dest.name == "Absolute Batman Vol. 07.cbz"
        assert r.dest.exists()

    def test_unmatched_archive_keeps_original_stem(self, tmp_path, series, issues):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "Extras and Omake.cbz")

        imported = run_import(payload, series, issues, tmp_path / "lib")

        r = imported[0]
        assert r.issue is None
        assert r.volume is None
        assert r.covered == []
        assert r.dest.name == "Absolute Batman (2024) - Extras and Omake.cbz"

    def test_force_issue_for_unparseable_single_file(self, tmp_path, series, issues):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "AbsBat-FCBD-Special.cbz")

        imported = run_import(payload, series, issues, tmp_path / "lib",
                              force_issue=issues[4])

        r = imported[0]
        assert r.issue is issues[4]
        assert r.dest.name == "Absolute Batman #005.cbz"

    def test_force_issue_ignored_when_name_matches(self, tmp_path, series, issues):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "Absolute Batman 002.cbz")

        imported = run_import(payload, series, issues, tmp_path / "lib",
                              force_issue=issues[4])

        assert imported[0].issue is issues[1]

    def test_move_semantics_removes_source(self, tmp_path, series, issues):
        payload = tmp_path / "payload"
        payload.mkdir()
        src = payload / "Absolute Batman 002.cbz"
        make_cbz(src)

        run_import(payload, series, issues, tmp_path / "lib", move=True)

        assert not src.exists()

    def test_existing_files_not_overwritten(self, tmp_path, series, issues):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "Absolute Batman 002.cbz")
        lib = tmp_path / "lib"

        first = run_import(payload, series, issues, lib)
        mtime = first[0].dest.stat().st_mtime_ns
        second = run_import(payload, series, issues, lib)

        assert second[0].dest.stat().st_mtime_ns == mtime
