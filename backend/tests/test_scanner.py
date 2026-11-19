import zipfile

import pytest

from pullarr.library.matcher import find_media_files
from pullarr.library.scanner import find_existing_folder, scan_series, series_dir
from pullarr.models import Issue, Series

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def tracked(*specs):
    return [Issue(id=i + 1, series_id=1, number=float(n), volume=v, downloaded=False,
                  file_path="")
            for i, (n, v) in enumerate(specs)]


def make_cbz(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.png", PNG)


class TestScanSeries:
    def test_marks_issue_files_owned_in_place(self, tmp_path):
        folder = tmp_path / "Absolute Batman (2024)"
        folder.mkdir()
        make_cbz(folder / "Absolute Batman #001.cbz")
        make_cbz(folder / "Absolute Batman #002.cbz")
        issues = tracked((1, None), (2, None), (3, None))
        series = Series(id=1, title="Absolute Batman", year=2024,
                        folder_name="Absolute Batman (2024)", alt_titles="")

        result = scan_series(series, issues, [folder])

        assert result.matched_issues == 2
        assert issues[0].downloaded and issues[0].file_path.endswith("#001.cbz")
        assert issues[1].downloaded
        assert not issues[2].downloaded  # #3 not on disk
        # files are untouched (still exactly the two we created)
        assert sorted(p.name for p in folder.iterdir()) == [
            "Absolute Batman #001.cbz", "Absolute Batman #002.cbz"]

    def test_adopts_pdf_issues_and_ranges_without_changing_files(self, tmp_path):
        folder = tmp_path / "Series"
        nested = folder / "Collected"
        nested.mkdir(parents=True)
        single = folder / "Series #001.PDF"
        collection = nested / "Series #002-003.pdf"
        unmatched = folder / "Bonus Artbook.pdf"
        for path in (single, collection, unmatched):
            path.write_bytes(b"%PDF-1.7\n")
        issues = tracked((1, None), (2, None), (3, None), (4, None))

        result = scan_series(Series(title="Series"), issues, [folder])

        assert result.matched_issues == 3
        assert [issue.file_path for issue in issues] == [str(single), str(collection), str(collection), ""]
        assert [issue.downloaded for issue in issues] == [True, True, True, False]
        assert [media.path for media in result.unmatched] == [unmatched]
        assert all(path.read_bytes() == b"%PDF-1.7\n" for path in (single, collection, unmatched))

    @pytest.mark.parametrize("name", ["Receipt #002.pdf", "Receipt 001-003.pdf", "Catalog Vol. 01.pdf",
                                     "[Superman] #002.pdf", "[Superman] [#001-003].pdf",
                                     "Issue [Superman] #002.pdf", "[300] #002.pdf",
                                     "[Superman] (Digital) #002.pdf"])
    def test_pdf_with_unrelated_title_stays_unmatched(self, tmp_path, name):
        path = tmp_path / name
        path.write_bytes(b"%PDF-1.7\n")
        issues = tracked((1, 1), (2, 1), (3, 1))

        result = scan_series(Series(title="Batman"), issues, [tmp_path])

        assert [media.path for media in result.unmatched] == [path]
        assert not any(issue.downloaded for issue in issues)

    @pytest.mark.parametrize("name", ["002.pdf", "Issue #002.pdf", "The Caped Crusader 001-003.pdf",
                                     "The Caped Crusader Vol. 01.pdf", "The Caped Crusader [#002].pdf",
                                     "[Batman] #002.pdf", "(2024) [The Caped Crusader] [#002].pdf",
                                     "#002 [Release Group].pdf", "[Batman] (Digital) #002.pdf",
                                     "[Release Group] [Batman] [Digital] #002.pdf"])
    def test_pdf_matches_alternate_or_number_only_name(self, tmp_path, name):
        path = tmp_path / name
        path.write_bytes(b"%PDF-1.7\n")
        issues = tracked((1, 1), (2, 1), (3, 1))

        scan_series(Series(title="Batman", alt_titles="Caped Crusader"), issues, [tmp_path])

        assert issues[1].file_path == str(path)

    @pytest.mark.parametrize("mapped", [False, True])
    def test_rescan_reconciles_conflicting_pdf_without_overriding_manual_mapping(self, tmp_path, mapped):
        pdf = tmp_path / "Receipt #002.pdf"
        pdf.write_bytes(b"%PDF-1.7\n")
        issues = tracked((2, None))
        issue = issues[0]
        issue.downloaded = True
        issue.file_path = str(pdf)
        issue.file_mapped = mapped
        series = Series(title="Batman")

        result = scan_series(series, issues, [tmp_path], root=tmp_path)

        assert result.cleared == int(not mapped)
        assert issue.downloaded == mapped
        assert issue.file_path == (str(pdf) if mapped else "")
        assert [media.path for media in result.unmatched] == [pdf]
        assert pdf.read_bytes() == b"%PDF-1.7\n"
        assert scan_series(series, issues, [tmp_path]).cleared == 0

    @pytest.mark.parametrize(("name", "numbers"), [
        ("Collected Stories #001-002.pdf", [1, 2]),
        ("Collected Stories #001-002.pdf", [2]),
        ("Collected Stories #002.pdf", [1, 2]),
        ("Collected Stories #007.pdf", [2]),
    ])
    def test_rescan_preserves_legacy_pdf_ownership_that_could_be_manual(self, tmp_path, name, numbers):
        pdf = tmp_path / name
        pdf.write_bytes(b"%PDF-1.7\n")
        issues = tracked(*[(number, None) for number in numbers])
        for issue in issues:
            issue.downloaded = True
            issue.file_path = str(pdf)
        series = Series(title="Batman")

        result = scan_series(series, issues, [tmp_path], root=tmp_path)

        assert result.cleared == 0
        assert all(issue.downloaded and issue.file_path == str(pdf) for issue in issues)
        assert [media.path for media in result.unmatched] == [pdf]

    def test_legacy_shared_pdf_stays_owned_after_an_exact_issue_arrives(self, tmp_path):
        pdf = tmp_path / "Collected Stories #002.pdf"
        pdf.write_bytes(b"%PDF-1.7\n")
        issues = tracked((1, None), (2, None))
        for issue in issues:
            issue.downloaded = True
            issue.file_path = str(pdf)
        series = Series(title="Batman")
        exact = tmp_path / "Batman #001.cbz"
        make_cbz(exact)

        assert scan_series(series, issues, [tmp_path]).cleared == 0
        assert scan_series(series, issues, [tmp_path]).cleared == 0
        assert [issue.file_path for issue in issues] == [str(exact), str(pdf)]

    def test_pdf_edition_suffix_still_requires_the_matching_edition(self, tmp_path):
        path = tmp_path / "Batman Annual #002.pdf"
        path.write_bytes(b"%PDF-1.7\n")
        issues = tracked((2, None), (2, None))
        issues[1].format = "Annual"

        scan_series(Series(title="Batman"), issues, [tmp_path])

        assert not issues[0].downloaded
        assert issues[1].file_path == str(path)

    def test_empty_pdf_is_ignored_and_clears_prior_automatic_ownership(self, tmp_path):
        path = tmp_path / "Batman #002.PDF"
        path.touch()
        issues = tracked((2, None))
        series = Series(title="Batman")

        assert find_media_files(path) == []
        assert scan_series(series, issues, [tmp_path]).matched_issues == 0
        assert not issues[0].downloaded

        path.write_bytes(b"\xef\xbb\xbf%PDF-1.7\n")
        assert scan_series(series, issues, [tmp_path]).matched_issues == 1
        path.write_bytes(b"")
        assert scan_series(series, issues, [tmp_path]).cleared == 1
        assert not issues[0].downloaded

    @pytest.mark.parametrize(("name", "content"), [
        ("._Batman #002.pdf", b"\x00\x05\x16\x07%PDF-"),
        ("Batman #002.pdf", b"This is not a PDF"),
    ])
    def test_ignores_pdf_sidecars_and_non_pdf_content(self, tmp_path, name, content):
        path = tmp_path / name
        path.write_bytes(content)
        series = Series(title="Batman")
        issues = tracked((2, None))

        assert find_media_files(path) == []
        assert scan_series(series, issues, [tmp_path]).matched_issues == 0

        issues[0].downloaded = True
        issues[0].file_path = str(path)
        assert scan_series(series, issues, [tmp_path]).cleared == 1
        assert path.read_bytes() == content

    def test_duplicate_matches_keep_the_tracked_archive_and_its_provenance(self, tmp_path):
        first = tmp_path / "Series #001.cbz"
        original = tmp_path / "Series #1.cbz"
        make_cbz(first)
        make_cbz(original)
        issues = tracked((1, None))
        issue = issues[0]
        issue.downloaded = True
        issue.file_path = str(original)
        issue.file_source = "getcomics"
        issue.file_download_id = 12
        series = Series(title="Series")

        scan_series(series, issues, [tmp_path])
        assert (issue.file_path, issue.file_source, issue.file_download_id) == (str(original), "getcomics", 12)

        original.unlink()
        scan_series(series, issues, [tmp_path])
        assert (issue.file_path, issue.file_source, issue.file_download_id) == (str(first), "", None)

    def test_volume_archive_covers_its_issues(self, tmp_path):
        folder = tmp_path / "Series"
        folder.mkdir()
        make_cbz(folder / "Series Vol. 01.cbz")
        issues = tracked((1, 1), (2, 1), (3, 1), (10, 2))
        series = Series(id=1, title="Series", folder_name="Series", alt_titles="")

        result = scan_series(series, issues, [folder])

        assert result.volume_files == 1
        assert all(i.downloaded for i in issues[:3])
        assert not issues[3].downloaded  # volume 2 not present

    def test_reconciles_missing_files(self, tmp_path):
        folder = tmp_path / "Series"
        folder.mkdir()
        issues = tracked((1, 1))
        issues[0].downloaded = True
        issues[0].file_path = str(folder / "gone.cbz")  # file doesn't exist
        series = Series(id=1, title="Series", folder_name="Series", alt_titles="")

        result = scan_series(series, issues, [folder])

        assert result.cleared == 1
        assert not issues[0].downloaded and issues[0].file_path == ""

    def test_unmatched_surfaced(self, tmp_path):
        folder = tmp_path / "Series"
        folder.mkdir()
        make_cbz(folder / "Bonus Artbook.cbz")
        issues = tracked((1, 1))
        series = Series(id=1, title="Series", folder_name="Series", alt_titles="")

        result = scan_series(series, issues, [folder])
        assert [m.path.name for m in result.unmatched] == ["Bonus Artbook.cbz"]


class TestScanMultipleFolders:
    def test_scans_across_tpb_and_issue_dirs(self, tmp_path):
        vols = tmp_path / "Series TPBs"
        singles = tmp_path / "Series Issues"
        vols.mkdir()
        singles.mkdir()
        make_cbz(vols / "Series Vol. 01.cbz")  # covers issues 1,2,3 (volume 1)
        make_cbz(singles / "Series #010.cbz")  # exact issue 10
        issues = tracked((1, 1), (2, 1), (3, 1), (10, 2))
        series = Series(id=1, title="Series", folder_name="Series TPBs", alt_titles="")

        result = scan_series(series, issues, [vols, singles])

        assert all(i.downloaded for i in issues[:3])  # from the volume archive
        assert issues[3].downloaded  # #10 from the issues dir
        assert issues[3].file_path.endswith("Series #010.cbz")
        assert result.matched_issues == 4

    def test_exact_issue_file_wins_over_volume_archive(self, tmp_path):
        vols = tmp_path / "vols"
        singles = tmp_path / "singles"
        vols.mkdir()
        singles.mkdir()
        make_cbz(vols / "Series Vol. 01.cbz")  # volume 1 covers issues 1,2,3
        make_cbz(singles / "Series #002.cbz")  # exact issue 2
        issues = tracked((1, 1), (2, 1), (3, 1))
        series = Series(id=1, title="Series", folder_name="vols", alt_titles="")

        scan_series(series, issues, [vols, singles])

        # issue 2 points at the precise issue file, not the volume archive
        assert issues[1].file_path.endswith("Series #002.cbz")
        assert issues[0].file_path.endswith("Series Vol. 01.cbz")
        assert issues[2].file_path.endswith("Series Vol. 01.cbz")


class TestFindExistingFolder:
    def test_exact_normalized_match(self, tmp_path):
        (tmp_path / "Chew").mkdir()
        (tmp_path / "Other").mkdir()
        s = Series(id=1, title="Chew!", alt_titles="")
        assert find_existing_folder(tmp_path, s) == "Chew"

    def test_title_year_match(self, tmp_path):
        (tmp_path / "Batman (2016)").mkdir()
        s = Series(id=1, title="Batman", year=2016, alt_titles="")
        assert find_existing_folder(tmp_path, s) == "Batman (2016)"

    def test_alt_title_match(self, tmp_path):
        (tmp_path / "TMNT").mkdir()
        s = Series(id=1, title="Teenage Mutant Ninja Turtles", alt_titles="TMNT")
        assert find_existing_folder(tmp_path, s) == "TMNT"

    def test_nested_publisher_folder_match(self, tmp_path):
        (tmp_path / "Marvel" / "Ghost Spider").mkdir(parents=True)
        s = Series(id=1, title="Spider-Gwen: Ghost-Spider", alt_titles="")
        assert find_existing_folder(tmp_path, s) == "Marvel/Ghost Spider"

    def test_respects_max_depth(self, tmp_path):
        (tmp_path / "A" / "B" / "C" / "Chew").mkdir(parents=True)
        s = Series(id=1, title="Chew!", alt_titles="")
        assert find_existing_folder(tmp_path, s, max_depth=3) is None

    def test_no_match(self, tmp_path):
        (tmp_path / "Totally Different").mkdir()
        s = Series(id=1, title="Saga", alt_titles="")
        assert find_existing_folder(tmp_path, s) is None


class TestSeriesDir:
    def test_uses_title_and_year_when_no_folder_name(self, tmp_path):
        s = Series(id=1, title="Batman", year=2016, folder_name="", alt_titles="")
        assert series_dir(tmp_path, s) == tmp_path / "Batman (2016)"
