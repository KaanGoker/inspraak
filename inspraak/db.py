"""Postgres access: connections, pool, plain SQL migrations.

Later stages import `connect`, `get_pool` and `migrate` from here and the query
helpers from `inspraak.store`. Migrations are numbered .sql files in
`inspraak/migrations/`, applied once each, in order.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from inspraak.config import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_pool: ConnectionPool | None = None


def _configure(conn: psycopg.Connection) -> None:
    try:
        register_vector(conn)
    except psycopg.ProgrammingError:
        # The vector extension does not exist before the first migration.
        conn.rollback()


def connect(database_url: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    """A single connection with dict rows and pgvector types registered."""
    conn = psycopg.connect(
        database_url or get_settings().database_url, row_factory=dict_row, autocommit=autocommit
    )
    _configure(conn)
    if not autocommit:
        conn.commit()
    return conn


def get_pool() -> ConnectionPool:
    """Process-wide pool for the web app and workers."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            get_settings().database_url,
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            configure=_configure,
            open=True,
        )
    return _pool


@contextmanager
def session(database_url: str | None = None) -> Iterator[psycopg.Connection]:
    """Open a connection, commit on success, roll back on error, always close."""
    conn = connect(database_url)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def migrate(database_url: str | None = None) -> list[str]:
    """Apply pending migrations. Returns the names applied in this call."""
    applied_now: list[str] = []
    with psycopg.connect(database_url or get_settings().database_url) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.commit()
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in migration_files():
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied_now.append(path.name)
    return applied_now


def start_run(conn: psycopg.Connection, source: str) -> int:
    row = conn.execute(
        "INSERT INTO ingest_runs (source) VALUES (%s) RETURNING id", (source,)
    ).fetchone()
    conn.commit()
    return row["id"]


def finish_run(conn: psycopg.Connection, run_id: int, ok: bool, stats: dict) -> None:
    from psycopg.types.json import Jsonb

    conn.execute(
        "UPDATE ingest_runs SET finished_at = now(), ok = %s, stats = %s WHERE id = %s",
        (ok, Jsonb(stats), run_id),
    )
    conn.commit()
