import zipfile

from pullarr.library.matcher import find_media_files, match_files
from pullarr.models import Issue

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def issues(*specs):
    """specs: (number, volume) tuples."""
    return [Issue(id=i + 1, series_id=1, number=float(n), volume=v)
            for i, (n, v) in enumerate(specs)]


def touch(path, content=b"x"):
    path.write_bytes(content)


class TestFindMediaFiles:
    def test_parses_varied_real_names(self, tmp_path):
        for name in [
            "Absolute Batman #015.cbz",
            "Batman Vol. 3.cbr",
            "Absolute_Batman_015_(2026)_(Webrip)_(The_Last_Kryptonian-DCP).cbr",
            "Saga 061 (2026) (Digital) (Zone-Empire).cbz",
            "Monstress v08 (2023).cbz",
        ]:
            touch(tmp_path / name)
        touch(tmp_path / "info.json")  # sidecar ignored

        found = {m.path.name: (m.issue_number, m.volume_number)
                 for m in find_media_files(tmp_path)}
        assert "info.json" not in found
        assert found["Absolute Batman #015.cbz"] == (15.0, None)
        assert found["Batman Vol. 3.cbr"] == (None, 3)
        assert found[
            "Absolute_Batman_015_(2026)_(Webrip)_(The_Last_Kryptonian-DCP).cbr"
        ][0] == 15.0
        assert found["Saga 061 (2026) (Digital) (Zone-Empire).cbz"][0] == 61.0
        assert found["Monstress v08 (2023).cbz"] == (None, 8)

    def test_series_iteration_with_issue_marker_is_issue(self, tmp_path):
        # "v2" is the series iteration here, not a TPB — the explicit #12 wins
        touch(tmp_path / "Batman v2 #12.cbz")
        found = find_media_files(tmp_path)
        assert found[0].issue_number == 12.0
        assert found[0].volume_number == 2

    def test_loose_image_dir(self, tmp_path):
        d = tmp_path / "Issue 5"
        d.mkdir()
        touch(d / "01.png", PNG)
        found = find_media_files(tmp_path)
        assert len(found) == 1
        assert found[0].is_dir and found[0].issue_number == 5.0


class TestMatchFiles:
    def test_issue_and_volume_coverage(self, tmp_path):
        tracked = issues((1, 1), (2, 1), (3, 1), (10, 2))
        for name in ["Series #003.cbz", "Series Vol. 01.cbz", "Random extra.cbz"]:
            touch(tmp_path / name)
        res = match_files(find_media_files(tmp_path), tracked)

        by_name = {m.media.path.name: m for m in res.matched}
        # issue file → single issue
        assert by_name["Series #003.cbz"].issue.number == 3.0
        # volume 1 archive covers issues 1,2,3 (mapped via manual range map)
        vol = by_name["Series Vol. 01.cbz"]
        assert vol.issue is None and vol.volume == 1
        assert sorted(i.number for i in vol.covered_issues) == [1.0, 2.0, 3.0]
        # unmatched
        assert [m.path.name for m in res.unmatched] == ["Random extra.cbz"]

    def test_volume_without_tracked_issues_still_tagged(self, tmp_path):
        tracked = issues((1, 1))
        touch(tmp_path / "Series Vol. 09.cbz")
        res = match_files(find_media_files(tmp_path), tracked)
        m = res.matched[0]
        assert m.volume == 9 and m.covered_issues == [] and not res.unmatched

    def test_multi_issue_bundle_covers_span(self, tmp_path):
        tracked = issues((1, None), (2, None), (3, None), (4, None))
        touch(tmp_path / "Series 001-003 (2019).cbz")
        res = match_files(find_media_files(tmp_path), tracked)
        m = res.matched[0]
        assert m.issue is None
        assert sorted(i.number for i in m.covered_issues) == [1.0, 2.0, 3.0]

    def test_bundle_file_parsed_as_range_not_single(self, tmp_path):
        touch(tmp_path / "Series #1-3.cbz")
        mf = find_media_files(tmp_path)[0]
        assert mf.issue_number is None
        assert mf.issue_range == (1.0, 3.0)
