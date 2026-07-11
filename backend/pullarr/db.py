from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import config

engine = create_async_engine(config.db_url, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """For background jobs that need their own session."""
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from . import models  # noqa: F401 — register mappings

    config.data_dir.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
        if conn.dialect.name == "sqlite":
            await _migrate_sqlite(conn)


async def _migrate_sqlite(conn) -> None:
    """Small idempotent migrations for SQLite installs.

    SQLAlchemy's create_all intentionally does not alter existing tables. Keep
    migrations here until the project grows enough to justify Alembic.
    """
    if not await _table_exists(conn, "issues"):
        return
    columns = await _columns(conn, "issues")
    has_display_number = "display_number" in columns
    has_old_unique = await _has_unique_index(conn, "issues", ("series_id", "number"))
    if has_display_number and not has_old_unique:
        await _ensure_issue_indexes(conn)
        await conn.exec_driver_sql(
            """
            UPDATE issues
            SET display_number = CASE
                WHEN number = CAST(number AS INTEGER) THEN CAST(CAST(number AS INTEGER) AS TEXT)
                ELSE CAST(number AS TEXT)
            END
            WHERE display_number IS NULL OR display_number = ''
            """
        )
    else:
        await _rebuild_issues_table(conn, has_display_number)

    await _ensure_columns(conn, "series", {
        "metron_id": "INTEGER",
        "metadata_refreshed_at": "DATETIME",
    })
    await _ensure_columns(conn, "issues", {
        "metron_id": "INTEGER",
        "summary": "TEXT NOT NULL DEFAULT ''",
        "imprint": "VARCHAR NOT NULL DEFAULT ''",
        "writers": "TEXT NOT NULL DEFAULT ''",
        "pencillers": "TEXT NOT NULL DEFAULT ''",
        "inkers": "TEXT NOT NULL DEFAULT ''",
        "colorists": "TEXT NOT NULL DEFAULT ''",
        "letterers": "TEXT NOT NULL DEFAULT ''",
        "cover_artists": "TEXT NOT NULL DEFAULT ''",
        "editors": "TEXT NOT NULL DEFAULT ''",
        "translators": "TEXT NOT NULL DEFAULT ''",
        "story_arcs": "TEXT NOT NULL DEFAULT ''",
        "reprints": "TEXT NOT NULL DEFAULT ''",
        "characters": "TEXT NOT NULL DEFAULT ''",
        "teams": "TEXT NOT NULL DEFAULT ''",
        "genres": "TEXT NOT NULL DEFAULT ''",
        "web_url": "VARCHAR NOT NULL DEFAULT ''",
        "format": "VARCHAR NOT NULL DEFAULT ''",
        "language": "VARCHAR NOT NULL DEFAULT ''",
        "page_count": "INTEGER",
        "metadata_refreshed_at": "DATETIME",
    })
    await _ensure_columns(conn, "downloads", {
        "error_code": "VARCHAR NOT NULL DEFAULT ''",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "next_retry_at": "DATETIME",
        "blocked": "BOOLEAN NOT NULL DEFAULT 0",
    })
    if await _table_exists(conn, "series"):
        await conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_series_metron_id "
            "ON series (metron_id) WHERE metron_id IS NOT NULL"
        )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_issues_metron_id ON issues (metron_id)"
    )


async def _table_exists(conn, name: str) -> bool:
    result = await conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    )
    return result.first() is not None


async def _columns(conn, table: str) -> set[str]:
    result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
    return {row[1] for row in result.fetchall()}


async def _ensure_columns(conn, table: str, definitions: dict[str, str]) -> None:
    """Idempotently add simple columns to existing SQLite installations."""
    if not await _table_exists(conn, table):
        return
    existing = await _columns(conn, table)
    for name, ddl in definitions.items():
        if name not in existing:
            await conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')


async def _has_unique_index(conn, table: str, columns: tuple[str, ...]) -> bool:
    result = await conn.exec_driver_sql(f"PRAGMA index_list({table})")
    for row in result.fetchall():
        index_name = row[1]
        is_unique = bool(row[2])
        if not is_unique:
            continue
        info = await conn.exec_driver_sql(f"PRAGMA index_info({index_name})")
        indexed_columns = tuple(col[2] for col in info.fetchall())
        if indexed_columns == columns:
            return True
    return False


async def _rebuild_issues_table(conn, has_display_number: bool) -> None:
    display_expr = (
        "COALESCE(NULLIF(display_number, ''), "
        "CASE WHEN number = CAST(number AS INTEGER) "
        "THEN CAST(CAST(number AS INTEGER) AS TEXT) ELSE CAST(number AS TEXT) END)"
        if has_display_number
        else "CASE WHEN number = CAST(number AS INTEGER) "
        "THEN CAST(CAST(number AS INTEGER) AS TEXT) ELSE CAST(number AS TEXT) END"
    )
    await conn.exec_driver_sql("ALTER TABLE issues RENAME TO issues_old")
    await conn.exec_driver_sql(
        """
        CREATE TABLE issues (
            id INTEGER NOT NULL PRIMARY KEY,
            series_id INTEGER NOT NULL,
            comicvine_id INTEGER,
            number FLOAT NOT NULL,
            display_number VARCHAR NOT NULL DEFAULT '',
            volume INTEGER,
            title VARCHAR NOT NULL DEFAULT '',
            monitored BOOLEAN NOT NULL DEFAULT 1,
            downloaded BOOLEAN NOT NULL DEFAULT 0,
            file_path VARCHAR NOT NULL DEFAULT '',
            released_at DATETIME,
            FOREIGN KEY(series_id) REFERENCES series (id)
        )
        """
    )
    await conn.exec_driver_sql(
        f"""
        INSERT INTO issues (
            id, series_id, comicvine_id, number, display_number, volume, title,
            monitored, downloaded, file_path, released_at
        )
        SELECT
            id, series_id, comicvine_id, number, {display_expr}, volume,
            COALESCE(title, ''), COALESCE(monitored, 1),
            COALESCE(downloaded, 0), COALESCE(file_path, ''), released_at
        FROM issues_old
        """
    )
    await conn.exec_driver_sql("DROP TABLE issues_old")
    await _ensure_issue_indexes(conn)


async def _ensure_issue_indexes(conn) -> None:
    await conn.exec_driver_sql(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ix_issues_series_comicvine
        ON issues (series_id, comicvine_id)
        WHERE comicvine_id IS NOT NULL
        """
    )
    await conn.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS ix_issues_series_number
        ON issues (series_id, number)
        """
    )
