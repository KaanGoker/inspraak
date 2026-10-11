"""Trace rows: one per node execution (agent, tool, summaries, tokens, cache hit,
duration), shown on the run trace page."""

from __future__ import annotations

import psycopg


def write_trace(
    conn: psycopg.Connection,
    run_id: str,
    agent: str,
    *,
    tool: str | None = None,
    input_summary: str | None = None,
    output_summary: str | None = None,
    prompt_tokens: int = 0,
    output_tokens: int = 0,
    llm_calls: int = 0,
    cache_hit: bool | None = None,
    duration_ms: int | None = None,
) -> int:
    row = conn.execute(
        "INSERT INTO run_traces (run_id, seq, agent, tool, input_summary, output_summary, "
        "prompt_tokens, output_tokens, llm_calls, cache_hit, duration_ms) "
        "SELECT %s, COALESCE(max(seq), 0) + 1, %s, %s, %s, %s, %s, %s, %s, %s, %s "
        "FROM run_traces WHERE run_id = %s RETURNING seq",
        (
            run_id,
            agent,
            tool,
            (input_summary or "")[:1000],
            (output_summary or "")[:2000],
            prompt_tokens,
            output_tokens,
            llm_calls,
            cache_hit,
            duration_ms,
            run_id,
        ),
    ).fetchone()
    conn.commit()
    return row["seq"]


def traces_for_run(conn: psycopg.Connection, run_id: str) -> list[dict]:
    return conn.execute(
        "SELECT seq, agent, tool, input_summary, output_summary, prompt_tokens, output_tokens, "
        "llm_calls, cache_hit, duration_ms, created_at FROM run_traces WHERE run_id = %s ORDER BY "
        "seq",
        (run_id,),
    ).fetchall()
