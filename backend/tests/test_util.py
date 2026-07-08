from datetime import datetime

from pullarr.models import Issue
from pullarr.sources.base import SourceRelease
from pullarr.util import (
    has_issue_marker,
    normalize_title,
    parse_issue_label,
    parse_issue_number,
    parse_issue_range,
    parse_volume_number,
    parse_year,
    release_covers_issue,
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

    def test_leading_article_dropped(self):
        # GetComics posts "Amazing Spider-Man"; ComicVine titles it with "The"
        assert normalize_title("The Amazing Spider-Man") == normalize_title(
            "Amazing Spider-Man"
        )

    def test_inner_article_kept(self):
        assert normalize_title("Batman: The Long Halloween") != normalize_title(
            "Batman Long Halloween"
        )

    def test_bare_article_survives(self):
        assert normalize_title("The") == "the"

    def test_sanitize(self):
        assert sanitize_filename('Spider-Man: No Way Home?') == "Spider-Man No Way Home"


class TestParseIssueLabel:
    def test_plain(self):
        assert parse_issue_label("Absolute Batman #15 (2025)") == "15"

    def test_variant_suffix_kept(self):
        assert parse_issue_label("Amazing Spider-Man #78.BEY (2021)") == "78.bey"

    def test_fractional(self):
        assert parse_issue_label("Spawn #10.5") == "10.5"

    def test_no_marker(self):
        assert parse_issue_label("Amazing Spider-Man 078 (2021)") is None


def _release(title, number="auto", end=None, year=None):
    return SourceRelease(
        source_name="getcomics", external_id="url", title=title,
        issue_number=parse_issue_number(title) if number == "auto" else number,
        issue_end=end,
        year=year if year is not None else parse_year(title),
    )


class TestReleaseCoversIssue:
    def test_plain_number_matches(self):
        issue = Issue(id=1, series_id=1, number=73.0, display_number="73",
                      released_at=datetime(2021, 8, 1))
        assert release_covers_issue(_release("Amazing Spider-Man #73 (2021)"), issue)

    def test_span_covers_issue(self):
        issue = Issue(id=1, series_id=1, number=2.0, display_number="2")
        assert release_covers_issue(_release("Saga #1 – 3 (2013)", number=1.0, end=3.0), issue)

    def test_variant_release_rejected_for_plain_issue(self):
        issue = Issue(id=1, series_id=1, number=78.0, display_number="78",
                      released_at=datetime(2021, 11, 1))
        assert not release_covers_issue(_release("Amazing Spider-Man #78.BEY (2021)"), issue)

    def test_variant_issue_matched_by_display_number(self):
        # the variant's sort number is synthetic (78.001), not in the span
        issue = Issue(id=1, series_id=1, number=78.001, display_number="78.BEY",
                      released_at=datetime(2021, 11, 1))
        assert release_covers_issue(_release("Amazing Spider-Man #78.BEY (2021)"), issue)

    def test_plain_release_rejected_for_variant_issue(self):
        issue = Issue(id=1, series_id=1, number=78.001, display_number="78.BEY")
        assert not release_covers_issue(_release("Amazing Spider-Man #78 (2021)"), issue)

    def test_relaunch_year_mismatch_rejected(self):
        # the 2018 series' #73 came out in 2021; a later relaunch reusing the
        # number must not satisfy it
        issue = Issue(id=1, series_id=1, number=73.0, display_number="73",
                      released_at=datetime(2021, 8, 1))
        assert not release_covers_issue(
            _release("The Amazing Spider-Man #73 (2028)"), issue
        )

    def test_year_off_by_one_allowed(self):
        issue = Issue(id=1, series_id=1, number=84.0, display_number="84",
                      released_at=datetime(2022, 1, 5))
        assert release_covers_issue(_release("Amazing Spider-Man #84 (2021)"), issue)

    def test_missing_years_allowed(self):
        issue = Issue(id=1, series_id=1, number=15.0, display_number="15")
        assert release_covers_issue(_release("Absolute Batman #15"), issue)

    def test_no_issue_number_rejected(self):
        issue = Issue(id=1, series_id=1, number=1.0, display_number="1")
        assert not release_covers_issue(
            _release("Amazing Spider-Man – Beyond Vol. 1 (TPB) (2021)", number=None), issue
        )
