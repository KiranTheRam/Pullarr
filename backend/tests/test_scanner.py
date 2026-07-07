import zipfile

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
