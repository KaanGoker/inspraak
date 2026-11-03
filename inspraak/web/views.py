"""View models: plain dicts for the templates, built from the snapshot and the agent tables.
Shared by the FastAPI routes and the static export. Read-only: a page view never calls a
model, never writes a cache and never touches a live endpoint.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg

from inspraak import db, store
from inspraak.agents import memory
from inspraak.agents.matcher import match
from inspraak.agents.rerank import Reranker
from inspraak.agents.service import get_run as service_get_run
from inspraak.agents.service import list_review_items, llm_call_counts
from inspraak.agents.state import jsonable
from inspraak.config import Settings, get_settings, portal_now
from inspraak.jobs.analyst import analyses_for_publication
from inspraak.jobs.meetings_map import publication_meetings
from inspraak.jobs.reader import get_summary
from inspraak.privacy import display_submitter
from inspraak.textutil import strip_chunk_markers
from inspraak.web import charts, links

USER_TYPE_LABELS = {
    "EU_CITIZEN": "EU citizen",
    "NON_EU_CITIZEN": "Non-EU citizen",
    "COMPANY": "Company",
    "BUSINESS_ASSOCIATION": "Business association",
    "NGO": "NGO",
    "OTHER": "Other",
    "ACADEMIC_RESEARCH_INSTITTUTION": "Academic or research institution",
    "ACADEMIC_RESEARCH_INSTITUTION": "Academic or research institution",
    "PUBLIC_AUTHORITY": "Public authority",
    "ENVIRONMENTAL_ORGANISATION": "Environmental organisation",
    "TRADE_UNION": "Trade union",
    "CONSUMER_ORGANISATION": "Consumer organisation",
}
STANCE_LABELS = {
    "supports": "supports",
    "opposes": "opposes",
    "wants_changes": "wants changes",
    "unclear": "unclear",
}
DRAFT_STATUS_LABELS = {
    "machine_draft": "machine draft, awaiting human review",
    "approved": "approved by the reviewer",
    "edited": "edited and approved by the reviewer",
    "rejected": "rejected by the reviewer",
}
DEMO_DRAFT_LABEL = "auto-approved for the demo, no human review"


def is_demo_decider(name: str | None) -> bool:
    """Decisions made while building the demo carry "demo" in decided_by."""
    return "demo" in (name or "").lower()


REVIEW_KIND_LABELS = {
    "go_no_go": "go / no-go decision",
    "draft_review": "draft review",
    "relevance": "relevance in the uncertain band",
    "deadline": "deadline under 7 days",
    "critic": "critic found unsupported claims",
    "stance": "low-confidence stance",
    "attachment": "submission without readable text",
    "conflict": "conflict with a stored earlier position",
}
PASSAGES = 3
MEETINGS_SHOWN = 12
COUNTRIES_SHOWN = 10


# The reader sometimes answers "what is asked" with a statement that the document asks
# nothing ("The provided text does not contain a consultation question ..."). That is not
# content, so the line is left off the page.
_ABOUT_THE_INPUT = re.compile(
    r"\b(provided|given|supplied) (text|document|excerpts?|extracts?|passages?)\b"
    r"|^\s*the (text|excerpts?|extracts?|passages?) (does|do|is|are)\b",
    re.IGNORECASE,
)
_NOTHING_ASKED = re.compile(
    r"\b(does|do) not (contain|specify|include|state|list|ask)\b"
    r"|\bno (specific |explicit |formal )?(consultation )?questions?\b",
    re.IGNORECASE,
)


def is_non_answer(value: str | None, *, asked: bool = False) -> bool:
    """True for an empty line or one that talks about the model's input ("The provided
    text ..."). For "what is asked", a line saying nothing is asked counts too."""
    text = (value or "").strip()
    if not text:
        return True
    return bool(_ABOUT_THE_INPUT.search(text) or (asked and _NOTHING_ASKED.search(text)))


def clean_summary(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    """The cached summary row as a page shows it: model non-answers left out."""
    if not summary:
        return None
    row = jsonable(dict(summary))
    body = dict(row.get("summary") or {})
    if is_non_answer(body.get("what_is_asked"), asked=True):
        body["what_is_asked"] = None
    body["key_points"] = [k for k in body.get("key_points") or [] if not is_non_answer(k)]
    row["summary"] = body
    return row


def is_readable_passage(text: str | None) -> bool:
    """False for table residue from a PDF: a passage where many words are bare numbers."""
    words = (text or "").split()
    if len(words) < 12:
        return False
    numbers = sum(1 for w in words if w.strip(".,;:%()*").isdigit())
    return numbers / len(words) < 0.15


def label_type(code: str | None) -> str:
    if not code:
        return "unknown"
    return USER_TYPE_LABELS.get(code, code.replace("_", " ").capitalize())


def label_stance(code: str | None) -> str:
    return STANCE_LABELS.get(code or "", code or "unknown")


def days_left(end: datetime | str | None, now: datetime | None = None) -> int | None:
    if end is None:
        return None
    if isinstance(end, str):
        end = datetime.fromisoformat(end)
    return (end - (now or portal_now())).days


# --------------------------------------------------------------------------- home


def home(conn: psycopg.Connection) -> dict[str, Any]:
    stats = store.snapshot_stats(conn)
    open_pubs: list[dict[str, Any]] = []
    by_title: dict[str, dict[str, Any]] = {}
    for p in store.list_publications(conn, open_only=True):
        row = _pub_row(p)
        # one initiative often has several open publications under one title: one row, counts added
        key = (row.get("short_title") or row.get("title") or "").strip().lower()
        first = by_title.get(key)
        if first is None:
            by_title[key] = row
            open_pubs.append(row)
        else:
            first["org_feedback_count"] = (first.get("org_feedback_count") or 0) + (
                row.get("org_feedback_count") or 0
            )
    return {
        "stats": stats,
        "open_publications": open_pubs,
        "demo_organisations": demo_organisations(conn),
        "snapshot_date": _snapshot_date(conn),
    }


def demo_organisations(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Organisations with at least one run: the precomputed demo set."""
    return jsonable(
        conn.execute(
            "SELECT o.tr_id, o.name, o.acronym, o.category, o.head_country, o.head_city, "
            "count(DISTINCT r.id) AS runs, max(r.created_at) AS last_run, "
            "count(DISTINCT d.id) AS drafts, "
            "count(DISTINCT d.id) FILTER (WHERE d.status = 'machine_draft') AS machine_drafts "
            "FROM organisations o JOIN runs r ON r.tr_id = o.tr_id "
            "LEFT JOIN drafts d ON d.tr_id = o.tr_id "
            "GROUP BY o.tr_id ORDER BY o.name"
        ).fetchall()
    )


def search(conn: psycopg.Connection, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = store.find_organisations(conn, query, limit=limit)
    if not rows:
        return []
    with_runs = {
        r["tr_id"]
        for r in conn.execute(
            "SELECT DISTINCT tr_id FROM runs WHERE tr_id = ANY(%s)", ([r["tr_id"] for r in rows],)
        ).fetchall()
    }
    out = []
    for r in rows:
        row = jsonable(dict(r))
        row.pop("score", None)
        row["has_runs"] = r["tr_id"] in with_runs
        out.append(row)
    return out


def snapshot_date(conn: psycopg.Connection) -> str | None:
    return _snapshot_date(conn)


def _snapshot_date(conn: psycopg.Connection) -> str | None:
    row = conn.execute(
        "SELECT max(finished_at) AS t FROM ingest_runs WHERE ok AND source LIKE 'hys%%'"
    ).fetchone()
    return row["t"].isoformat() if row and row["t"] else None


# --------------------------------------------------------------------------- organisation


def organisation(
    conn: psycopg.Connection, tr_id: str, settings: Settings | None = None
) -> dict[str, Any] | None:
    settings = settings or get_settings()
    org = store.get_organisation(conn, tr_id)
    if org is None:
        return None
    now = portal_now()
    edits = memory.profile_edits(conn, tr_id)
    profile = jsonable(dict(org))
    profile["register_values"] = {k: profile.get(k) for k in edits}
    profile.update({k: v for k, v in edits.items() if k != "note"})
    profile["note"] = edits.get("note")
    profile["edited_fields"] = sorted(k for k in edits if k != "note")
    profile["source"] = links.organisation(tr_id)

    vector = store.organisation_vector(conn, tr_id)
    corrections = memory.not_our_topic_ids(conn, tr_id)
    decisions = _latest_decisions(conn, tr_id)
    drafts = _latest_drafts(conn, tr_id)
    matches: list[dict[str, Any]] = []
    if vector is not None:
        matches = match(
            conn,
            vector,
            top_k=settings.match_top_k,
            min_score=settings.match_min_score,
            uncertain_score=settings.match_uncertain_score,
            corrections=corrections,
            open_only=True,
            now=now,
            # cached judgments only: a page read never calls a model
            reranker=Reranker(conn, tr_id, profile) if settings.match_rerank else None,
            keep=sorted(drafts),
        )

    # matcher order (rerank, else similarity); uncertain fits and cards kept only for their
    # draft go last, so the first card is always the best fit
    matches.sort(key=lambda m: (bool(m.get("kept")) or m["band"] == "uncertain", m["rank"]))
    consultations = [
        _organisation_consultation(conn, m, vector, decisions, drafts, now) for m in matches
    ]

    submissions = []
    for s in store.organisation_submissions(conn, tr_id):
        row = jsonable(dict(s))
        row["excerpt"] = (s["feedback_text_en"] or s["feedback_text"] or "")[:600]
        initiative = conn.execute(
            "SELECT initiative_id FROM publications WHERE id = %s", (s["publication_id"],)
        ).fetchone()
        row["source"] = links.feedback(initiative["initiative_id"], s["id"])
        submissions.append(row)

    own_meetings = jsonable(store.meetings_for_organisation(conn, tr_id))
    runs = jsonable(
        conn.execute(
            "SELECT id, status, stage, requested_by, created_at, finished_at FROM runs "
            "WHERE tr_id = %s ORDER BY created_at DESC LIMIT 20",
            (tr_id,),
        ).fetchall()
    )
    open_items = list_review_items("open", tr_id=tr_id)
    mem = jsonable(memory.recall(conn, tr_id))
    return {
        "profile": profile,
        "has_vector": vector is not None,
        "corrections": corrections,
        "consultations": consultations,
        "submissions": submissions,
        "own_meetings": own_meetings,
        "own_meetings_count": len(own_meetings),
        "runs": runs,
        "open_review_items": len(open_items),
        "memory": mem,
        "drafts_total": sum(1 for d in drafts.values()),
        "scope_publications": conn.execute(
            "SELECT count(*) AS n FROM publications WHERE in_scope"
        ).fetchone()["n"],
    }


def _latest_decisions(conn: psycopg.Connection, tr_id: str) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in memory.recall(conn, tr_id, ["go", "no_go", "not_our_topic"]):
        if row["publication_id"] is not None:
            out[int(row["publication_id"])] = jsonable(dict(row))
    return out


def _latest_drafts(conn: psycopg.Connection, tr_id: str) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        "SELECT DISTINCT ON (d.publication_id) d.id, d.run_id, d.publication_id, d.version, "
        "d.status, d.stance, d.text, length(d.text) AS chars, d.critic, d.created_at, "
        "(SELECT ri.decided_by FROM review_items ri WHERE ri.run_id = d.run_id "
        "AND ri.kind = 'draft_review' AND ri.status = 'decided' ORDER BY ri.id DESC LIMIT 1) "
        "AS reviewed_by FROM drafts d WHERE d.tr_id = %s "
        "ORDER BY d.publication_id, d.version DESC, d.created_at DESC",
        (tr_id,),
    ).fetchall()
    return {int(r["publication_id"]): _draft_view(r, conn) for r in rows}


def cited_sources(conn: psycopg.Connection, sources: list[str]) -> list[dict[str, Any]]:
    """The critic's 'chunk:<id>' citations as document title and page, with a link."""
    ids = [int(s.split(":", 1)[1]) for s in sources if s.startswith("chunk:")]
    by_id: dict[int, dict[str, Any]] = {}
    if ids:
        for r in conn.execute(
            "SELECT c.id, c.document_id, c.page_no, d.title FROM chunks c "
            "JOIN documents d ON d.document_id = c.document_id WHERE c.id = ANY(%s)",
            (ids,),
        ).fetchall():
            by_id[int(r["id"])] = r
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for s in sources:
        if s.startswith("chunk:") and int(s.split(":", 1)[1]) in by_id:
            r = by_id[int(s.split(":", 1)[1])]
            key = (r["document_id"], r["page_no"])
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "label": f"{(r['title'] or r['document_id'])[:70]}, page {r['page_no']}",
                    "source": links.document(r["document_id"], r["page_no"]),
                }
            )
        elif s.startswith("submission:"):
            row = conn.execute(
                "SELECT f.id, f.date_feedback, p.initiative_id, "
                "COALESCE(p.short_title, p.title) AS title FROM feedback f "
                "JOIN publications p ON p.id = f.publication_id WHERE f.id = %s",
                (int(s.split(":", 1)[1]),),
            ).fetchone()
            if row:
                out.append(
                    {
                        "label": f"the organisation's own submission on {row['title'][:60]}",
                        "source": links.feedback(row["initiative_id"], row["id"]),
                    }
                )
        elif s == "profile":
            out.append({"label": "the organisation's register profile", "source": None})
    return out


def _draft_view(row: dict[str, Any], conn: psycopg.Connection | None = None) -> dict[str, Any]:
    d = jsonable(dict(row))
    d["text"] = strip_chunk_markers(d.get("text"))
    d["status_label"] = DRAFT_STATUS_LABELS.get(d["status"], d["status"])
    d["is_demo_review"] = d["status"] != "machine_draft" and is_demo_decider(d.get("reviewed_by"))
    if d["is_demo_review"]:
        d["status_label"] = DEMO_DRAFT_LABEL
    d["stance_label"] = label_stance(d.get("stance"))
    d["is_machine"] = d["status"] == "machine_draft" or d["is_demo_review"]
    critic = dict(d.get("critic") or {})
    critic["removed"] = [
        {
            **r,
            "sentence": strip_chunk_markers(r.get("sentence")),
            "note": strip_chunk_markers(r.get("note")),
        }
        for r in critic.get("removed") or []
    ]
    d["critic"] = critic
    d["critic_summary"] = (
        f"{critic.get('supported', 0)} of {critic.get('sentences', 0)} sentences supported"
        if critic
        else None
    )
    d["cited"] = cited_sources(conn, critic.get("sources") or []) if conn is not None else []
    return d


def _organisation_consultation(
    conn: psycopg.Connection,
    m: dict[str, Any],
    vector,
    decisions: dict[int, dict[str, Any]],
    drafts: dict[int, dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    pid = m["publication_id"]
    pub = store.get_publication(conn, pid)
    landscape = _landscape(conn, pub, limit=3)
    lobby = _lobby(conn, pub, shown=MEETINGS_SHOWN)
    passages = []
    if vector is not None:
        for c in store.similar_chunks(conn, pid, vector, limit=PASSAGES * 3):
            if len(passages) == PASSAGES or not is_readable_passage(c["text"]):
                continue
            passages.append(
                {
                    "chunk_id": c["id"],
                    "document_id": c["document_id"],
                    "document_title": c["document_title"],
                    "language": c["language"],
                    "page_no": c["page_no"],
                    "text": c["text"],
                    "score": round(float(c["score"]), 3),
                    "source": links.document(c["document_id"], c["page_no"]),
                }
            )
    summary = get_summary(conn, pid)
    decision = decisions.get(pid)
    return {
        **m,
        "publication": _pub_row(pub),
        "days_left": days_left(pub["end_date"], now),
        "band_label": "good fit" if m["band"] == "match" else "possible fit",
        "summary": clean_summary(summary),
        "landscape": landscape,
        "lobby": lobby,
        "passages": passages,
        "documents_count": len(store.publication_documents(conn, pid)),
        "draft": drafts.get(pid),
        "decision": decision,
        "decision_label": _decision_label(decision),
        "decision_is_demo": bool(decision)
        and is_demo_decider((decision.get("content") or {}).get("decided_by")),
    }


def _decision_label(decision: dict[str, Any] | None) -> str | None:
    if not decision:
        return None
    return {"go": "go", "no_go": "no-go", "not_our_topic": "not our topic"}.get(
        decision["kind"], decision["kind"]
    )


# --------------------------------------------------------------------------- consultation


def consultation(conn: psycopg.Connection, publication_id: int) -> dict[str, Any] | None:
    pub = store.get_publication(conn, publication_id)
    if pub is None:
        return None
    now = portal_now()
    documents = []
    for d in store.publication_documents(conn, publication_id):
        row = jsonable(dict(d))
        row["source"] = links.document(d["document_id"])
        documents.append(row)
    summary = get_summary(conn, publication_id)
    drafts = jsonable(
        conn.execute(
            "SELECT d.id, d.run_id, d.tr_id, o.name AS organisation, d.version, d.status, "
            "d.stance, length(d.text) AS chars, d.created_at, "
            "(SELECT ri.decided_by FROM review_items ri WHERE ri.run_id = d.run_id "
            "AND ri.kind = 'draft_review' AND ri.status = 'decided' ORDER BY ri.id DESC LIMIT 1) "
            "AS reviewed_by FROM drafts d "
            "JOIN organisations o ON o.tr_id = d.tr_id WHERE d.publication_id = %s "
            "ORDER BY d.created_at DESC",
            (publication_id,),
        ).fetchall()
    )
    for d in drafts:
        d["status_label"] = DRAFT_STATUS_LABELS.get(d["status"], d["status"])
        if d["status"] != "machine_draft" and is_demo_decider(d.get("reviewed_by")):
            d["status_label"] = DEMO_DRAFT_LABEL
    return {
        "publication": _pub_row(pub),
        "days_left": days_left(pub["end_date"], now),
        "initiatives": [
            {**jsonable(dict(i)), "source": links.initiative(i["id"])}
            for i in store.publication_initiatives(conn, publication_id)
        ],
        "dossier_summary": pub.get("dossier_summary"),
        "summary": clean_summary(summary),
        "documents": documents,
        "landscape": _landscape(conn, pub, limit=60),
        "lobby": _lobby(conn, pub, shown=30),
        "drafts": drafts,
    }


def _pub_row(pub: dict[str, Any]) -> dict[str, Any]:
    row = jsonable(dict(pub))
    row.pop("embedding", None)
    row.pop("match_text", None)
    row.pop("attachments", None)
    row["days_left"] = days_left(pub.get("end_date"))
    row["is_open"] = pub.get("feedback_status") == "OPEN" and (
        row["days_left"] is None or row["days_left"] >= 0
    )
    row["source"] = links.initiative(pub["initiative_id"])
    row["feedback_source"] = links.publication_feedback(pub["initiative_id"], pub["id"])
    return row


def _landscape(conn: psycopg.Connection, pub: dict[str, Any], *, limit: int) -> dict[str, Any]:
    pid = pub["id"]
    counts = store.feedback_counts(conn, pid)
    by_type = [
        {**jsonable(dict(r)), "label": label_type(r["user_type"])} for r in counts["by_type"]
    ]
    by_country = [jsonable(dict(r)) for r in counts["by_country"] if r["country"]]
    analyses = analyses_for_publication(conn, pid)
    shown, held_back, no_text = [], 0, 0
    stances: dict[str, int] = {}
    for a in analyses:
        if a["source"] == "none":
            no_text += 1
            continue
        if a["needs_review"]:
            held_back += 1
            continue
        stances[a["stance"]] = stances.get(a["stance"], 0) + 1
        if len(shown) < limit:
            shown.append(
                {
                    "feedback_id": a["feedback_id"],
                    "organization": display_submitter(
                        a["organization"],
                        tr_number=a["tr_number"],
                        company_size=a.get("company_size"),
                        country=a["country"],
                        register_name=a["register_name"],
                    ),
                    "tr_number": a["tr_number"],
                    "user_type": label_type(a["user_type"]),
                    "country": a["country"],
                    "register_category": a["register_category"],
                    "stance": a["stance"],
                    "stance_label": label_stance(a["stance"]),
                    "confidence": round(float(a["confidence"]), 2),
                    "key_asks": a["key_asks"],
                    "source_kind": a["source"],
                    "translated_from": _translated_from(a),
                    "source": links.feedback(pub["initiative_id"], a["feedback_id"]),
                    "date": a["date_feedback"].isoformat() if a["date_feedback"] else None,
                }
            )
    stored = sum(int(r["n"]) for r in counts["by_type"])
    org_rows = sum(int(r["n"]) for r in counts["by_type"] if not r["is_citizen"])
    return {
        "by_type": by_type,
        "by_type_chart": charts.bar_chart(
            [(r["label"], r["n"]) for r in by_type], title="Submissions by stakeholder type"
        ),
        "by_country": by_country[:COUNTRIES_SHOWN],
        "by_country_chart": charts.bar_chart(
            [(r["country"], r["n"]) for r in by_country[:COUNTRIES_SHOWN]],
            title="Submissions by country",
        ),
        "stored_rows": stored,
        "organisation_rows": org_rows,
        "total_feedback": pub.get("total_feedback"),
        "truncated": bool(pub.get("feedback_truncated")),
        "analysed": len(analyses),
        "read": len(analyses) - no_text,
        "stances": {label_stance(k): v for k, v in sorted(stances.items())},
        "submissions": shown,
        "submissions_more": max(0, len(analyses) - no_text - held_back - len(shown)),
        "held_back": held_back,
        "without_text": no_text,
        "not_analysed": max(0, org_rows - len(analyses)),
    }


def _translated_from(analysis: dict[str, Any]) -> str | None:
    """The original language when the quotes come from the Commission's English machine
    translation of the submission (source text_en)."""
    language = (analysis.get("language") or "").upper()
    if analysis.get("source") != "text_en" or language in ("", "EN"):
        return None
    return language


def _lobby(conn: psycopg.Connection, pub: dict[str, Any], *, shown: int) -> dict[str, Any]:
    pid = pub["id"]
    rows = publication_meetings(conn, pid)
    cached = bool(rows)
    if not rows:
        vec = conn.execute("SELECT embedding FROM publications WHERE id = %s", (pid,)).fetchone()
        if vec and vec["embedding"] is not None:
            rows = store.similar_meetings(
                conn, store.to_array(vec["embedding"]), limit=60, min_score=0.45
            )
    from inspraak.jobs.meetings_map import DEFAULT_LIMIT

    ids = [r["id"] for r in rows]
    by_category = jsonable(store.meeting_counts_by_category(conn, ids))
    meetings = []
    for r in rows[:shown]:
        meetings.append(
            {
                "id": r["id"],
                "cabinet": r["cabinet"],
                "ec_representative": r.get("ec_representative"),
                "date": r["meeting_date"].isoformat() if r["meeting_date"] else None,
                "subject": r["subject"],
                "score": round(float(r["score"]), 2),
                "orgs": r["orgs"],
            }
        )
    return {
        "note": "subject match, approximate",
        "cached": cached,
        "count": len(rows),
        "capped": len(rows) >= DEFAULT_LIMIT,
        "by_category": by_category,
        "by_category_chart": charts.bar_chart(
            [(r["category"], r["meetings"]) for r in by_category],
            title="Meetings by register category of the participants",
        ),
        "meetings": meetings,
        "source_file": links.MEETINGS_FILE,
        "source_portal": links.MEETINGS_PORTAL,
    }


# --------------------------------------------------------------------------- inbox


def inbox(conn: psycopg.Connection, *, decided_limit: int = 20) -> dict[str, Any]:
    open_items = [_inbox_item(conn, i) for i in list_review_items("open", limit=200)]
    decided = [_inbox_item(conn, i) for i in list_review_items("decided", limit=decided_limit)]
    decided.sort(key=lambda i: i.get("decided_at") or "", reverse=True)
    counts: dict[str, int] = {}
    for i in open_items:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    return {
        "open": open_items,
        "blocking": [i for i in open_items if i["blocking"]],
        "other": [i for i in open_items if not i["blocking"]],
        "decided": decided,
        "counts": {REVIEW_KIND_LABELS.get(k, k): v for k, v in counts.items()},
        "stances": list(STANCE_LABELS.items()),
    }


def inbox_item(conn: psycopg.Connection, item_id: int) -> dict[str, Any] | None:
    rows = conn.execute(
        "SELECT ri.id, ri.run_id, ri.tr_id, o.name AS organisation, ri.publication_id, "
        "p.title AS publication_title, ri.feedback_id, ri.kind, ri.blocking, ri.triggers, "
        "ri.payload, ri.status, ri.decision, ri.decided_by, ri.created_at, ri.decided_at "
        "FROM review_items ri LEFT JOIN organisations o ON o.tr_id = ri.tr_id "
        "LEFT JOIN publications p ON p.id = ri.publication_id WHERE ri.id = %s",
        (item_id,),
    ).fetchall()
    if not rows:
        return None
    return _inbox_item(conn, jsonable(dict(rows[0])))


def _inbox_item(conn: psycopg.Connection, item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    out["kind_label"] = REVIEW_KIND_LABELS.get(item["kind"], item["kind"])
    out["requested_by"] = None
    if item.get("run_id"):
        run_row = conn.execute(
            "SELECT requested_by FROM runs WHERE id = %s", (item["run_id"],)
        ).fetchone()
        out["requested_by"] = run_row["requested_by"] if run_row else None
    payload = item.get("payload") or {}
    out["payload"] = payload
    if item["kind"] == "go_no_go":
        for c in payload.get("consultations", []):
            c["days_left"] = days_left(c.get("end_date"))
            c["stances_labelled"] = {
                label_stance(k): v for k, v in (c.get("stances") or {}).items()
            }
    if item["kind"] == "draft_review":
        for d in payload.get("drafts", []):
            d["stance_label"] = label_stance(d.get("stance"))
    if item.get("feedback_id"):
        row = conn.execute(
            "SELECT f.organization, f.user_type, f.country, f.tr_number, f.company_size, "
            "p.initiative_id FROM feedback f "
            "JOIN publications p ON p.id = f.publication_id WHERE f.id = %s",
            (item["feedback_id"],),
        ).fetchone()
        if row:
            out["feedback"] = {
                "organization": display_submitter(
                    row["organization"],
                    tr_number=row["tr_number"],
                    company_size=row["company_size"],
                    country=row["country"],
                ),
                "user_type": label_type(row["user_type"]),
                "country": row["country"],
                "source": links.feedback(row["initiative_id"], item["feedback_id"]),
            }
    return out


# --------------------------------------------------------------------------- run


def run(run_id: str) -> dict[str, Any]:
    data = service_get_run(run_id)
    traces = data.get("traces") or []
    data["totals"] = {
        "nodes": len(traces),
        "llm_calls": sum(int(t.get("llm_calls") or 0) for t in traces),
        "prompt_tokens": sum(int(t.get("prompt_tokens") or 0) for t in traces),
        "output_tokens": sum(int(t.get("output_tokens") or 0) for t in traces),
        "cache_hits": sum(1 for t in traces if t.get("cache_hit") is True),
        "duration_ms": sum(int(t.get("duration_ms") or 0) for t in traces),
    }
    reviewed_by = next(
        (
            i.get("decided_by")
            for i in reversed(data.get("review_items") or [])
            if i.get("kind") == "draft_review" and i.get("status") == "decided"
        ),
        None,
    )
    with db.session(get_settings().database_url) as conn:
        data["drafts"] = [
            _draft_view({**d, "reviewed_by": reviewed_by}, conn) for d in data.get("drafts") or []
        ]
    data["active"] = data["status"] in ("queued", "running")
    data["stage_label"] = {
        "go_no_go": "waiting for the go / no-go decision",
        "draft_review": "waiting for the draft review",
        "done": "finished",
        "queued": "queued",
        "resuming": "queued to continue after the decision",
        "superseded": "replaced by a newer run (new match list)",
    }.get(data["stage"], data["stage"])
    result = data.get("result") or {}
    for m in result.get("matches") or []:
        m["days_left"] = days_left(m.get("end_date"))
    return data


def public_run(data: dict[str, Any]) -> dict[str, Any]:
    """A run as the static export shows it: the brief stage only, up to the moment the run
    pauses for the human go / no-go. Decisions, drafts, review items, who requested the
    run and the later agents' calls stay in the local app."""
    traces = data.get("traces") or []
    cut = next(
        (i for i, t in enumerate(traces) if t.get("agent") == "go_no_go"),
        len(traces) - 1,
    )
    shown = traces[: cut + 1]
    result = dict(data.get("result") or {})
    out = {
        k: data.get(k) for k in ("id", "tr_id", "organisation", "created_at", "error") if k in data
    }
    out.update(
        {
            "traces": shown,
            "result": {"matches": result.get("matches") or []},
            "drafts": [],
            "review_items": [],
            "public": True,
            "active": False,
            "status": "brief",
            "stage_label": "brief stage, up to the human go / no-go",
            "totals": {
                "nodes": len(shown),
                "llm_calls": sum(int(t.get("llm_calls") or 0) for t in shown),
                "prompt_tokens": sum(int(t.get("prompt_tokens") or 0) for t in shown),
                "output_tokens": sum(int(t.get("output_tokens") or 0) for t in shown),
                "cache_hits": sum(1 for t in shown if t.get("cache_hit") is True),
                "duration_ms": sum(
                    int(t.get("duration_ms") or 0) for t in shown if t.get("tool") != "interrupt"
                ),
            },
        }
    )
    return out


def exported_run_ids(conn: psycopg.Connection, tr_id: str) -> list[str]:
    """The one run a static organisation page links to: the latest that was not superseded."""
    row = conn.execute(
        "SELECT id FROM runs WHERE tr_id = %s AND stage IS DISTINCT FROM 'superseded' "
        "ORDER BY created_at DESC LIMIT 1",
        (tr_id,),
    ).fetchone()
    return [str(row["id"])] if row else []


# --------------------------------------------------------------------------- about


PROVIDER_LABELS = {
    "agy": "Gemini Flash, batch",
    "claude_cli": "Claude Haiku, batch",
    "gemini": "Gemini Flash Lite, API",
}
EVAL_PATH = Path(__file__).resolve().parents[2] / "docs" / "eval.json"


def load_evaluation(path: Path | None = None) -> dict[str, Any] | None:
    """docs/eval.json, written by `inspraak eval-report --write docs/eval.json`: the one
    source of the numbers on the about page, in the README and in docs/EVAL.md."""
    path = path or EVAL_PATH
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def about(conn: psycopg.Connection) -> dict[str, Any]:
    stats = store.snapshot_stats(conn)
    return {
        "stats": stats,
        "evaluation": load_evaluation(),
        "llm_calls": [
            {**c, "provider_label": PROVIDER_LABELS.get(c["provider"], c["provider"])}
            for c in llm_call_counts(conn)
            if c["provider"] != "fake"
        ],
        "snapshot_date": _snapshot_date(conn),
        "demo_count": conn.execute("SELECT count(DISTINCT tr_id) AS n FROM runs").fetchone()["n"],
        "sources": {
            "register": links.REGISTER_EXPORT,
            "hys": links.HYS,
            "meetings": links.MEETINGS_FILE,
        },
    }
