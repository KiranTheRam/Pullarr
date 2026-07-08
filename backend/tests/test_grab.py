from datetime import datetime

import pytest

from pullarr.jobs import tasks
from pullarr.models import Download, Issue, Series
from pullarr.sources.base import SourceRelease


def _release(number, title, ext_id="url", end=None):
    return SourceRelease(
        source_name="getcomics", external_id=ext_id, title=title,
        issue_number=number, issue_end=end,
    )


class Recorder:
    """Stand-in session that records enqueue_direct calls."""

    def __init__(self):
        self.grabbed: list[tuple[float, str]] = []


@pytest.mark.asyncio
async def test_grab_matches_enqueues_matching_and_pops(monkeypatch):
    async def fake_enqueue(session, series, issue, source_name, external_id, title=""):
        session.grabbed.append((issue.number, title))

    monkeypatch.setattr(tasks, "enqueue_direct", fake_enqueue)

    series = Series(id=1, title="Spider-Gwen: The Ghost-Spider", alt_titles="")
    remaining = {n: Issue(id=int(n), series_id=1, number=n) for n in (12.0, 13.0, 14.0)}
    wanted = {tasks.normalize_title("Spider-Gwen: The Ghost-Spider")}
    releases = [
        _release(14.0, "Spider-Gwen – The Ghost-Spider #14 (2025)"),
        _release(13.0, "Spider-Gwen – The Ghost-Spider #13 (2025)"),
        # wrong series — must not match even though the number is wanted
        _release(12.0, "All-New Spider-Gwen – Ghost-Spider #12 (2026)"),
        # a TPB (no issue number) — ignored
        _release(None, "Spider-Gwen – The Ghost-Spider Vol. 2 (TPB) (2025)"),
    ]
    session = Recorder()

    count = await tasks._grab_matches(session, series, "getcomics", releases,
                                      remaining, wanted, failed_pairs=set())

    assert count == 2
    assert sorted(n for n, _ in session.grabbed) == [13.0, 14.0]
    # matched numbers are removed; the wrong-series #12 stays wanted
    assert set(remaining) == {12.0}


@pytest.mark.asyncio
async def test_grab_matches_bundle_grabbed_once_covers_span(monkeypatch):
    async def fake_enqueue(session, series, issue, source_name, external_id, title=""):
        session.grabbed.append((issue.number, title))

    monkeypatch.setattr(tasks, "enqueue_direct", fake_enqueue)

    series = Series(id=1, title="Absolute Carnage: Miles Morales", alt_titles="")
    remaining = {n: Issue(id=int(n), series_id=1, number=n) for n in (1.0, 2.0, 3.0)}
    releases = [
        _release(1.0, "Absolute Carnage – Miles Morales #1 – 3 (2019)", end=3.0),
    ]
    session = Recorder()

    count = await tasks._grab_matches(
        session, series, "getcomics", releases, remaining,
        {tasks.normalize_title("Absolute Carnage: Miles Morales")}, failed_pairs=set(),
    )

    # one grab (anchored to #1), and all three issues removed from wanted
    assert count == 1
    assert session.grabbed == [(1.0, "Absolute Carnage – Miles Morales #1 – 3 (2019)")]
    assert remaining == {}


@pytest.mark.asyncio
async def test_grab_matches_skips_failed_pairs(monkeypatch):
    async def fake_enqueue(session, series, issue, source_name, external_id, title=""):
        session.grabbed.append((issue.number, title))

    monkeypatch.setattr(tasks, "enqueue_direct", fake_enqueue)

    series = Series(id=1, title="Absolute Batman", alt_titles="")
    issue = Issue(id=5, series_id=1, number=5.0)
    remaining = {5.0: issue}
    releases = [_release(5.0, "Absolute Batman #5 (2025)")]
    session = Recorder()

    count = await tasks._grab_matches(
        session, series, "getcomics", releases, remaining,
        {tasks.normalize_title("Absolute Batman")},
        failed_pairs={(5, "getcomics")},
    )
    assert count == 0
    assert session.grabbed == []
    assert set(remaining) == {5.0}


def test_releasable_accepts_naive_sqlite_datetime():
    issue = Issue(id=1, series_id=1, number=1.0, released_at=datetime(2026, 1, 1))
    assert tasks._releasable(issue)


def test_download_covered_issue_ids_expands_title_range():
    dl = Download(id=1, issue_id=1, title="Absolute Batman #1-3 (2025)")
    issues = [Issue(id=n, series_id=1, number=float(n)) for n in (1, 2, 3, 4)]

    assert tasks._download_covered_issue_ids(dl, issues) == {1, 2, 3}


@pytest.mark.asyncio
async def test_grab_matches_skips_failed_release_payload(monkeypatch):
    async def fake_enqueue(session, series, issue, source_name, external_id, title=""):
        session.grabbed.append((issue.number, title))

    monkeypatch.setattr(tasks, "enqueue_direct", fake_enqueue)

    series = Series(id=1, title="Absolute Batman", alt_titles="")
    issue = Issue(id=5, series_id=1, number=5.0)
    remaining = {5.0: issue}
    releases = [_release(5.0, "Absolute Batman #5 (2025)", ext_id="dead-url")]
    session = Recorder()

    count = await tasks._grab_matches(
        session, series, "getcomics", releases, remaining,
        {tasks.normalize_title("Absolute Batman")},
        failed_pairs=set(),
        failed_releases={("getcomics", "dead-url")},
    )

    assert count == 0
    assert session.grabbed == []
    assert set(remaining) == {5.0}
