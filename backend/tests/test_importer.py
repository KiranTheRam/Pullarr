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


class TestImportPayload:
    def test_single_cbz_matched_to_issue(self, tmp_path, series, issues):
        src = tmp_path / "payload" / "Absolute Batman 002 (2025) (Webrip).cbz"
        src.parent.mkdir()
        make_cbz(src)

        imported = run_import(src, series, issues, tmp_path / "lib")

        assert len(imported) == 1
        dest, issue, volume = imported[0]
        assert dest.name == "Absolute Batman #002.cbz"
        assert dest.parent.name == "Absolute Batman (2024)"
        assert dest.exists()
        assert issue is issues[1]
        assert volume is None  # issue matched, so no volume-level claim

    def test_comicinfo_injected_into_cbz(self, tmp_path, series, issues):
        src = tmp_path / "payload" / "Absolute Batman 002.cbz"
        src.parent.mkdir()
        make_cbz(src)

        imported = run_import(src, series, issues, tmp_path / "lib")

        with zipfile.ZipFile(imported[0][0]) as zf:
            xml = zf.read("ComicInfo.xml").decode()
        assert "<Series>Absolute Batman</Series>" in xml
        assert "<Number>2</Number>" in xml
        assert "<Publisher>DC Comics</Publisher>" in xml

    def test_existing_comicinfo_untouched(self, tmp_path, series, issues):
        src = tmp_path / "payload" / "Absolute Batman 002.cbz"
        src.parent.mkdir()
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("001.png", PNG)
            zf.writestr("ComicInfo.xml", "<ComicInfo><Series>Original</Series></ComicInfo>")

        imported = run_import(src, series, issues, tmp_path / "lib")

        with zipfile.ZipFile(imported[0][0]) as zf:
            xml = zf.read("ComicInfo.xml").decode()
        assert "Original" in xml

    def test_mixed_archives_and_loose_image_dirs(self, tmp_path, series, issues):
        payload = tmp_path / "Absolute Batman (Digital)"
        payload.mkdir()
        make_cbz(payload / "Absolute Batman 002.cbz")
        loose = payload / "Absolute Batman - Issue 3"
        loose.mkdir()
        (loose / "p01.png").write_bytes(PNG)
        (loose / "p02.png").write_bytes(PNG)

        imported = run_import(payload, series, issues, tmp_path / "lib")

        by_issue = {i.number: dest for dest, i, _ in imported if i is not None}
        assert set(by_issue) == {2.0, 3.0}
        # loose images were zipped into a CBZ
        with zipfile.ZipFile(by_issue[3.0]) as zf:
            assert "p01.png" in zf.namelist() and "p02.png" in zf.namelist()

    def test_volume_archive_without_issue_match(self, tmp_path, series, issues):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "Absolute Batman Vol. 07.zip")

        imported = run_import(payload, series, issues, tmp_path / "lib")

        assert len(imported) == 1
        dest, issue, volume = imported[0]
        assert issue is None
        assert volume == 7  # caller can mark all issues of vol 7 downloaded
        assert dest.name == "Absolute Batman Vol. 07.cbz"  # .zip renamed to .cbz
        assert dest.exists()

    def test_unmatched_archive_keeps_original_stem(self, tmp_path, series, issues):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "Extras and Omake.cbz")

        imported = run_import(payload, series, issues, tmp_path / "lib")

        dest, issue, volume = imported[0]
        assert issue is None
        assert volume is None
        assert dest.name == "Absolute Batman (2024) - Extras and Omake.cbz"

    def test_force_issue_for_unparseable_single_file(self, tmp_path, series, issues):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "AbsBat-FCBD-Special.cbz")  # no parsable number

        imported = run_import(payload, series, issues, tmp_path / "lib",
                              force_issue=issues[4])

        dest, issue, volume = imported[0]
        assert issue is issues[4]
        assert dest.name == "Absolute Batman #005.cbz"

    def test_force_issue_ignored_when_name_matches(self, tmp_path, series, issues):
        payload = tmp_path / "payload"
        payload.mkdir()
        make_cbz(payload / "Absolute Batman 002.cbz")

        imported = run_import(payload, series, issues, tmp_path / "lib",
                              force_issue=issues[4])

        # the filename says #2 — trust the parse, not the grab hint
        assert imported[0][1] is issues[1]

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
        mtime = first[0][0].stat().st_mtime_ns
        second = run_import(payload, series, issues, lib)

        assert second[0][0].stat().st_mtime_ns == mtime
