from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
TEST_DB_NAME = f"inspraak_test_{os.getpid()}"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def load_json():
    def _load(name: str):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return _load


def _test_url() -> str:
    """A separate database in the same Postgres, so tests never touch the snapshot."""
    from inspraak.config import get_settings

    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    base = get_settings().database_url
    head, _, _ = base.rpartition("/")
    return f"{head}/{TEST_DB_NAME}"


@pytest.fixture(scope="session")
def db_url():
    import psycopg

    from inspraak import db
    from inspraak.config import get_settings

    url = _test_url()
    admin_url = get_settings().database_url
    try:
        with psycopg.connect(admin_url, connect_timeout=2, autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB_NAME,)
            ).fetchone()
            if not exists and not os.environ.get("TEST_DATABASE_URL"):
                conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    except psycopg.OperationalError:
        pytest.skip("Postgres not reachable; start it with `make up`")
    db.migrate(url)
    yield url
    if not os.environ.get("TEST_DATABASE_URL"):
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)')


@pytest.fixture
def conn(db_url):
    """A clean connection: every table emptied before the test."""
    from inspraak import db

    with db.session(db_url) as c:
        c.execute(
            "TRUNCATE chunks, document_pages, documents, feedback, publications, initiatives, "
            "meeting_orgs, meetings, organisations, ingest_runs, llm_calls, publication_summaries, "
            "feedback_analyses, publication_meetings, runs, run_traces, review_items, org_memory, "
            "drafts, match_reranks RESTART IDENTITY CASCADE"
        )
        c.commit()
        yield c


@pytest.fixture
def agent_env(db_url, monkeypatch):
    """Point the service layer at the test database and run inline."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("RUN_INLINE", "1")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    return db_url


@pytest.fixture
def snapshot(conn, fixtures):
    """The small but complete snapshot of `inspraak.seed` (3 registrants, Greenpeace among
    them, publication 26173 open and in scope, 5 feedback rows, a two-page document, 4
    meetings, hash vectors everywhere). Returns the connection."""
    from inspraak.seed import seed_snapshot

    seed_snapshot(conn, fixtures)
    return conn
