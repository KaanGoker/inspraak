"""Fill the vector columns. Local model, no API quota, idempotent: only NULL vectors
are computed, and an ingest sets a vector back to NULL when its text changes.

Targets:
- organisations.profile_embedding  from profile_text (name, category, interests, goals)
- publications.embedding           from match_text (initiative title, publication title,
                                   dossier summary, topics), in-scope publications first
- feedback.embedding               organisation rows only, English translation preferred
- chunks.embedding                 proposal and attachment chunks
- meetings.embedding               the raw meeting subject
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from inspraak.embed import Embedder, get_embedder

Log = Callable[[str], None]


def _noop(_: str) -> None:
    pass


@dataclass(frozen=True)
class Target:
    name: str
    select_sql: str  # yields (key, text) for rows that still need a vector
    update_sql: str  # takes (vector, key)
    prepare_sql: str | None = None  # run once before, e.g. to fill a text column


TARGETS: dict[str, Target] = {
    "organisations": Target(
        "organisations",
        "SELECT tr_id AS key, profile_text AS text FROM organisations "
        "WHERE profile_embedding IS NULL AND profile_text IS NOT NULL ORDER BY tr_id",
        "UPDATE organisations SET profile_embedding = %s WHERE tr_id = %s",
    ),
    "publications": Target(
        "publications",
        "SELECT id AS key, match_text AS text FROM publications "
        "WHERE embedding IS NULL AND match_text IS NOT NULL ORDER BY in_scope DESC, id",
        "UPDATE publications SET embedding = %s WHERE id = %s",
        prepare_sql=(
            "UPDATE publications p SET match_text = sub.t, embedding = NULL FROM ("
            "  SELECT p2.id, concat_ws(E'\\n', "
            "    string_agg(DISTINCT i.short_title, E'\\n'), p2.title, "
            "    string_agg(DISTINCT i.dossier_summary, E'\\n'), "
            "    NULLIF(string_agg(DISTINCT NULLIF(array_to_string(i.topics, ', '), ''), ', '), "
            "      '')) AS t "
            "  FROM publications p2 "
            "  JOIN publication_initiatives pi ON pi.publication_id = p2.id "
            "  JOIN initiatives i ON i.id = pi.initiative_id GROUP BY p2.id"
            ") sub WHERE sub.id = p.id AND p.match_text IS DISTINCT FROM sub.t"
        ),
    ),
    "feedback": Target(
        "feedback",
        "SELECT id AS key, COALESCE(feedback_text_en, feedback_text) AS text FROM feedback "
        "WHERE embedding IS NULL AND NOT is_citizen "
        "AND COALESCE(feedback_text_en, feedback_text) IS NOT NULL ORDER BY id",
        "UPDATE feedback SET embedding = %s WHERE id = %s",
    ),
    "chunks": Target(
        "chunks",
        "SELECT id AS key, text FROM chunks WHERE embedding IS NULL ORDER BY id",
        "UPDATE chunks SET embedding = %s WHERE id = %s",
    ),
    "meetings": Target(
        "meetings",
        "SELECT id AS key, subject AS text FROM meetings "
        "WHERE embedding IS NULL AND subject IS NOT NULL ORDER BY id",
        "UPDATE meetings SET embedding = %s WHERE id = %s",
    ),
}


def embed_target(
    conn: psycopg.Connection,
    target: Target,
    embedder: Embedder | None = None,
    *,
    batch_size: int = 256,
    limit: int | None = None,
    log: Log = _noop,
) -> int:
    """Compute and store vectors for one target. Returns the number of rows updated."""
    embedder = embedder or get_embedder()
    if target.prepare_sql:
        conn.execute(target.prepare_sql)
        conn.commit()
    # When texts changed, the ingest already set the vector to NULL; a changed match_text
    # on publications is handled here because prepare_sql rewrites it.
    if target.name == "publications":
        conn.execute("UPDATE publications SET embedding = NULL WHERE match_text IS NULL")
        conn.commit()
    sql = target.select_sql + (f" LIMIT {int(limit)}" if limit else "")
    rows = conn.execute(sql).fetchall()
    done = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        vectors = embedder.embed([r["text"] for r in batch])
        with conn.cursor() as cur:
            cur.executemany(
                target.update_sql, [(vec, r["key"]) for vec, r in zip(vectors, batch, strict=True)]
            )
        conn.commit()
        done += len(batch)
        log(f"{target.name}: {done}/{len(rows)}")
    return done


def embed_all(
    conn: psycopg.Connection,
    names: list[str] | None = None,
    embedder: Embedder | None = None,
    *,
    limit: int | None = None,
    log: Log = _noop,
) -> dict[str, int]:
    embedder = embedder or get_embedder()
    result: dict[str, int] = {}
    for name in names or list(TARGETS):
        result[name] = embed_target(conn, TARGETS[name], embedder, limit=limit, log=log)
    if result.get("organisations") or result.get("publications"):
        from inspraak.agents.matcher import compute_hub_scores

        hubs = compute_hub_scores(conn)
        log(f"hub scores: {hubs['updated']} publications")
    return result


def pending_counts(conn: psycopg.Connection) -> dict[str, Any]:
    return {
        "organisations": conn.execute(
            "SELECT count(*) AS n FROM organisations WHERE profile_embedding IS NULL"
        ).fetchone()["n"],
        "publications": conn.execute(
            "SELECT count(*) AS n FROM publications WHERE embedding IS NULL"
        ).fetchone()["n"],
        "feedback": conn.execute(
            "SELECT count(*) AS n FROM feedback WHERE embedding IS NULL AND NOT is_citizen "
            "AND COALESCE(feedback_text_en, feedback_text) IS NOT NULL"
        ).fetchone()["n"],
        "chunks": conn.execute(
            "SELECT count(*) AS n FROM chunks WHERE embedding IS NULL"
        ).fetchone()["n"],
        "meetings": conn.execute(
            "SELECT count(*) AS n FROM meetings WHERE embedding IS NULL AND subject IS NOT NULL"
        ).fetchone()["n"],
    }
