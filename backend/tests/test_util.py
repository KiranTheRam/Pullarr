from pullarr.util import (
    has_issue_marker,
    normalize_title,
    parse_issue_number,
    parse_issue_range,
    parse_volume_number,
    parse_year,
    sanitize_filename,
    strip_issue_suffix,
)


class TestParseIssueRange:
    def test_hash_dash(self):
        assert parse_issue_range("Absolute Carnage – Miles Morales #1 – 3 (2019)") == (1.0, 3.0)

    def test_hash_hash(self):
        assert parse_issue_range("Saga #1-#6 (2013)") == (1.0, 6.0)

    def test_zero_padded_bare(self):
        assert parse_issue_range(
            "Absolute Carnage - Miles Morales 001-003 (2019) (Digital)"
        ) == (1.0, 3.0)

    def test_wide_span(self):
        assert parse_issue_range("Absolute Batman #1-12 (2025)") == (1.0, 12.0)

    def test_single_issue_is_none(self):
        assert parse_issue_range("Absolute Batman #13 (2025)") is None

    def test_tpb_is_none(self):
        assert parse_issue_range("Absolute Carnage – Miles Morales (TPB) (2020)") is None

    def test_volume_range_excluded(self):
        assert parse_issue_range("Batman Vol. 1-3 (TPB) (2020)") is None

    def test_year_not_a_range(self):
        assert parse_issue_range("2000AD 2415 (2026)") is None


class TestParseIssueNumber:
    def test_hash_marker(self):
        assert parse_issue_number("Absolute Batman #15 (2025)") == 15.0

    def test_hash_with_space(self):
        assert parse_issue_number("Saga # 61") == 61.0

    def test_fractional(self):
        assert parse_issue_number("Spawn #10.5") == 10.5

    def test_word_marker(self):
        assert parse_issue_number("Batman Issue 12") == 12.0
        assert parse_issue_number("Batman No. 12") == 12.0

    def test_scene_style_trailing_number(self):
        assert parse_issue_number(
            "Absolute_Batman_015_(2026)_(Webrip)_(The_Last_Kryptonian-DCP)"
        ) == 15.0

    def test_plain_trailing_number(self):
        assert parse_issue_number("Absolute Batman 015") == 15.0

    def test_no_number(self):
        assert parse_issue_number("Batman The Long Halloween TPB") is None

    def test_year_only_is_not_issue(self):
        assert parse_issue_number("Batman Annual (2026)") is None


class TestIssueMarker:
    def test_hash_is_marker(self):
        assert has_issue_marker("Batman #12")

    def test_bare_number_is_not(self):
        assert not has_issue_marker("Batman 012")

    def test_volume_is_not(self):
        assert not has_issue_marker("Batman Vol. 3")


class TestParseVolumeNumber:
    def test_vol_forms(self):
        assert parse_volume_number("Batman Vol. 3 - The Quiet Man (TPB)") == 3
        assert parse_volume_number("Batman v2 #12") == 2
        assert parse_volume_number("Monstress Volume 08") == 8

    def test_no_volume(self):
        assert parse_volume_number("Batman #12") is None

    def test_word_ending_in_v_not_volume(self):
        assert parse_volume_number("Harrow County 4") is None


class TestParseYear:
    def test_year(self):
        assert parse_year("Absolute Batman #15 (2025)") == 2025

    def test_no_year(self):
        assert parse_year("Absolute Batman #15") is None


class TestStripIssueSuffix:
    def test_hash_form(self):
        assert strip_issue_suffix("Absolute Batman #15 (2025)") == "Absolute Batman"

    def test_dash_separator(self):
        assert strip_issue_suffix("Saga – #61 (2026)") == "Saga"

    def test_tpb_title_keeps_name(self):
        assert strip_issue_suffix(
            "Batman and Robin Vol. 3 – The Quiet Man (TPB) (2026)"
        ) == "Batman and Robin Vol. 3 – The Quiet Man"

    def test_trailing_number_without_hash(self):
        assert strip_issue_suffix("2000AD 2415 (2026)") == "2000AD"


class TestNormalizeAndSanitize:
    def test_normalize(self):
        assert normalize_title("Batman: The Long Halloween!") == "batman the long halloween"

    def test_sanitize(self):
        assert sanitize_filename('Spider-Man: No Way Home?') == "Spider-Man No Way Home"
