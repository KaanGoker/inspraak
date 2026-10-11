"""Matching baseline measured against what organisations really did.

Ground truth: a registrant that filed feedback on an in-scope publication was, by its
own action, interested in it. For each such organisation the in-scope publications are
ranked by cosine similarity between the stored profile vector and the stored publication
vector, and the ranks of the publications it answered are scored. No LLM, no network.

This understates precision (an organisation can care about a file and not answer) and
it is a recall measure only. It exists so a matcher change can be compared to a number.
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import psycopg

from inspraak.config import PORTAL_NOW_SQL
from inspraak.store import to_array


def _ground_truth(conn: psycopg.Connection) -> dict[str, Any] | None:
    pubs = conn.execute(
        "SELECT id, initiative_id, hub_score, embedding FROM publications "
        "WHERE in_scope AND embedding IS NOT NULL ORDER BY id"
    ).fetchall()
    pairs = conn.execute(
        "SELECT DISTINCT f.tr_number, f.publication_id FROM feedback f "
        "JOIN publications p ON p.id = f.publication_id AND p.in_scope "
        "JOIN organisations o ON o.tr_id = f.tr_number "
        "WHERE p.embedding IS NOT NULL AND o.profile_embedding IS NOT NULL"
    ).fetchall()
    if not pubs or not pairs:
        return None
    column = {p["id"]: i for i, p in enumerate(pubs)}
    truth: dict[str, set[int]] = {}
    for pair in pairs:
        truth.setdefault(pair["tr_number"], set()).add(column[pair["publication_id"]])
    orgs = conn.execute(
        "SELECT tr_id, profile_embedding FROM organisations WHERE tr_id = ANY(%s) ORDER BY tr_id",
        (sorted(truth),),
    ).fetchall()
    pub_matrix = np.stack([to_array(p["embedding"]) for p in pubs])
    org_matrix = np.stack([to_array(o["profile_embedding"]) for o in orgs])
    initiatives = sorted({p["initiative_id"] for p in pubs})
    init_col = {i: n for n, i in enumerate(initiatives)}
    pub_init = np.array([init_col[p["initiative_id"]] for p in pubs])
    hub = np.array([p["hub_score"] or 0.0 for p in pubs], dtype=np.float32)
    return {
        "pubs": pubs,
        "pairs": pairs,
        "truth": truth,
        "orgs": orgs,
        "raw": org_matrix @ pub_matrix.T,
        "hub": hub,
        "has_hub": any(p["hub_score"] is not None for p in pubs),
        "pub_init": pub_init,
        "n_initiatives": len(initiatives),
    }


def _by_initiative(scores: np.ndarray, pub_init: np.ndarray, n: int) -> np.ndarray:
    """Per organisation and initiative: the best score of its publications."""
    out = np.full((scores.shape[0], n), -np.inf, dtype=np.float32)
    for col, init in enumerate(pub_init):
        out[:, init] = np.maximum(out[:, init], scores[:, col])
    return out


def _initiative_truth(gt: dict[str, Any]) -> dict[str, set[int]]:
    return {t: {int(gt["pub_init"][c]) for c in cols} for t, cols in gt["truth"].items()}


def match_baseline(conn: psycopg.Connection, *, seed: int = 0) -> dict[str, Any]:
    """Recall of the embedding matcher against real past submissions, without and with
    the hub penalty, per publication and per initiative (one card per initiative)."""
    from inspraak.agents.matcher import HUB_WEIGHT

    gt = _ground_truth(conn)
    if gt is None:
        pubs = conn.execute(
            "SELECT count(*) AS n FROM publications WHERE in_scope AND embedding IS NOT NULL"
        ).fetchone()["n"]
        return {"organisations": 0, "publications": pubs, "pairs": 0}
    raw, orgs, truth = gt["raw"], gt["orgs"], gt["truth"]
    adjusted = raw - HUB_WEIGHT * gt["hub"][None, :]
    random_scores = np.random.default_rng(seed).random(raw.shape)
    result: dict[str, Any] = {
        "organisations": len(orgs),
        "publications": len(gt["pubs"]),
        "initiatives": gt["n_initiatives"],
        "pairs": len(gt["pairs"]),
        "embedding": _rank_metrics(raw, orgs, truth),
        "random": _rank_metrics(random_scores, orgs, truth),
    }
    if gt["has_hub"]:
        result["hub_penalty"] = {"weight": HUB_WEIGHT, **_rank_metrics(adjusted, orgs, truth)}
    itruth = _initiative_truth(gt)
    n = gt["n_initiatives"]
    result["per_initiative"] = {
        "embedding": _rank_metrics(_by_initiative(raw, gt["pub_init"], n), orgs, itruth),
        "random": _rank_metrics(_by_initiative(random_scores, gt["pub_init"], n), orgs, itruth),
    }
    if gt["has_hub"]:
        result["per_initiative"]["hub_penalty"] = _rank_metrics(
            _by_initiative(adjusted, gt["pub_init"], n), orgs, itruth
        )
    return result


def rerank_eval(
    provider: Any,
    *,
    sample: int = 100,
    seed: int = 42,
    workers: int = 8,
    database_url: str | None = None,
    log: Any = None,
) -> dict[str, Any]:
    """Recall per initiative on a random sample of the ground-truth organisations: the
    embedding order with the hub penalty, against the same list after the model rerank of
    its `RERANK_POOL` strongest initiatives. Judgments go into the shared cache."""
    import random
    from concurrent.futures import ThreadPoolExecutor

    from inspraak import db, store
    from inspraak.agents.matcher import HUB_WEIGHT, RERANK_POOL, UNJUDGED
    from inspraak.agents.rerank import Reranker

    with db.session(database_url) as conn:
        gt = _ground_truth(conn)
    if gt is None:
        return {"organisations": 0}
    n = gt["n_initiatives"]
    adjusted = _by_initiative(gt["raw"] - HUB_WEIGHT * gt["hub"][None, :], gt["pub_init"], n)
    # an initiative's best publication represents it in the prompt
    best_pub: list[dict[int, int]] = []
    pub_scores = gt["raw"] - HUB_WEIGHT * gt["hub"][None, :]
    itruth = _initiative_truth(gt)
    rows = list(range(len(gt["orgs"])))
    random.Random(seed).shuffle(rows)
    rows = sorted(rows[:sample])
    for i in rows:
        reps: dict[int, int] = {}
        for col in np.argsort(-pub_scores[i], kind="stable"):
            reps.setdefault(int(gt["pub_init"][col]), int(gt["pubs"][col]["id"]))
        best_pub.append(reps)

    def one(k: int) -> tuple[int, dict[int, dict[str, Any]], int, str | None]:
        i = rows[k]
        tr_id = gt["orgs"][i]["tr_id"]
        pool = [int(c) for c in np.argsort(-adjusted[i], kind="stable")[:RERANK_POOL]]
        with db.session(database_url) as conn:
            org = store.get_organisation(conn, tr_id)
            reranker = Reranker(conn, tr_id, dict(org), provider, log=log)
            judged = reranker.scores([best_pub[k][init] for init in pool])
        return k, judged, reranker.calls, reranker.error

    reranked = np.array(adjusted)
    calls = errors = 0
    with ThreadPoolExecutor(max_workers=workers) as pool_exec:
        for k, judged, c, err in pool_exec.map(one, range(len(rows))):
            calls += c
            errors += bool(err)
            i = rows[k]
            order = [int(x) for x in np.argsort(-adjusted[i], kind="stable")]
            head = order[:RERANK_POOL]
            rel = {
                init: judged.get(best_pub[k][init], {}).get("relevance", UNJUDGED) for init in head
            }
            head.sort(key=lambda init: (-rel[init], -adjusted[i, init]))
            new_order = head + order[RERANK_POOL:]
            for pos, init in enumerate(new_order):
                reranked[i, init] = -pos
    orgs = [gt["orgs"][i] for i in rows]
    return {
        "organisations": len(rows),
        "seed": seed,
        "pool": RERANK_POOL,
        "calls": calls,
        "errors": errors,
        "hub_penalty": _rank_metrics(adjusted[rows], orgs, itruth),
        "reranked": _rank_metrics(reranked[rows], orgs, itruth),
        "embedding": _rank_metrics(
            _by_initiative(gt["raw"], gt["pub_init"], n)[rows], orgs, itruth
        ),
    }


_READER_CACHE = re.compile(r"(\d+)/(\d+) summaries from cache")


def _pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _timing(values: list[int]) -> dict[str, float] | None:
    if not values:
        return None
    arr = np.array(values, dtype=float)
    return {
        "n": len(values),
        "mean_ms": round(float(arr.mean())),
        "median_ms": round(float(np.median(arr))),
        "p95_ms": round(float(np.percentile(arr, 95))),
        "max_ms": int(arr.max()),
    }


def report(conn: psycopg.Connection, *, provider: str | None = None) -> dict[str, Any]:
    """The measured numbers behind docs/EVAL.md, read from the tables. `provider` narrows
    the call log and the caches to one provider (the demo uses gemini; the fake one is
    for tests)."""
    where = "WHERE provider = %s" if provider else ""
    params: tuple = (provider,) if provider else ()

    quotes = conn.execute(
        "SELECT count(*) AS rows_total, count(*) FILTER (WHERE source <> 'none') AS analysed, "
        "count(*) FILTER (WHERE source = 'none') AS no_text, "
        "count(*) FILTER (WHERE needs_review AND source <> 'none') AS held_back, "
        "COALESCE(sum(quotes_total), 0) AS quotes_total, "
        "COALESCE(sum(quotes_dropped), 0) AS quotes_dropped, "
        "count(DISTINCT publication_id) AS publications "
        f"FROM feedback_analyses {where}",
        params,
    ).fetchone()
    stances = conn.execute(
        "SELECT stance, count(*) AS n FROM feedback_analyses WHERE source <> 'none' "
        + ("AND provider = %s " if provider else "")
        + "GROUP BY stance ORDER BY n DESC",
        params,
    ).fetchall()
    by_source = conn.execute(
        "SELECT source, count(*) AS n FROM feedback_analyses "
        + ("WHERE provider = %s " if provider else "")
        + "GROUP BY source ORDER BY n DESC",
        params,
    ).fetchall()
    summaries = conn.execute(
        f"SELECT count(*) AS n FROM publication_summaries {where}", params
    ).fetchone()["n"]
    scope = conn.execute(
        "SELECT count(*) FILTER (WHERE feedback_status = 'OPEN' AND end_date >= "
        + PORTAL_NOW_SQL
        + ") AS open, "
        "count(*) AS total FROM publications WHERE in_scope"
    ).fetchone()
    open_summarised = conn.execute(
        "SELECT count(*) AS n FROM publications p JOIN publication_summaries s "
        "ON s.publication_id = p.id WHERE p.in_scope AND p.feedback_status = 'OPEN' "
        "AND p.end_date >= " + PORTAL_NOW_SQL + ""
    ).fetchone()["n"]

    calls = conn.execute(
        "SELECT purpose, count(*) FILTER (WHERE ok) AS ok, count(*) FILTER (WHERE NOT ok) AS "
        "failed, count(*) FILTER (WHERE ok AND attempts > 1) AS retried, "
        "COALESCE(sum(prompt_tokens) FILTER (WHERE ok), 0) AS prompt_tokens, "
        "COALESCE(sum(output_tokens) FILTER (WHERE ok), 0) AS output_tokens, "
        "array_agg(duration_ms) FILTER (WHERE ok) AS durations "
        f"FROM llm_calls {where} GROUP BY purpose ORDER BY purpose",
        params,
    ).fetchall()
    calls_out = []
    for c in calls:
        row = dict(c)
        row["timing"] = _timing([d for d in (row.pop("durations") or []) if d is not None])
        calls_out.append(row)
    errors = conn.execute(
        f"SELECT purpose, error, created_at FROM llm_calls {where}"
        + (" AND" if provider else " WHERE")
        + " NOT ok ORDER BY created_at",
        params,
    ).fetchall()

    runs = conn.execute(
        "SELECT r.id, r.tr_id, o.name, o.category, r.status, r.stage, r.result, r.started_at, "
        "r.updated_at, r.finished_at, "
        "(SELECT count(*) FROM llm_calls c WHERE c.run_id = r.id AND c.ok) AS llm_calls, "
        "(SELECT COALESCE(sum(duration_ms), 0) FROM run_traces t WHERE t.run_id = r.id "
        " AND t.tool <> 'interrupt') AS node_ms, "
        "(SELECT count(*) FROM run_traces t WHERE t.run_id = r.id) AS nodes, "
        "(SELECT count(*) FROM drafts d WHERE d.run_id = r.id AND d.version = 1) AS drafts "
        "FROM runs r JOIN organisations o ON o.tr_id = r.tr_id ORDER BY r.created_at"
    ).fetchall()
    per_run = []
    reader_hits = reader_total = 0
    bands: dict[str, int] = {}
    scores: list[float] = []
    for r in runs:
        matches = (r["result"] or {}).get("matches") or []
        for m in matches:
            bands[m["band"]] = bands.get(m["band"], 0) + 1
            scores.append(float(m["score"]))
        trace = conn.execute(
            "SELECT output_summary FROM run_traces WHERE run_id = %s AND agent = 'reader' "
            "ORDER BY seq LIMIT 1",
            (r["id"],),
        ).fetchone()
        if trace and trace["output_summary"]:
            found = _READER_CACHE.search(trace["output_summary"])
            if found:
                reader_hits += int(found.group(1))
                reader_total += int(found.group(2))
        per_run.append(
            {
                "run_id": str(r["id"]),
                "tr_id": r["tr_id"],
                "organisation": r["name"],
                "category": r["category"],
                "status": r["status"],
                "stage": r["stage"],
                "matches": len(matches),
                "llm_calls": r["llm_calls"],
                "drafts": r["drafts"],
                "nodes": r["nodes"],
                "node_seconds": round(r["node_ms"] / 1000, 1),
            }
        )
    call_counts = [p["llm_calls"] for p in per_run]
    with_draft = [p["llm_calls"] for p in per_run if p["drafts"]]
    without_draft = [p["llm_calls"] for p in per_run if not p["drafts"]]

    critic = conn.execute(
        "SELECT count(*) AS drafts, COALESCE(sum((critic->>'sentences')::int), 0) AS sentences, "
        "COALESCE(sum((critic->>'supported')::int), 0) AS supported, "
        "COALESCE(sum(jsonb_array_length(critic->'removed')), 0) AS removed, "
        "COALESCE(avg(length(text)), 0)::int AS mean_chars, COALESCE(max(length(text)), 0) AS "
        "max_chars FROM drafts WHERE version = 1 AND critic IS NOT NULL"
    ).fetchone()
    draft_status = conn.execute(
        "SELECT status, count(*) AS n FROM drafts WHERE version = 1 GROUP BY status ORDER BY n DESC"
    ).fetchall()
    inbox = conn.execute(
        "SELECT kind, status, count(*) AS n FROM review_items GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()

    return {
        "scope": {
            "publications": scope["total"],
            "open": scope["open"],
            "open_summarised": open_summarised,
            "summaries": summaries,
        },
        "analyst": {
            **dict(quotes),
            "quotes_verified": quotes["quotes_total"] - quotes["quotes_dropped"],
            "quotes_verified_pct": _pct(
                quotes["quotes_total"] - quotes["quotes_dropped"], quotes["quotes_total"]
            ),
            "stances": [dict(s) for s in stances],
            "by_source": [dict(s) for s in by_source],
        },
        "calls": calls_out,
        "call_errors": [dict(e) for e in errors],
        "runs": {
            "total": len(per_run),
            "by_status": {
                s: sum(1 for p in per_run if p["status"] == s)
                for s in sorted({p["status"] for p in per_run})
            },
            "organisations": len({p["tr_id"] for p in per_run}),
            "calls_per_run_mean": round(float(np.mean(call_counts)), 2) if call_counts else None,
            "calls_per_run_with_draft": _timing_free(with_draft),
            "calls_per_run_without_draft": _timing_free(without_draft),
            "reader_cache": {
                "hits": reader_hits,
                "reads": reader_total,
                "hit_pct": _pct(reader_hits, reader_total),
            },
            "matches": {
                "total": len(scores),
                "bands": bands,
                "score_min": round(min(scores), 3) if scores else None,
                "score_median": round(float(np.median(scores)), 3) if scores else None,
                "score_max": round(max(scores), 3) if scores else None,
            },
            "node_seconds_median": round(float(np.median([p["node_seconds"] for p in per_run])), 1)
            if per_run
            else None,
            "per_run": per_run,
        },
        "critic": {
            **dict(critic),
            "supported_pct": _pct(critic["supported"], critic["sentences"]),
            "draft_status": [dict(d) for d in draft_status],
        },
        "inbox": [dict(i) for i in inbox],
    }


def _timing_free(values: list[int]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "n": len(values),
        "min": int(min(values)),
        "mean": round(float(np.mean(values)), 2),
        "max": int(max(values)),
    }


def _rank_metrics(
    scores: np.ndarray, orgs: list[dict[str, Any]], truth: dict[str, set[int]]
) -> dict[str, float]:
    ranks: list[int] = []
    for i, org in enumerate(orgs):
        order = np.argsort(-scores[i], kind="stable")
        position = {int(col): rank for rank, col in enumerate(order)}
        ranks.extend(position[col] for col in truth[org["tr_id"]])
    arr = np.array(ranks)
    return {
        "recall_at_5": round(float((arr < 5).mean()), 3),
        "recall_at_10": round(float((arr < 10).mean()), 3),
        "mrr": round(float((1 / (arr + 1)).mean()), 3),
        "median_rank": float(np.median(arr) + 1),
    }


# --------------------------------------------------------------------------- headline numbers

EVAL_FILE = "docs/eval.json"


def headline(conn: psycopg.Connection) -> dict[str, Any]:
    """The numbers the about page, the README and docs/EVAL.md quote, with one definition
    each. Written to docs/eval.json by `inspraak eval-report --write`; the about page reads
    that file, so the three never disagree.

    - analyses: every provider except the test stand-in; "held back" are analysed rows with
      a low confidence or an unclear stance (rows without text are counted separately).
    - critic: the current drafts, version 1 of every draft not rejected by a reviewer.
    - runs: runs not superseded by a rebuilt match list.
    """
    from inspraak import store

    stats = store.snapshot_stats(conn)
    snapshot = conn.execute(
        "SELECT max(finished_at) AS t FROM ingest_runs WHERE ok AND source LIKE 'hys%%'"
    ).fetchone()["t"]
    analyses = conn.execute(
        "SELECT count(*) AS rows_total, count(*) FILTER (WHERE source <> 'none') AS analysed, "
        "count(*) FILTER (WHERE source = 'none') AS no_text, "
        "count(*) FILTER (WHERE needs_review AND source <> 'none') AS held_back, "
        "count(*) FILTER (WHERE source = 'text_en') AS from_translation, "
        "COALESCE(sum(quotes_total), 0) AS quotes_total, "
        "COALESCE(sum(quotes_dropped), 0) AS quotes_dropped "
        "FROM feedback_analyses WHERE provider <> 'fake'"
    ).fetchone()
    by_provider = conn.execute(
        "SELECT provider, model, count(*) AS analysed, "
        "COALESCE(sum(quotes_total), 0) AS quotes, COALESCE(sum(quotes_dropped), 0) AS dropped "
        "FROM feedback_analyses WHERE source <> 'none' AND provider NOT IN ('fake', 'none') "
        "GROUP BY 1, 2 ORDER BY 3 DESC"
    ).fetchall()
    critic = conn.execute(
        "SELECT count(*) AS drafts, COALESCE(sum((critic->>'sentences')::int), 0) AS sentences, "
        "COALESCE(sum((critic->>'supported')::int), 0) AS supported FROM drafts "
        "WHERE version = 1 AND critic IS NOT NULL AND status <> 'rejected'"
    ).fetchone()
    runs = conn.execute(
        "SELECT count(*) FILTER (WHERE stage IS DISTINCT FROM 'superseded') AS current, "
        "count(*) FILTER (WHERE stage = 'superseded') AS superseded, "
        "count(DISTINCT tr_id) AS organisations FROM runs"
    ).fetchone()
    calls = conn.execute(
        "SELECT provider, model, purpose, count(*) FILTER (WHERE ok) AS ok, "
        "count(*) FILTER (WHERE NOT ok) AS failed FROM llm_calls WHERE provider <> 'fake' "
        "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
    ).fetchall()
    reader = report(conn)["runs"]["reader_cache"]
    verified = analyses["quotes_total"] - analyses["quotes_dropped"]
    return {
        "snapshot": snapshot.isoformat() if snapshot else None,
        "scope": {
            "registrants": stats["organisations"],
            "publications": stats["publications_in_scope"],
            "open": stats["publications_open_in_scope"],
            "organisation_submissions": stats["feedback_organisations"],
        },
        "analyses": {
            **dict(analyses),
            "quotes_verified": verified,
            "quotes_verified_pct": _pct(verified, analyses["quotes_total"]),
            "by_provider": [
                {
                    **dict(p),
                    "verified_pct": _pct(p["quotes"] - p["dropped"], p["quotes"]),
                }
                for p in by_provider
            ],
        },
        "critic": {**dict(critic), "supported_pct": _pct(critic["supported"], critic["sentences"])},
        "runs": dict(runs),
        "reader_cache": reader,
        "calls": [dict(c) for c in calls],
        "calls_total": sum(c["ok"] for c in calls),
    }
