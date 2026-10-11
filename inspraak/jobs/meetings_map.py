"""Meetings mapper, publication layer, no LLM: meetings whose raw subject is close to a
publication's match text (embedding similarity), cached in `publication_meetings`.
The register-ID join (who met, with which category) is SQL on top of this table.
Everything here is approximate by construction and is labelled so on the page."""

from __future__ import annotations

from typing import Any

import psycopg

from inspraak import store

DEFAULT_MIN_SCORE = 0.45
DEFAULT_LIMIT = 60


def map_publication(
    conn: psycopg.Connection,
    publication_id: int,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    limit: int = DEFAULT_LIMIT,
    force: bool = False,
) -> tuple[list[dict], bool]:
    """Cached rows or a fresh similarity pass. Returns (rows, cache_hit)."""
    if not force:
        cached = publication_meetings(conn, publication_id)
        if cached:
            return cached, True
    row = conn.execute(
        "SELECT embedding FROM publications WHERE id = %s", (publication_id,)
    ).fetchone()
    if row is None or row["embedding"] is None:
        return [], False
    hits = store.similar_meetings(
        conn, store.to_array(row["embedding"]), limit=limit, min_score=min_score
    )
    with conn.cursor() as cur:
        cur.execute("DELETE FROM publication_meetings WHERE publication_id = %s", (publication_id,))
        cur.executemany(
            "INSERT INTO publication_meetings (publication_id, meeting_id, score) VALUES (%s, %s, "
            "%s)",
            [(publication_id, h["id"], float(h["score"])) for h in hits],
        )
    conn.commit()
    return publication_meetings(conn, publication_id), False


def publication_meetings(conn: psycopg.Connection, publication_id: int) -> list[dict]:
    return conn.execute(
        "SELECT m.id, m.cabinet, m.ec_representative, m.meeting_date, m.subject, pm.score, "
        "json_agg(json_build_object('tr_id', mo.tr_id, 'name', COALESCE(o.name, mo.org_name), "
        "'category', o.category, 'country', o.head_country) ORDER BY mo.position) AS orgs "
        "FROM publication_meetings pm JOIN meetings m ON m.id = pm.meeting_id "
        "JOIN meeting_orgs mo ON mo.meeting_id = m.id "
        "LEFT JOIN organisations o ON o.tr_id = mo.tr_id "
        "WHERE pm.publication_id = %s GROUP BY m.id, pm.score ORDER BY pm.score DESC, "
        "m.meeting_date DESC",
        (publication_id,),
    ).fetchall()


def lobby_picture(conn: psycopg.Connection, publication_id: int, tr_id: str) -> dict[str, Any]:
    """Counts by register category over the mapped meetings, plus the organisation's own
    meetings, for the Lobby mapper node."""
    rows, _ = map_publication(conn, publication_id)
    ids = [r["id"] for r in rows]
    return {
        "publication_id": publication_id,
        "meetings": rows,
        "by_category": store.meeting_counts_by_category(conn, ids),
        "own_meetings": store.meetings_for_organisation(conn, tr_id),
        "note": "subject match, approximate",
    }
