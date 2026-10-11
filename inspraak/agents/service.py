"""Service API the web stage calls: start_run, get_run, list_review_items, decide,
plus edit_profile and queue_position. Runs execute inline (RUN_INLINE=1, tests, CLI
--inline) or through the Celery `brief` queue; a decision on a blocking item resumes the
graph the same way.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from langgraph.types import Command
from psycopg.types.json import Jsonb

from inspraak import db
from inspraak.agents import memory as org_memory
from inspraak.agents.graph import build_graph, open_checkpointer
from inspraak.agents.state import jsonable
from inspraak.agents.trace import traces_for_run
from inspraak.config import get_settings
from inspraak.llm import QuotaExhausted, get_provider
from inspraak.llm.base import Provider


class RunNotFound(LookupError):
    pass


def _conn():
    return db.session(get_settings().database_url)


# --------------------------------------------------------------------------- start


def start_run(
    tr_id: str,
    *,
    requested_by: str | None = None,
    options: dict[str, Any] | None = None,
    inline: bool | None = None,
    provider: Provider | None = None,
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    with _conn() as conn:
        org = conn.execute("SELECT name FROM organisations WHERE tr_id = %s", (tr_id,)).fetchone()
        if org is None:
            raise RunNotFound(f"organisation {tr_id} is not in the snapshot")
        conn.execute(
            "INSERT INTO runs (id, tr_id, status, stage, requested_by, options) "
            "VALUES (%s, %s, 'queued', 'queued', %s, %s)",
            (run_id, tr_id, requested_by, Jsonb(options or {})),
        )
    dispatched = _dispatch("execute", run_id, None, inline=inline, provider=provider)
    return {**get_run(run_id), "dispatch": dispatched}


def _dispatch(
    action: str,
    run_id: str,
    decision: dict | None,
    *,
    inline: bool | None,
    provider: Provider | None = None,
) -> str:
    settings = get_settings()
    if inline is None:
        inline = settings.run_inline
    if inline or provider is not None:
        if action == "execute":
            execute_run(run_id, provider)
        else:
            resume_run(run_id, decision or {}, provider)
        return "inline"
    from inspraak import queue

    with _conn() as conn:  # visible as queued (with a position) until a worker takes it
        conn.execute(
            "UPDATE runs SET status = 'queued', stage = %s, updated_at = now() WHERE id = %s",
            ("queued" if action == "execute" else "resuming", run_id),
        )
    try:
        if action == "execute":
            queue.enqueue_run(run_id)
        else:
            queue.enqueue_resume(run_id, decision or {})
        return "celery:brief"
    except Exception as exc:  # broker down: do the work here, say so
        if action == "execute":
            execute_run(run_id)
        else:
            resume_run(run_id, decision or {})
        return f"inline (queue unavailable: {type(exc).__name__})"


# --------------------------------------------------------------------------- execute


def _invoke(run_id: str, payload: Any, provider: Provider | None = None) -> dict[str, Any]:
    settings = get_settings()
    provider = provider or get_provider()
    config = {"configurable": {"thread_id": run_id}}
    with _conn() as conn:
        conn.execute(
            "UPDATE runs SET status = 'running', started_at = COALESCE(started_at, now()), "
            "updated_at = now(), error = NULL WHERE id = %s",
            (run_id,),
        )
    try:
        with open_checkpointer(settings.database_url) as saver:
            graph = build_graph(provider, saver, settings)
            result = graph.invoke(payload, config)
    except Exception as exc:
        with _conn() as conn:
            conn.execute(
                "UPDATE runs SET status = 'failed', error = %s, updated_at = now(), "
                "finished_at = now() WHERE id = %s",
                (f"{type(exc).__name__}: {exc}"[:1000], run_id),
            )
        raise
    interrupts = result.get("__interrupt__") or []
    with _conn() as conn:
        if interrupts:
            value = interrupts[0].value if hasattr(interrupts[0], "value") else interrupts[0]
            kind = value.get("kind") if isinstance(value, dict) else "interrupt"
            conn.execute(
                "UPDATE runs SET status = 'waiting', stage = %s, result = %s, updated_at = now() "
                "WHERE id = %s",
                (kind, Jsonb(_result_view(result)), run_id),
            )
        else:
            conn.execute(
                "UPDATE runs SET status = 'done', stage = 'done', result = %s, updated_at = now(), "
                "finished_at = now() WHERE id = %s",
                (Jsonb(_result_view(result)), run_id),
            )
    return get_run(run_id)


def execute_run(run_id: str, provider: Provider | None = None) -> dict[str, Any]:
    run = _run_row(run_id)
    return _invoke(
        run_id, {"run_id": run_id, "tr_id": run["tr_id"], "options": run["options"]}, provider
    )


def resume_run(
    run_id: str, decision: dict[str, Any], provider: Provider | None = None
) -> dict[str, Any]:
    _run_row(run_id)
    return _invoke(run_id, Command(resume=decision), provider)


def _result_view(state: dict[str, Any]) -> dict[str, Any]:
    """The part of the final state worth keeping on the run row."""
    view = {
        "matches": state.get("matches"),
        "go_decision": state.get("go_decision"),
        "drafts": state.get("drafts"),
        "critiques": state.get("critiques"),
        "review_decision": state.get("review_decision"),
        "escalations": state.get("escalations"),
        "finished": state.get("finished", False),
    }
    return jsonable({k: v for k, v in view.items() if v is not None})


# --------------------------------------------------------------------------- read


def _run_row(run_id: str) -> dict[str, Any]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT r.*, o.name AS organisation FROM runs r JOIN organisations o ON o.tr_id = "
            "r.tr_id "
            "WHERE r.id = %s",
            (run_id,),
        ).fetchone()
    if row is None:
        raise RunNotFound(run_id)
    return row


def get_run(run_id: str) -> dict[str, Any]:
    row = _run_row(run_id)
    with _conn() as conn:
        traces = traces_for_run(conn, run_id)
        items = conn.execute(
            "SELECT id, kind, blocking, status, triggers, publication_id, feedback_id, created_at, "
            "decided_at, decided_by FROM review_items WHERE run_id = %s ORDER BY id",
            (run_id,),
        ).fetchall()
        drafts = conn.execute(
            "SELECT id, publication_id, version, status, stance, length(text) AS chars, text, "
            "critic "
            "FROM drafts WHERE run_id = %s ORDER BY publication_id, version",
            (run_id,),
        ).fetchall()
        calls = conn.execute(
            "SELECT count(*) AS calls, COALESCE(sum(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(sum(output_tokens), 0) AS output_tokens FROM llm_calls WHERE run_id = %s "
            "AND ok",
            (run_id,),
        ).fetchone()
    position = None
    if row["status"] == "queued":
        try:
            from inspraak import queue

            position = queue.queue_position(run_id)
        except Exception:
            position = None
    return jsonable(
        {
            "id": str(row["id"]),
            "tr_id": row["tr_id"],
            "organisation": row["organisation"],
            "status": row["status"],
            "stage": row["stage"],
            "queue_position": position,
            "requested_by": row["requested_by"],
            "options": row["options"],
            "result": row["result"],
            "error": row["error"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "llm": calls,
            "traces": traces,
            "review_items": items,
            "drafts": drafts,
        }
    )


def list_runs(tr_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as conn:
        sql = (
            "SELECT r.id, r.tr_id, o.name AS organisation, r.status, r.stage, r.created_at, "
            "r.finished_at FROM runs r JOIN organisations o ON o.tr_id = r.tr_id"
        )
        params: list[Any] = []
        if tr_id:
            sql += " WHERE r.tr_id = %s"
            params.append(tr_id)
        sql += " ORDER BY r.created_at DESC LIMIT %s"
        params.append(limit)
        return jsonable(conn.execute(sql, params).fetchall())


def list_review_items(
    status: str | None = "open",
    *,
    tr_id: str | None = None,
    run_id: str | None = None,
    kind: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT ri.id, ri.run_id, ri.tr_id, o.name AS organisation, ri.publication_id, "
        "p.title AS publication_title, ri.feedback_id, ri.kind, ri.blocking, ri.triggers, "
        "ri.payload, ri.status, ri.decision, ri.decided_by, ri.created_at, ri.decided_at "
        "FROM review_items ri LEFT JOIN organisations o ON o.tr_id = ri.tr_id "
        "LEFT JOIN publications p ON p.id = ri.publication_id WHERE true"
    )
    params: list[Any] = []
    if status:
        sql += " AND ri.status = %s"
        params.append(status)
    if tr_id:
        sql += " AND ri.tr_id = %s"
        params.append(tr_id)
    if run_id:
        sql += " AND ri.run_id = %s"
        params.append(run_id)
    if kind:
        sql += " AND ri.kind = %s"
        params.append(kind)
    sql += " ORDER BY ri.blocking DESC, ri.created_at LIMIT %s"
    params.append(limit)
    with _conn() as conn:
        return jsonable(conn.execute(sql, params).fetchall())


# --------------------------------------------------------------------------- decide


class AlreadyDecided(ValueError):
    """The review item was decided already (or by a concurrent request)."""


def validate_decision(item: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Check a decision for a blocking item against its payload, before anything is stored,
    so a malformed decision never reaches the graph (the checkpointer would replay it on
    every resume). Returns the normalised decision the graph will see."""
    from inspraak.agents.nodes import _normalise_go, _normalise_review

    payload = item.get("payload") or {}
    if item["kind"] == "go_no_go":
        candidates = [int(c["publication_id"]) for c in payload.get("consultations", [])]
        return _normalise_go(decision, candidates)
    if item["kind"] == "draft_review":
        keys = [str(d["publication_id"]) for d in payload.get("drafts", [])]
        out = _normalise_review(decision, keys)
        for key, entry in out["items"].items():
            if entry["action"] == "edit" and not str(entry.get("text") or "").strip():
                raise ValueError(f"edit for {key} needs the edited text")
        return out
    return decision


def decide(
    item_id: int,
    decision: dict[str, Any],
    *,
    decided_by: str | None = None,
    inline: bool | None = None,
    provider: Provider | None = None,
) -> dict[str, Any]:
    """Record the decision. For a blocking item the graph resumes with it.

    The decision is validated first, then claimed with a conditional UPDATE (status still
    open), so a double submit gets `AlreadyDecided` and only one resume is dispatched."""
    if not isinstance(decision, dict):
        raise ValueError("a decision must be a JSON object")
    with _conn() as conn:
        item = conn.execute("SELECT * FROM review_items WHERE id = %s", (item_id,)).fetchone()
        if item is None:
            raise RunNotFound(f"review item {item_id}")
        if item["status"] == "decided":
            raise AlreadyDecided(f"review item {item_id} is already decided")
        decision = {**decision, "decided_by": decision.get("decided_by") or decided_by}
        if item["blocking"]:
            decision = validate_decision(item, decision)
        claimed = conn.execute(
            "UPDATE review_items SET status = 'decided', decision = %s, decided_by = %s, "
            "decided_at = now() WHERE id = %s AND status = 'open' RETURNING id",
            (Jsonb(jsonable(decision)), decision["decided_by"], item_id),
        ).fetchone()
        if claimed is None:
            raise AlreadyDecided(f"review item {item_id} is already decided")
        if item["kind"] == "stance" and item["feedback_id"] and decision.get("stance"):
            conn.execute(
                "UPDATE feedback_analyses SET stance = %s, confidence = 1.0, needs_review = false "
                "WHERE feedback_id = %s",
                (decision["stance"], item["feedback_id"]),
            )
    out: dict[str, Any] = {"item_id": item_id, "kind": item["kind"], "decision": jsonable(decision)}
    if item["blocking"] and item["run_id"]:
        run_id = str(item["run_id"])
        out["dispatch"] = _dispatch("resume", run_id, decision, inline=inline, provider=provider)
        out["run"] = get_run(run_id)
    return out


def retry_run(run_id: str, provider: Provider | None = None) -> dict[str, Any]:
    """Continue a failed run from its last checkpoint (for example after a quota stop or a
    model error in the drafter). The graph replays from the last saved step."""
    run = _run_row(run_id)
    if run["status"] != "failed":
        raise ValueError(f"run {run_id} is {run['status']}, only a failed run can be retried")
    return _invoke(run_id, None, provider)


def start_demo(
    tr_ids: list[str],
    *,
    requested_by: str = "demo",
    options: dict[str, Any] | None = None,
    provider: Provider | None = None,
    log=None,
    replace: bool = False,
) -> list[dict[str, Any]]:
    """Start the brief stage for every listed organisation that has no run yet. Resumable:
    organisations with a run are skipped, so a pass cut short by the daily budget is
    finished by running it again. Each new run stops at the go / no-go interrupt.

    `replace=True` rebuilds the match list of an organisation whose latest run still waits
    at an undecided go / no-go and has no draft: that run is closed as superseded (see
    `supersede_run`) and a new one starts. Runs with a decision or a draft are kept."""
    out: list[dict[str, Any]] = []
    for tr_id in tr_ids:
        with _conn() as conn:
            org = conn.execute(
                "SELECT name FROM organisations WHERE tr_id = %s", (tr_id,)
            ).fetchone()
            existing = conn.execute(
                "SELECT id, status, stage FROM runs WHERE tr_id = %s ORDER BY created_at DESC "
                "LIMIT 1",
                (tr_id,),
            ).fetchone()
            replaced = None
            if replace and existing is not None and existing["status"] != "failed":
                if supersede_run(conn, str(existing["id"]), by=f"{requested_by} (rebuild)"):
                    replaced = str(existing["id"])
                    existing = None
        if org is None:
            out.append({"tr_id": tr_id, "skipped": "not in the snapshot"})
            continue
        if existing is not None and existing["status"] != "failed":
            out.append(
                {
                    "tr_id": tr_id,
                    "organisation": org["name"],
                    "run_id": str(existing["id"]),
                    "status": existing["status"],
                    "stage": existing["stage"],
                    "skipped": "has a run",
                }
            )
            continue
        if log:
            log(f"run for {org['name']} ({tr_id})")
        try:
            run = start_run(
                tr_id, requested_by=requested_by, options=options, inline=True, provider=provider
            )
        except QuotaExhausted as exc:
            out.append({"tr_id": tr_id, "organisation": org["name"], "stopped": str(exc)})
            break
        except Exception as exc:  # the run row holds the error; go on with the next one
            out.append(
                {
                    "tr_id": tr_id,
                    "organisation": org["name"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        matches = (run.get("result") or {}).get("matches") or []
        out.append(
            {
                "tr_id": tr_id,
                "organisation": org["name"],
                "run_id": run["id"],
                "status": run["status"],
                "stage": run["stage"],
                "matches": [
                    (m["publication_id"], m["score"], m.get("relevance"), m["band"])
                    for m in matches
                ],
                "llm_calls": (run.get("llm") or {}).get("calls", 0),
                **({"replaced": replaced} if replaced else {}),
            }
        )
    return out


def supersede_run(conn: psycopg.Connection, run_id: str, *, by: str) -> bool:
    """Close a run that waits at an undecided go / no-go and has no draft, so a newer run
    can replace its match list. Its open review items are marked decided with
    `{"superseded": true}`; the run becomes done with stage `superseded`. Nothing is
    deleted. Returns False (and changes nothing) for any other run."""
    run = conn.execute(
        "SELECT status, stage FROM runs WHERE id = %s FOR UPDATE", (run_id,)
    ).fetchone()
    if run is None or run["status"] != "waiting" or run["stage"] != "go_no_go":
        conn.rollback()
        return False
    decided = conn.execute(
        "SELECT 1 FROM review_items WHERE run_id = %s AND kind = 'go_no_go' AND status = 'decided'",
        (run_id,),
    ).fetchone()
    drafts = conn.execute("SELECT 1 FROM drafts WHERE run_id = %s", (run_id,)).fetchone()
    if decided or drafts:
        conn.rollback()
        return False
    conn.execute(
        "UPDATE review_items SET status = 'decided', decision = %s, decided_by = %s, "
        "decided_at = now() WHERE run_id = %s AND status = 'open'",
        (Jsonb({"superseded": True}), by, run_id),
    )
    conn.execute(
        "UPDATE runs SET status = 'done', stage = 'superseded', finished_at = now(), "
        "updated_at = now() WHERE id = %s",
        (run_id,),
    )
    conn.commit()
    return True


def edit_profile(tr_id: str, edits: dict[str, Any], *, by: str | None = None) -> int:
    """Reviewer corrections to the register profile, merged by the Profiler on the next run."""
    allowed = {
        "goals",
        "interests",
        "levels_of_interest",
        "interest_represented",
        "website",
        "note",
    }
    unknown = set(edits) - allowed
    if unknown:
        raise ValueError(f"cannot edit {sorted(unknown)}; allowed: {sorted(allowed)}")
    with _conn() as conn:
        return org_memory.remember(conn, tr_id, "profile_edit", {**edits, "by": by})


def organisation_memory(tr_id: str) -> list[dict[str, Any]]:
    with _conn() as conn:
        return jsonable(org_memory.recall(conn, tr_id))


def llm_call_counts(conn: psycopg.Connection | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT provider, model, purpose, count(*) FILTER (WHERE ok) AS ok, "
        "count(*) FILTER (WHERE NOT ok) AS failed, COALESCE(sum(prompt_tokens), 0) AS "
        "prompt_tokens, "
        "COALESCE(sum(output_tokens), 0) AS output_tokens, min(created_at) AS first, "
        "max(created_at) AS last "
        "FROM llm_calls GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
    )
    if conn is not None:
        return jsonable(conn.execute(sql).fetchall())
    with _conn() as c:
        return jsonable(c.execute(sql).fetchall())
