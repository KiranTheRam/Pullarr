from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pullarr.db import _migrate_sqlite
from pullarr.models import Base, Issue, Series


@pytest.mark.asyncio
async def test_datetime_round_trips_as_aware_utc(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        series = Series(title="Series", sort_title="series")
        series.issues.append(
            Issue(number=1.0, display_number="1", released_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        )
        session.add(series)
        await session.commit()

    async with Session() as session:
        issue = await session.get(Issue, 1)
        assert issue.released_at.tzinfo is timezone.utc
        assert issue.released_at <= datetime.now(timezone.utc)

    await engine.dispose()


@pytest.mark.asyncio
async def test_migration_adds_display_number_and_removes_number_unique(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            """
            CREATE TABLE issues (
                id INTEGER NOT NULL PRIMARY KEY,
                series_id INTEGER NOT NULL,
                comicvine_id INTEGER,
                number FLOAT NOT NULL,
                volume INTEGER,
                title VARCHAR NOT NULL DEFAULT '',
                monitored BOOLEAN NOT NULL DEFAULT 1,
                downloaded BOOLEAN NOT NULL DEFAULT 0,
                file_path VARCHAR NOT NULL DEFAULT '',
                released_at DATETIME,
                UNIQUE (series_id, number)
            )
            """
        )
        await conn.exec_driver_sql(
            "INSERT INTO issues (id, series_id, number, title) VALUES (1, 1, 1.0, 'One')"
        )
        await _migrate_sqlite(conn)
        cols = {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(issues)")).fetchall()}
        assert "display_number" in cols
        row = (await conn.exec_driver_sql("SELECT display_number FROM issues WHERE id = 1")).one()
        assert row[0] == "1"
        indexes = (await conn.exec_driver_sql("PRAGMA index_list(issues)")).fetchall()
        unique_number_indexes = []
        for index in indexes:
            if not index[2]:
                continue
            cols = tuple(
                info[2]
                for info in (await conn.exec_driver_sql(f"PRAGMA index_info({index[1]})")).fetchall()
            )
            if cols == ("series_id", "number"):
                unique_number_indexes.append(index[1])
        assert unique_number_indexes == []

    await engine.dispose()
