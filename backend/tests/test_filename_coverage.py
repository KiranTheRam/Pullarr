"""Regression coverage for intact comic bundles and conservative cleanup."""

import zipfile
from unittest.mock import patch

import pytest

from pullarr.filename import parse_filename
from pullarr.library.cleanup import analyze, apply_cleanup
from pullarr.library.importer import import_payload
from pullarr.library.matcher import find_media_files, match_files
from pullarr.library.naming import DEFAULT_TEMPLATE
from pullarr.library.rename import plan_renames
from pullarr.library.scanner import scan_series
from pullarr.models import Issue, Series
from pullarr.util import parse_issue_label, parse_issue_number, parse_issue_range, parse_volume_number


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


def rows():
    return [Issue(id=n + 1, series_id=1, number=number, display_number=f"{number:g}",
                  title="", volume=1 if number <= 3 else 2, downloaded=False,
                  file_path="", file_source="", file_download_id=None)
            for n, number in enumerate([1., 1.5, 2., 3., 4.])]


def series():
    return Series(id=1, title="Saga", year=2012, folder_name="Saga (2012)", alt_titles="")


def archive(path, number=None):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("001.png", PNG)
        if number is not None:
            zf.writestr("ComicInfo.xml", f"<ComicInfo><Number>{number}</Number></ComicInfo>")
    return path


@pytest.mark.parametrize("stem,expected", [
    ("Saga #1-#3", [1., 1.5, 2., 3.]),
    ("Saga Issue 1.5-Issue 3", [1.5, 2., 3.]),
    ("Saga 001.5-003 (2012)", [1.5, 2., 3.]),
    ("Saga v09 (#1-3)", [1., 1.5, 2., 3.]),
    ("Saga [Issue 1-3]", [1., 1.5, 2., 3.]),
    ("Saga Vol. 1-2", [1., 1.5, 2., 3., 4.]),
    ("Saga v01 (Issue1fi7)", [1., 1.5, 2., 3.]),
    ("Saga #3-1", []),
    ("Saga #1 #3", []),
    ("Saga #1 003", []),
])
def test_match_and_scan_share_range_coverage_even_with_comicinfo(tmp_path, stem, expected):
    path = archive(tmp_path / f"{stem}.cbz", number="1")
    tracked = rows()
    matched = match_files(find_media_files(tmp_path), tracked)
    assert sorted(i.number for m in matched.matched for i in m.covered_issues) == expected
    scan_series(series(), tracked, [tmp_path])
    assert [i.number for i in tracked if i.downloaded] == expected
    assert all(i.file_path == str(path) for i in tracked if i.downloaded)


def test_helpers_never_collapse_spans_or_parse_partial_scene_tags():
    assert parse_issue_range("Saga #1.5-3") == (1.5, 3.)
    assert parse_issue_number("Saga #1-3") is None
    assert parse_issue_number("Saga (#1fi7)") is None
    assert parse_volume_number("Saga v1-3") is None
    assert parse_filename("Saga #3-1").ambiguous
    assert parse_filename("Saga #1 (#3)").ambiguous
    assert not parse_filename("Saga #1 (#001)").ambiguous


@pytest.mark.parametrize("stem,expected", [
    ("Saga #1.5-3", "Saga #001.5-003.cbz"),
    ("Saga v01-03", "Saga Vol. 01-03.cbz"),
    ("Saga v09 [Issue 1-3]", "Saga #001-003.cbz"),
])
def test_import_and_rename_preserve_full_bounds_with_one_tracked_issue(tmp_path, stem, expected):
    source = archive(tmp_path / f"{stem}.cbz", "1")
    root = tmp_path / "library"
    root.mkdir()
    tracked = rows()[2:3]
    imported = import_payload(source, series(), tracked, root, DEFAULT_TEMPLATE)
    assert imported[0].dest.name == expected
    assert imported[0].covered == tracked
    tracked[0].downloaded = True
    tracked[0].file_path = str(source)
    renamed = plan_renames(series(), tracked, DEFAULT_TEMPLATE)
    assert renamed[0].new_name == expected
    rescanned = rows()
    scan_series(series(), rescanned, [root])
    assert [i.number for i in rescanned if i.downloaded] == (
        [1.5, 2., 3.] if stem == "Saga #1.5-3" else
        [1., 1.5, 2., 3., 4.] if stem == "Saga v01-03" else [1., 1.5, 2., 3.]
    )


def test_loose_image_range_preserves_decimal_bounds(tmp_path):
    source = tmp_path / "Saga #1.5-3"
    source.mkdir()
    (source / "001.png").write_bytes(PNG)
    root = tmp_path / "library"
    root.mkdir()
    imported = import_payload(source, series(), rows(), root, DEFAULT_TEMPLATE)
    assert imported[0].dest.name == "Saga #001.5-003.cbz"
    assert [i.number for i in imported[0].covered] == [1.5, 2., 3.]


@pytest.mark.parametrize("stem", [
    "Saga #1-3", "Saga v1-3", "Saga v1 [#1-3]", "Saga #1 #3",
    "Saga #1 003", "Saga v1 (Issue1fi7)", "Saga #1.MU",
])
def test_cleanup_never_recommends_ambiguous_or_range_names(tmp_path, stem):
    real = archive(tmp_path / "Saga #001.cbz")
    archive(tmp_path / f"{stem}.cbz")
    tracked = rows()
    tracked[0].downloaded, tracked[0].file_path = True, str(real)
    plan = analyze(series(), tracked, [tmp_path], DEFAULT_TEMPLATE)
    assert not plan.groups
    assert all(item.keep for item in plan.orphans)
    result = apply_cleanup(series(), tracked, [tmp_path], [str(real)])
    assert result.deleted == 0
    assert real.exists()


def test_cleanup_protects_unreferenced_known_coverage_and_whole_batch(tmp_path):
    first = archive(tmp_path / "Saga #001.cbz")
    second = archive(tmp_path / "Saga Issue 1.cbz")
    result = apply_cleanup(series(), rows(), [tmp_path], [str(first), str(second)])
    assert (result.deleted, result.skipped) == (0, 2)
    assert first.exists() and second.exists()


def test_cleanup_does_not_shrink_manually_assigned_coverage(tmp_path):
    first = archive(tmp_path / "Saga #001.cbz")
    second = archive(tmp_path / "Saga Issue 1.cbz")
    tracked = rows()
    tracked[2].downloaded, tracked[2].file_path = True, str(first)
    result = apply_cleanup(series(), tracked, [tmp_path], [str(first)])
    assert (result.deleted, result.skipped) == (0, 1)
    assert first.exists() and second.exists()


def test_cleanup_repoints_only_after_successful_delete(tmp_path):
    first = archive(tmp_path / "Saga #001.cbz")
    archive(tmp_path / "Saga Issue 1.cbz")
    tracked = rows()
    tracked[0].downloaded, tracked[0].file_path = True, str(first)
    tracked[0].file_source, tracked[0].file_download_id = "torrent", 9
    with patch("pullarr.library.cleanup.os.remove", side_effect=OSError("unavailable")):
        result = apply_cleanup(series(), tracked, [tmp_path], [str(first)])
    assert (result.deleted, result.skipped) == (0, 1)
    assert tracked[0].file_path == str(first)
    assert (tracked[0].file_source, tracked[0].file_download_id) == ("torrent", 9)


def test_cleanup_range_can_only_be_manually_deleted_when_all_coverage_survives(tmp_path):
    bundle = archive(tmp_path / "Saga #1-3.cbz")
    tracked = rows()
    for issue in tracked[:4]:
        archive(tmp_path / f"Saga #{issue.number:g}.cbz")
        issue.downloaded, issue.file_path = True, str(bundle)
    result = apply_cleanup(series(), tracked, [tmp_path], [str(bundle)])
    assert (result.deleted, result.repointed) == (1, 4)
    assert all(i.file_path != str(bundle) for i in tracked[:4])


def test_scan_replaces_missing_owned_file_with_range_and_prefers_exact_issue(tmp_path):
    bundle = archive(tmp_path / "Saga #1-3.cbz")
    exact = archive(tmp_path / "Saga #2.cbz")
    tracked = rows()
    tracked[0].downloaded, tracked[0].file_path = True, str(tmp_path / "missing.cbz")
    tracked[0].file_source, tracked[0].file_download_id = "old", 4
    scan_series(series(), tracked, [tmp_path])
    assert tracked[0].file_path == str(bundle)
    assert (tracked[0].file_source, tracked[0].file_download_id) == ("", None)
    assert tracked[2].file_path == str(exact)
    assert scan_series(series(), tracked, [tmp_path]).matched_issues == 0



def test_untracked_explicit_issue_never_falls_back_to_collected_volume(tmp_path):
    archive(tmp_path / "Saga v01 #99.cbz")
    result = match_files(find_media_files(tmp_path), rows())
    assert not result.matched
    assert len(result.unmatched) == 1


def test_issue_range_precedes_volume_mapping_and_corrects_previous_scan(tmp_path):
    volume = archive(tmp_path / "Saga Vol. 01.cbz")
    bundle = archive(tmp_path / "Saga #1-2.cbz")
    tracked = rows()
    for issue in tracked[:4]:
        issue.downloaded, issue.file_path = True, str(volume)
    scan_series(series(), tracked, [tmp_path])
    assert all(issue.file_path == str(bundle) for issue in tracked[:3])
    assert tracked[3].file_path == str(volume)


@pytest.mark.parametrize("stem", ["Saga #3-1", "Saga #9-12", "Saga v9-12"])
def test_force_issue_cannot_collapse_ambiguous_or_untracked_range(tmp_path, stem):
    source = archive(tmp_path / f"{stem}.cbz")
    root = tmp_path / "library"
    root.mkdir()
    tracked = rows()
    result = import_payload(source, series(), tracked, root, DEFAULT_TEMPLATE, force_issue=tracked[0])
    assert result[0].issue is None
    assert result[0].covered == []
    assert '#001.cbz' not in result[0].dest.name



def test_explicit_single_issue_overrides_a_volume_span_when_renaming(tmp_path):
    source = archive(tmp_path / "Saga v1-3 #1.cbz")
    tracked = rows()[:1]
    tracked[0].downloaded, tracked[0].file_path = True, str(source)
    assert plan_renames(series(), tracked, DEFAULT_TEMPLATE)[0].new_name == "Saga #001.cbz"


@pytest.mark.parametrize("label", ["1A", "1.MU", "78.BEY"])
@pytest.mark.parametrize("metadata", [False, True])
def test_variant_labels_match_exactly_in_scan_and_pack_import(tmp_path, label, metadata):
    # Numeric prefixes retain the base branch's semantics, but variant identity
    # must survive even when the provider assigned it a different sort number.
    number = float(label.split('.')[0].rstrip('A'))
    assert parse_issue_number(f"Saga #{label}") == number
    assert parse_issue_label(f"Saga #{label}") == label.casefold()
    payload = tmp_path / "payload"
    payload.mkdir()
    source = archive(payload / ("Unknown.cbz" if metadata else f"Saga #{label}.cbz"), label if metadata else None)
    archive(payload / "Saga #002.cbz")  # exercise a pack, without force_issue
    tracked = [
        Issue(id=1, number=number, display_number=f"{number:g}", title="", downloaded=False),
        Issue(id=2, number=number + .001, display_number=label, title="", downloaded=False),
        Issue(id=3, number=2., display_number="2", title="", downloaded=False),
    ]
    scan_series(series(), tracked, [payload])
    assert not tracked[0].downloaded
    assert tracked[1].downloaded and tracked[1].file_path == str(source)
    root = tmp_path / "library"
    root.mkdir()
    result = import_payload(payload, series(), tracked, root, DEFAULT_TEMPLATE)
    variant = next(item for item in result if item.issue is tracked[1])
    assert variant.dest.name == f"Saga #{label}.cbz"
    assert variant.covered == [tracked[1]]


@pytest.mark.parametrize("metadata", [False, True])
def test_untracked_variant_cannot_match_plain_issue_or_prove_cleanup(tmp_path, metadata):
    archive(tmp_path / "Saga #1A.cbz", "1A" if metadata else None)
    tracked = rows()
    assert not match_files(find_media_files(tmp_path), tracked).matched
    real = archive(tmp_path / "Saga #001.cbz")
    tracked[0].downloaded, tracked[0].file_path = True, str(real)
    assert apply_cleanup(series(), tracked, [tmp_path], [str(real)]).deleted == 0


@pytest.mark.parametrize("stem", ["Saga #1A-3", "Saga #1-3A", "Saga #1A #3", "Saga #1A 001"])
def test_variant_endpoints_and_conflicts_do_not_collapse_to_single_issue(tmp_path, stem):
    source = archive(tmp_path / f"{stem}.cbz", "1A")
    tracked = rows() + [Issue(id=10, number=1.001, display_number="1A", title="", downloaded=False)]
    assert parse_filename(stem).ambiguous
    assert not match_files(find_media_files(source), tracked).matched
    root = tmp_path / "library"
    root.mkdir()
    result = import_payload(source, series(), tracked, root, DEFAULT_TEMPLATE, force_issue=tracked[-1])
    assert not result[0].covered


@pytest.mark.parametrize("stem,expected", [("Saga #1-3", [1., 1.5, 2., 3.]), ("Saga Vol. 1-2", [1., 1.5, 2., 3., 4.])])
def test_variant_comicinfo_cannot_narrow_numeric_collections(tmp_path, stem, expected):
    source = archive(tmp_path / f"{stem}.cbz", "1A")
    tracked = rows() + [Issue(id=10, number=1.001, display_number="1A", title="", downloaded=False)]
    result = match_files(find_media_files(source), tracked)
    assert sorted(issue.number for item in result.matched for issue in item.covered_issues) == expected


@pytest.mark.parametrize("metadata", [False, True])
def test_zero_padded_variant_label_matches_display_identity(tmp_path, metadata):
    source = archive(tmp_path / "Saga #001A.cbz", "001a" if metadata else None)
    tracked = [Issue(id=1, number=1.001, display_number="1A")]
    assert match_files(find_media_files(source), tracked).matched[0].issue is tracked[0]


def test_unknown_metadata_label_cannot_fall_back_to_a_volume(tmp_path):
    source = archive(tmp_path / "Unknown.cbz")
    with zipfile.ZipFile(source, "a") as zf:
        zf.writestr("ComicInfo.xml", "<ComicInfo><Number>1A</Number><Volume>1</Volume></ComicInfo>")
    assert not match_files(find_media_files(source), rows()).matched
