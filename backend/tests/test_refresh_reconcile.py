import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr.jobs.tasks import reconcile_downloaded_files
from pullarr.models import Base, Issue, Series


class FakeSession:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_reconcile_clears_downloaded_when_file_missing(tmp_path):
    present = tmp_path / "present.cbz"
    present.write_bytes(b"cbz")
    missing = tmp_path / "missing.cbz"
    series = Series(id=1, title="Kagurabachi")
    issues = [
        Issue(id=1, series_id=1, number=1, downloaded=True, file_path=str(present)),
        Issue(id=2, series_id=1, number=2, downloaded=True, file_path=str(missing)),
        Issue(id=3, series_id=1, number=3, downloaded=False, file_path=""),
    ]
    series.issues = issues
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            session.add(series)
            await session.commit()
            assert await reconcile_downloaded_files(session, series) == 1
        async with maker() as session:
            existing, gone, pending = [await session.get(Issue, issue.id) for issue in issues]
            assert existing.downloaded and existing.file_path == str(present)
            assert not gone.downloaded and gone.file_path == ""
            assert not pending.downloaded
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_does_not_commit_when_nothing_changed(tmp_path):
    present = tmp_path / "present.cbz"
    present.write_bytes(b"cbz")
    series = Series(id=1, title="Kagurabachi")
    series.issues = [
        Issue(id=1, series_id=1, number=1, downloaded=True, file_path=str(present)),
        Issue(id=2, series_id=1, number=2, downloaded=False, file_path=""),
    ]
    session = FakeSession()

    changed = await reconcile_downloaded_files(session, series)

    assert changed == 0
    assert session.commits == 0
