"""Publication-layer jobs: Reader summary, Feedback analyst, Meetings mapper. Everything
is cached per publication or per feedback row, forever, and shared by all organisations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg

from inspraak.config import PORTAL_NOW_SQL
from inspraak.jobs.analyst import analyse_publication
from inspraak.jobs.meetings_map import map_publication
from inspraak.jobs.reader import summarise_publication
from inspraak.llm.base import Provider
from inspraak.llm.bucket import QuotaExhausted


def publications_to_precompute(conn: psycopg.Connection, *, open_only: bool = False) -> list[int]:
    """The V1 scope in budget order: open publications first, nearest deadline first, then
    the recently closed ones, most recently closed first. A pass that runs out of daily
    budget has then spent it on the consultations an organisation can still answer."""
    is_open = f"(feedback_status = 'OPEN' AND end_date >= {PORTAL_NOW_SQL})"
    rows = conn.execute(
        "SELECT id FROM publications WHERE in_scope "
        + (f"AND {is_open} " if open_only else "")
        + f"ORDER BY {is_open} DESC, CASE WHEN {is_open} THEN end_date END ASC, "
        "end_date DESC, id"
    ).fetchall()
    return [r["id"] for r in rows]


def precompute_publication(
    conn: psycopg.Connection,
    provider: Provider,
    publication_id: int,
    *,
    feedback_limit: int | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Summary, analyses and meeting map for one publication. Stops cleanly on quota."""
    stats: dict[str, Any] = {"publication_id": publication_id, "calls_before": provider.calls}
    try:
        _, hit = summarise_publication(conn, provider, publication_id)
        stats["summary_cache_hit"] = hit
        stats["analyst"] = analyse_publication(
            conn, provider, publication_id, limit=feedback_limit, log=log
        )
    except QuotaExhausted as exc:
        stats["stopped"] = str(exc)
    rows, hit = map_publication(conn, publication_id)
    stats["meetings_mapped"] = len(rows)
    stats["meetings_cache_hit"] = hit
    stats["calls"] = provider.calls - stats.pop("calls_before")
    return stats
