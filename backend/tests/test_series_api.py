import asyncio
from types import SimpleNamespace

import pytest

from pullarr.api import series as series_api
from pullarr.models import Series


class FakeSession:
    async def get(self, model, object_id):
        assert model is Series
        return Series(id=object_id, title="Powers of X", monitored=False)


@pytest.mark.asyncio
async def test_explicit_search_missing_ignores_monitoring(monkeypatch):
    scheduled: dict[str, object] = {}

    async def fake_create_job(session, kind, series_id):
        return SimpleNamespace(id=42)

    async def fake_refresh_series_full(
        series_id, grab_missing=False, only_monitored=True, job_id=None
    ):
        scheduled.update(
            series_id=series_id,
            grab_missing=grab_missing,
            only_monitored=only_monitored,
            job_id=job_id,
        )

    monkeypatch.setattr(series_api, "create_job", fake_create_job)
    monkeypatch.setattr(series_api, "refresh_series_full", fake_refresh_series_full)

    response = await series_api.search_missing_issues(5, FakeSession())
    await asyncio.sleep(0)

    assert response == {"status": "searching", "job_id": 42}
    assert scheduled == {
        "series_id": 5,
        "grab_missing": True,
        "only_monitored": False,
        "job_id": 42,
    }
