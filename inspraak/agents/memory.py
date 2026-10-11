"""Long-term memory per organisation, in `org_memory`. Kinds:

- go, no_go, not_our_topic: the reviewer's decision per consultation (not_our_topic
  reweights the matcher on the next run)
- profile_edit: corrections to the register profile, merged by the Profiler
- draft_edit: the reviewer's edited text next to the machine text, read by the Drafter
  as a preference on the next run
- approved, rejected: the final decision on a draft
- position: the stance the organisation took on an initiative (conflict check)
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb


def remember(
    conn: psycopg.Connection,
    tr_id: str,
    kind: str,
    content: dict[str, Any],
    *,
    publication_id: int | None = None,
    run_id: str | None = None,
) -> int:
    row = conn.execute(
        "INSERT INTO org_memory (tr_id, kind, publication_id, run_id, content) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (tr_id, kind, publication_id, run_id, Jsonb(content)),
    ).fetchone()
    conn.commit()
    return row["id"]


def recall(conn: psycopg.Connection, tr_id: str, kinds: list[str] | None = None) -> list[dict]:
    sql = (
        "SELECT id, kind, publication_id, run_id, content, created_at FROM org_memory "
        "WHERE tr_id = %s"
    )
    params: list[Any] = [tr_id]
    if kinds:
        sql += " AND kind = ANY(%s)"
        params.append(kinds)
    sql += " ORDER BY created_at"
    return conn.execute(sql, params).fetchall()


def not_our_topic_ids(conn: psycopg.Connection, tr_id: str) -> list[int]:
    rows = recall(conn, tr_id, ["not_our_topic"])
    return sorted({r["publication_id"] for r in rows if r["publication_id"] is not None})


def profile_edits(conn: psycopg.Connection, tr_id: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for row in recall(conn, tr_id, ["profile_edit"]):
        merged.update({k: v for k, v in row["content"].items() if k != "by"})
    return merged


def draft_preferences(conn: psycopg.Connection, tr_id: str, limit: int = 3) -> list[dict]:
    rows = recall(conn, tr_id, ["draft_edit"])
    return [r["content"] for r in rows[-limit:]]


def stored_positions(conn: psycopg.Connection, tr_id: str) -> dict[int, str]:
    """initiative_id -> stance, latest wins."""
    out: dict[int, str] = {}
    for row in recall(conn, tr_id, ["position"]):
        initiative = row["content"].get("initiative_id")
        if initiative is not None and row["content"].get("stance"):
            out[int(initiative)] = row["content"]["stance"]
    return out
