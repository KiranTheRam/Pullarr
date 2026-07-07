from pathlib import Path

from pullarr.library.naming import (
    DEFAULT_TEMPLATE,
    issue_filename,
    issue_path,
    series_folder,
    volume_filename,
)


def name(issue: float, title: str = "", year: int | None = None) -> str:
    return issue_filename(DEFAULT_TEMPLATE, "Absolute Batman", issue, title, year)


class TestIssueFilename:
    def test_whole_issue_padded(self):
        assert name(15) == "Absolute Batman #015.cbz"

    def test_fractional_issue(self):
        assert name(15.5) == "Absolute Batman #015.5.cbz"

    def test_series_name_sanitized(self):
        result = issue_filename(DEFAULT_TEMPLATE, "Batman: Year One", 1)
        assert result == "Batman Year One #001.cbz"

    def test_custom_template_with_year_and_title(self):
        template = "{series} ({year}) #{issue:03d} - {title}"
        result = issue_filename(template, "Saga", 61, "Chapter Sixty-One", 2012)
        assert result == "Saga (2012) #061 - Chapter Sixty-One.cbz"

    def test_extension_preserved(self):
        assert issue_filename(DEFAULT_TEMPLATE, "Saga", 1, ext=".cbr") == "Saga #001.cbr"


class TestVolumeFilename:
    def test_tpb_name(self):
        assert volume_filename("Batman", 3) == "Batman Vol. 03.cbz"


class TestSeriesFolder:
    def test_with_year(self):
        assert series_folder("Batman", 2016) == "Batman (2016)"

    def test_without_year(self):
        assert series_folder("Batman") == "Batman"

    def test_sanitized(self):
        assert series_folder("Batman: White Knight", 2017) == "Batman White Knight (2017)"


class TestIssuePath:
    def test_full_path(self):
        p = issue_path(Path("/library"), DEFAULT_TEMPLATE, "Absolute Batman",
                       "", 15, year=2024)
        assert p == Path("/library/Absolute Batman (2024)/Absolute Batman #015.cbz")

    def test_explicit_folder_wins(self):
        p = issue_path(Path("/library"), DEFAULT_TEMPLATE, "Absolute Batman",
                       "AbsBat", 15, year=2024)
        assert p == Path("/library/AbsBat/Absolute Batman #015.cbz")
