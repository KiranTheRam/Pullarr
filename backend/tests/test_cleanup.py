from pullarr.library.cleanup import analyze, apply_cleanup
from pullarr.library.naming import DEFAULT_TEMPLATE
from pullarr.models import Issue, Series


def series():
    return Series(id=1, title="Saga", year=2012, folder_name="Saga (2012)", alt_titles="")


def issue(id, number, volume=None, downloaded=False, file_path=""):
    return Issue(id=id, series_id=1, number=float(number), volume=volume,
                 downloaded=downloaded, file_path=file_path, title="")


def run_analyze(s, issues, folder):
    return analyze(s, issues, [folder], DEFAULT_TEMPLATE)


class TestAnalyze:
    def test_duplicate_issue_grouped_referenced_kept(self, tmp_path):
        folder = tmp_path / "Saga (2012)"
        folder.mkdir()
        a = folder / "Saga #003.cbz"
        b = folder / "Saga 003 (2013) (digital).cbz"
        a.write_bytes(b"aa")
        b.write_bytes(b"bbbb")
        issues = [issue(1, 3, downloaded=True, file_path=str(b))]

        plan = run_analyze(series(), issues, folder)

        assert len(plan.groups) == 1
        group = plan.groups[0]
        assert group.label == "Issue 3"
        by_name = {f.name: f for f in group.files}
        assert by_name["Saga 003 (2013) (digital).cbz"].keep  # referenced wins
        assert not by_name["Saga #003.cbz"].keep

    def test_orphan_covered_issue_marked_deletable(self, tmp_path):
        folder = tmp_path / "Saga (2012)"
        folder.mkdir()
        kept = folder / "Saga #003.cbz"
        kept.write_bytes(b"x")
        extra = folder / "Saga Vol. 01.cbz"
        extra.write_bytes(b"y")
        issues = [issue(1, 3, volume=1, downloaded=True, file_path=str(kept))]

        plan = run_analyze(series(), issues, folder)

        # the volume archive duplicates a fully-downloaded volume → deletable
        assert len(plan.orphans) == 1
        assert plan.orphans[0].name == "Saga Vol. 01.cbz"
        assert not plan.orphans[0].keep

    def test_unknown_extra_kept_by_default(self, tmp_path):
        folder = tmp_path / "Saga (2012)"
        folder.mkdir()
        (folder / "Saga Artbook.cbz").write_bytes(b"x")

        plan = run_analyze(series(), [issue(1, 3)], folder)

        assert plan.orphans[0].keep


class TestApplyCleanup:
    def test_deletes_and_repoints(self, tmp_path):
        folder = tmp_path / "Saga (2012)"
        folder.mkdir()
        a = folder / "Saga #003.cbz"
        b = folder / "Saga 003 (2013) (digital).cbz"
        a.write_bytes(b"aa")
        b.write_bytes(b"bbbb")
        i = issue(1, 3, downloaded=True, file_path=str(b))

        result = apply_cleanup(series(), [i], [folder], [str(b)])

        assert result.deleted == 1
        assert result.repointed == 1
        assert not b.exists()
        assert i.file_path == str(a)

    def test_refuses_to_delete_last_copy(self, tmp_path):
        folder = tmp_path / "Saga (2012)"
        folder.mkdir()
        only = folder / "Saga #003.cbz"
        only.write_bytes(b"x")
        i = issue(1, 3, downloaded=True, file_path=str(only))

        result = apply_cleanup(series(), [i], [folder], [str(only)])

        assert result.skipped == 1
        assert only.exists()
        assert i.file_path == str(only)
