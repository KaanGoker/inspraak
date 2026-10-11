"""Feedback analyst, publication layer: one structured call per organisation submission,
cached forever by feedback id. Stance (supports, opposes, wants_changes, unclear),
confidence, up to 3 key asks, each with a verbatim quote that must be found in the source
text by string match or it is dropped. Low confidence goes to the review queue, not to
the page. Citizen rows are never read (the query excludes them; the store has no text).

Source text order: the English translation, the original text, else the first pages of
the first parsed attachment. A submission with no text anywhere gets no call: it is
recorded as `unclear` with confidence 0. When it carries attachments none of which could
be read, that is an `attachment` review item (unparseable attachment); a questionnaire
answer with neither text nor attachment is only counted, there is nothing to fix.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from inspraak.config import get_settings
from inspraak.jobs.quotes import verify_quotes
from inspraak.llm.base import Provider
from inspraak.llm.schemas import FeedbackAnalysis

ATTACHMENT_CHARS = 7000
SOURCE_CHARS = 9000
Log = Callable[[str], None]

SYSTEM = (
    "You analyse stakeholder submissions to European Commission consultations. Classify the "
    "stance toward the proposal and extract at most 3 key asks. Every quote must be copied "
    "verbatim from the submission text, character for character. Never invent a quote. Be "
    "neutral. Use plain hyphens, never long dashes."
)


def get_analysis(conn: psycopg.Connection, feedback_id: int) -> dict | None:
    return conn.execute(
        "SELECT * FROM feedback_analyses WHERE feedback_id = %s", (feedback_id,)
    ).fetchone()


def analyses_for_publication(conn: psycopg.Connection, publication_id: int) -> list[dict]:
    return conn.execute(
        "SELECT a.*, f.user_type, f.country, f.organization, f.tr_number, f.date_feedback, "
        "f.attachment_count, f.company_size, f.language, "
        "o.category AS register_category, o.name AS register_name "
        "FROM feedback_analyses a JOIN feedback f ON f.id = a.feedback_id "
        "LEFT JOIN organisations o ON o.tr_id = f.tr_number "
        "WHERE a.publication_id = %s ORDER BY a.confidence DESC, f.date_feedback",
        (publication_id,),
    ).fetchall()


def source_text(conn: psycopg.Connection, feedback: dict) -> tuple[str | None, str]:
    """(text, source) where source is text_en, text or attachment."""
    if feedback.get("feedback_text_en"):
        return feedback["feedback_text_en"][:SOURCE_CHARS], "text_en"
    if feedback.get("feedback_text"):
        return feedback["feedback_text"][:SOURCE_CHARS], "text"
    rows = conn.execute(
        "SELECT p.text FROM documents d JOIN document_pages p ON p.document_id = d.document_id "
        "WHERE d.feedback_id = %s AND d.parse_status = 'ok' "
        "ORDER BY d.document_id, p.page_no LIMIT 12",
        (feedback["id"],),
    ).fetchall()
    if rows:
        text = "\n".join(r["text"] for r in rows)[:ATTACHMENT_CHARS]
        return text, "attachment"
    return None, "none"


def build_prompt(feedback: dict, publication_title: str, text: str) -> str:
    return (
        f"Consultation: {publication_title}\n"
        f"Submitter type: {feedback.get('user_type')}, country: {feedback.get('country')}.\n\n"
        f'Submission text:\n"""\n{text}\n"""\n\n'
        "Return JSON: stance (supports, opposes, wants_changes or unclear), confidence (0 to 1), "
        "key_asks (at most 3, each with ask and a verbatim quote copied exactly from the text)."
    )


def _store(
    conn: psycopg.Connection,
    feedback: dict,
    provider_name: str,
    model: str,
    stance: str,
    confidence: float,
    asks: list[dict],
    quotes_total: int,
    dropped: int,
    source: str,
    needs_review: bool,
) -> dict:
    conn.execute(
        "INSERT INTO feedback_analyses (feedback_id, publication_id, provider, model, stance, "
        "confidence, key_asks, quotes_total, quotes_dropped, source, needs_review) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (feedback_id) DO UPDATE "
        "SET "
        "provider = EXCLUDED.provider, model = EXCLUDED.model, stance = EXCLUDED.stance, "
        "confidence = EXCLUDED.confidence, key_asks = EXCLUDED.key_asks, quotes_total = "
        "EXCLUDED.quotes_total, quotes_dropped = EXCLUDED.quotes_dropped, source = "
        "EXCLUDED.source, "
        "needs_review = EXCLUDED.needs_review, created_at = now()",
        (
            feedback["id"],
            feedback["publication_id"],
            provider_name,
            model,
            stance,
            confidence,
            Jsonb(asks),
            quotes_total,
            dropped,
            source,
            needs_review,
        ),
    )
    conn.commit()
    return get_analysis(conn, feedback["id"])


def _review_item(
    conn: psycopg.Connection, feedback: dict, kind: str, triggers: list[str], payload: dict
) -> None:
    exists = conn.execute(
        "SELECT 1 FROM review_items WHERE feedback_id = %s AND kind = %s AND status = 'open'",
        (feedback["id"], kind),
    ).fetchone()
    if exists:
        return
    conn.execute(
        "INSERT INTO review_items (tr_id, publication_id, feedback_id, kind, blocking, triggers, "
        "payload) VALUES (%s, %s, %s, %s, false, %s, %s)",
        (
            feedback.get("tr_number"),
            feedback["publication_id"],
            feedback["id"],
            kind,
            triggers,
            Jsonb(payload),
        ),
    )
    conn.commit()


PARSED_ATTACHMENT_SQL = (
    "EXISTS (SELECT 1 FROM documents d WHERE d.feedback_id = {fid} AND d.parse_status = 'ok')"
)


def _has_parsed_attachment(conn: psycopg.Connection, feedback_id: int) -> bool:
    """A row stored without text is not final: an attachment that was pending then can
    parse later, and the submission is analysed again."""
    sql = "SELECT " + PARSED_ATTACHMENT_SQL.format(fid="%s") + " AS ok"
    return bool(conn.execute(sql, (feedback_id,)).fetchone()["ok"])


def analyse_feedback(
    conn: psycopg.Connection,
    provider: Provider,
    feedback_id: int,
    *,
    force: bool = False,
    allow_call: bool = True,
) -> tuple[dict | None, bool]:
    """Cached analysis or one call. Returns (row, cache_hit). With `allow_call=False` a
    submission that would need a model call is left alone and (None, False) comes back;
    submissions without readable text are still recorded, they cost nothing."""
    if not force:
        cached = get_analysis(conn, feedback_id)
        if cached is not None and not (
            cached["source"] == "none" and _has_parsed_attachment(conn, feedback_id)
        ):
            return cached, True
    feedback = conn.execute(
        "SELECT f.*, p.title AS publication_title FROM feedback f "
        "JOIN publications p ON p.id = f.publication_id WHERE f.id = %s AND NOT f.is_citizen",
        (feedback_id,),
    ).fetchone()
    if feedback is None:
        raise ValueError(f"feedback {feedback_id} is not an organisation submission")
    text, source = source_text(conn, feedback)
    if text is None:
        row = _store(conn, feedback, "none", "none", "unclear", 0.0, [], 0, 0, "none", True)
        if feedback["attachment_count"]:
            _review_item(
                conn,
                feedback,
                "attachment",
                ["unparseable attachment"],
                {
                    "organization": feedback.get("organization"),
                    "attachments": feedback["attachment_count"],
                },
            )
        return row, False
    if not allow_call:
        return None, False
    result = provider.complete_json(
        build_prompt(feedback, feedback["publication_title"] or "", text),
        FeedbackAnalysis,
        purpose="feedback_analysis",
        system=SYSTEM,
        context={"source_text": text},
        cache_key=f"feedback:{feedback_id}",
    )
    data = result.data
    asks_in = [a.model_dump() for a in data.key_asks][:3]
    kept, dropped = verify_quotes(asks_in, text)
    settings = get_settings()
    low = data.confidence < settings.analyst_min_confidence or data.stance == "unclear"
    row = _store(
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
    return row, False


def analyse_publication(
    conn: psycopg.Connection,
    provider: Provider,
    publication_id: int,
    *,
    limit: int | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Analyse the organisation submissions of a publication that have no row yet.
    `limit` caps the model calls of this pass (the budget); rows without readable text
    cost nothing and are always recorded, rows left over wait for the next pass."""
    rows = conn.execute(
        "SELECT f.id FROM feedback f LEFT JOIN feedback_analyses a ON a.feedback_id = f.id "
        "WHERE f.publication_id = %s AND NOT f.is_citizen AND (a.feedback_id IS NULL OR "
        "(a.source = 'none' AND " + PARSED_ATTACHMENT_SQL.format(fid="f.id") + ")) "
        "ORDER BY (COALESCE(f.feedback_text_en, f.feedback_text) IS NULL), f.date_feedback",
        (publication_id,),
    ).fetchall()
    stats = {
        "publication_id": publication_id,
        "analysed": 0,
        "calls": 0,
        "no_text": 0,
        "dropped": 0,
        "left_for_next_pass": 0,
    }
    for row in rows:
        before = provider.calls
        allow = limit is None or stats["calls"] < limit
        analysis, _ = analyse_feedback(conn, provider, row["id"], allow_call=allow)
        if analysis is None:
            stats["left_for_next_pass"] += 1
            continue
        stats["analysed"] += 1
        stats["calls"] += provider.calls - before
        stats["dropped"] += analysis["quotes_dropped"]
        if analysis["source"] == "none":
            stats["no_text"] += 1
        if log:
            log(
                f"feedback {row['id']}: {analysis['stance']} ({analysis['confidence']:.2f}), "
                f"{len(analysis['key_asks'])} quotes kept, {analysis['quotes_dropped']} dropped"
            )
    return stats
