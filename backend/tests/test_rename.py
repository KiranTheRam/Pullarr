from pullarr.library.naming import DEFAULT_TEMPLATE
from pullarr.library.rename import apply_renames, plan_renames
from pullarr.models import Issue, Series


def series():
    return Series(id=1, title="Absolute Batman", year=2024, folder_name="", alt_titles="")


def issue(id, number, path, volume=None, title=""):
    return Issue(id=id, series_id=1, number=float(number), volume=volume, title=title,
                 downloaded=True, file_path=str(path))


def plan(s, issues):
    return plan_renames(s, issues, DEFAULT_TEMPLATE)


class TestPlanRenames:
    def test_renames_offconvention_issue_file(self, tmp_path):
        f = tmp_path / "Absolute_Batman_015_(2026)_(Webrip).cbz"
        f.write_bytes(b"x")
        items = plan(series(), [issue(1, 15, f)])
        assert len(items) == 1
        assert items[0].new_name == "Absolute Batman #015.cbz"
        assert not items[0].conflict

    def test_convention_named_file_skipped(self, tmp_path):
        f = tmp_path / "Absolute Batman #015.cbz"
        f.write_bytes(b"x")
        assert plan(series(), [issue(1, 15, f)]) == []

    def test_format_preserved_for_cbr(self, tmp_path):
        f = tmp_path / "Absolute Batman 015.cbr"
        f.write_bytes(b"x")
        items = plan(series(), [issue(1, 15, f)])
        assert items[0].new_name == "Absolute Batman #015.cbr"

    def test_volume_archive_named_by_volume(self, tmp_path):
        f = tmp_path / "absolute batman v01 (2025).cbz"
        f.write_bytes(b"x")
        issues = [issue(1, 1, f, volume=1), issue(2, 2, f, volume=1)]
        items = plan(series(), issues)
        assert len(items) == 1
        assert items[0].new_name == "Absolute Batman Vol. 01.cbz"
        assert sorted(items[0].issue_ids) == [1, 2]

    def test_conflict_flagged(self, tmp_path):
        f = tmp_path / "Absolute Batman 015.cbz"
        f.write_bytes(b"x")
        (tmp_path / "Absolute Batman #015.cbz").write_bytes(b"other")
        items = plan(series(), [issue(1, 15, f)])
        assert items[0].conflict

    def test_ordering_volumes_then_issues_numeric(self, tmp_path):
        files = {
            "v10": tmp_path / "Series v10.cbz",
            "v2": tmp_path / "Series v2.cbz",
            "i3": tmp_path / "Series 003.cbz",
        }
        for f in files.values():
            f.write_bytes(b"x")
        s = Series(id=1, title="Series", folder_name="", alt_titles="")
        issues = [
            issue(1, 30, files["v10"], volume=10),
            issue(2, 5, files["v2"], volume=2),
            issue(3, 3, files["i3"]),
        ]
        items = plan(s, issues)
        assert [i.new_name for i in items] == [
            "Series Vol. 02.cbz", "Series Vol. 10.cbz", "Series #003.cbz",
        ]


class TestApplyRenames:
    def test_moves_and_repoints(self, tmp_path):
        f = tmp_path / "Absolute Batman 015.cbz"
        f.write_bytes(b"x")
        i = issue(1, 15, f)
        items = plan(series(), [i])

        outcomes = apply_renames(items, {1: i})

        assert outcomes[0].status == "renamed"
        new = tmp_path / "Absolute Batman #015.cbz"
        assert new.exists() and not f.exists()
        assert i.file_path == str(new)

    def test_never_overwrites(self, tmp_path):
        f = tmp_path / "Absolute Batman 015.cbz"
        f.write_bytes(b"x")
        target = tmp_path / "Absolute Batman #015.cbz"
        target.write_bytes(b"existing")
        i = issue(1, 15, f)
        items = plan(series(), [i])

        outcomes = apply_renames(items, {1: i})

        assert outcomes[0].status == "skipped-collision"
        assert target.read_bytes() == b"existing"
        assert f.exists()

    def test_missing_source_skipped(self, tmp_path):
        f = tmp_path / "Absolute Batman 015.cbz"
        f.write_bytes(b"x")
        i = issue(1, 15, f)
        items = plan(series(), [i])
        f.unlink()

        outcomes = apply_renames(items, {1: i})
        assert outcomes[0].status == "skipped-missing"
