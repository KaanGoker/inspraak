"""Parallel, batched feedback analysis for providers without a shared rate limit
(claude_cli). Several submissions go into one call, numbered; each answer is checked
against its own source text exactly like the single-row analyst (quote verification,
low-confidence review items). A submission the model skipped stays pending for the next
pass. Rows without readable text go through `analyse_feedback`, which costs no call."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from inspraak.config import get_settings
from inspraak.db import connect
from inspraak.jobs.analyst import (
    PARSED_ATTACHMENT_SQL,
    SYSTEM,
    _review_item,
    _store,
    analyse_feedback,
    source_text,
)
from inspraak.jobs.quotes import normalise, verify_quotes
from inspraak.llm.base import Provider
from inspraak.llm.bucket import QuotaExhausted
from inspraak.llm.schemas import FeedbackBatchAnalysis

BATCH_SIZE = 10


def pending_feedback_ids(conn) -> list[int]:
    rows = conn.execute(
        "SELECT f.id FROM feedback f JOIN publications p ON p.id = f.publication_id "
        "LEFT JOIN feedback_analyses a ON a.feedback_id = f.id "
        "WHERE p.in_scope AND NOT f.is_citizen AND (a.feedback_id IS NULL OR "
        "(a.source = 'none' AND " + PARSED_ATTACHMENT_SQL.format(fid="f.id") + ")) "
        "ORDER BY f.publication_id, f.id"
    ).fetchall()
    return [r["id"] for r in rows]


def opening_matches(opening: str, text: str) -> bool:
    """The answer is tied to its submission only when the words it echoes open that
    submission's text (first 400 characters, after normalising), at least 4 words."""
    words = normalise(opening).lower().split()
    if len(words) < 4:
        return False
    return " ".join(words[:6]) in normalise(text[:400]).lower()


def build_batch_prompt(items: list[tuple[dict, str]]) -> str:
    parts = []
    for n, (feedback, text) in enumerate(items, 1):
        parts.append(
            f"### Submission {n}\n"
            f"Consultation: {feedback.get('publication_title') or ''}\n"
            f"Submitter type: {feedback.get('user_type')}, country: {feedback.get('country')}.\n"
            f'Text:\n"""\n{text}\n"""'
        )
    return (
        "\n\n".join(parts)
        + "\n\nReturn one entry in items per submission, with submission (its number), "
        "opening_words (the first 8 words of that submission's text, copied exactly), "
        "stance (supports, opposes, wants_changes or unclear), confidence (0 to 1) and "
        "key_asks (at most 3, each with ask and a verbatim quote copied exactly from that "
        "submission's own text)."
    )


def analyse_batch(conn, provider: Provider, feedback_ids: list[int]) -> int:
    """One call for the submissions with text; returns how many rows were stored."""
    items: list[tuple[dict, str, str]] = []
    stored = 0
    for fid in feedback_ids:
        # another worker or job may have done it meanwhile: the cache wins, no call
        row, _ = analyse_feedback(conn, provider, fid, allow_call=False)
        if row is not None:
            continue
        feedback = conn.execute(
            "SELECT f.*, p.title AS publication_title FROM feedback f "
            "JOIN publications p ON p.id = f.publication_id WHERE f.id = %s AND NOT f.is_citizen",
            (fid,),
        ).fetchone()
        if feedback is None:
            continue
        text, source = source_text(conn, feedback)
        items.append((feedback, text, source))
    if not items:
        return stored
    result = provider.complete_json(
        build_batch_prompt([(f, t) for f, t, _ in items]),
        FeedbackBatchAnalysis,
        purpose="feedback_analysis_batch",
        system=SYSTEM,
        max_output_tokens=8192,
        cache_key="feedback:" + ",".join(str(f["id"]) for f, _, _ in items),
    )
    min_conf = get_settings().analyst_min_confidence
    answers = {a.submission: a for a in result.data.items}
    for n, (feedback, text, source) in enumerate(items, 1):
        data = answers.get(n)
        if data is None or not opening_matches(data.opening_words, text):
            continue  # skipped or tied to the wrong text: stays pending for a single call
        asks_in = [a.model_dump() for a in data.key_asks][:3]
        kept, dropped = verify_quotes(asks_in, text)
        low = data.confidence < min_conf or data.stance == "unclear"
        _store(
            conn,
            feedback,
            result.provider,
            result.model,
            data.stance,
            float(data.confidence),
            kept,
            len(asks_in),
            dropped,
            source,
            low,
        )
        stored += 1
        if low:
            _review_item(
                conn,
                feedback,
                "stance",
                ["low-confidence stance"],
                {
                    "organization": feedback.get("organization"),
                    "stance": data.stance,
                    "confidence": data.confidence,
                },
            )
    return stored


def analyse_parallel(
    provider: Provider,
    *,
    workers: int = 8,
    batch_size: int = BATCH_SIZE,
    single: bool = False,
    reverse: bool = False,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """`single=True` analyses one submission per call through the plain analyst: the
    pass for the rows a batch skipped or could not tie to their text."""
    with connect() as conn:
        ids = pending_feedback_ids(conn)
    if reverse:  # a second job works from the other end, so the two rarely meet
        ids.reverse()
    size = 1 if single else batch_size
    batches = [ids[i : i + size] for i in range(0, len(ids), size)]
    stats = {"pending": len(ids), "batches": len(batches), "stored": 0, "errors": 0}
    stats["stopped"] = None
    local = threading.local()
    stop = threading.Event()
    lock = threading.Lock()

    def one(batch: list[int]) -> None:
        if stop.is_set():
            return
        if not hasattr(local, "conn"):
            local.conn = connect()
        try:
            if single:
                n = sum(analyse_feedback(local.conn, provider, fid)[0] is not None for fid in batch)
            else:
                n = analyse_batch(local.conn, provider, batch)
            with lock:
                stats["stored"] += n
        except QuotaExhausted as exc:
            stats["stopped"] = str(exc)
            stop.set()
        except Exception as exc:  # one bad batch never stops the others
            local.conn.rollback()
            with lock:
                stats["errors"] += 1
            if log:
                log(f"batch {batch[0]}..: {exc}")

    with ThreadPoolExecutor(workers) as ex:
        futures = [ex.submit(one, b) for b in batches]
        for i, _ in enumerate(as_completed(futures), 1):
            if log and i % 20 == 0:
                done = f"{i}/{len(batches)} batches, {stats['stored']} stored"
                log(f"{done}, {stats['errors']} errors")
    return stats
