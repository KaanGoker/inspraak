"""Have Your Say: initiatives, publications and feedback into Postgres.

Endpoints (the website's own backend, undocumented, re-verified 2026-10-03):
- GET brpapi/searchInitiatives?size=100&page=N&language=EN
  Response: {"initiativeResultDtoPage": {content, totalPages, totalElements, ...}}.
  The receivingFeedbackStatus parameter is ignored by the server (OPEN, CLOSED, UPCOMING
  and no filter all return the same 4128 rows), so open ones are selected here from
  currentStatuses. `id` arrives as a float.
- GET brpapi/groupInitiatives/{id}: detail with publications and their attachments.
  Carries Commission staff contact fields (authorName, authorMail, ...) that are dropped.
- GET api/allFeedback?publicationId=..&size=100&page=N&sort=dateFeedback,ASC
  (note /api/, not /brpapi/). Plain Spring page: {content, totalPages, totalElements}.
  Rows carry firstName and surname for every user type; the privacy guard drops them
  before the page touches the disk. `feedbackTextUserLanguage`, despite its name, is
  the English translation of a non-English submission.

Scope (SPEC, V1): publications whose feedback period is open and closes within
`scope_open_days`, plus up to `scope_closed_max` recently closed publications with at
least `scope_closed_min_org` organisation submissions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from inspraak.config import Settings, get_settings, portal_now
from inspraak.http import Fetcher
from inspraak.ingest.cache import RawCache
from inspraak.privacy import assert_safe, scrub_feedback_page, scrub_initiative_detail
from inspraak.textutil import clean_text, parse_hys_datetime

PAGE_SIZE = 100
Log = Callable[[str], None]


def _noop(_: str) -> None:
    pass


# --------------------------------------------------------------------------- listing


def current_status(item: dict[str, Any]) -> dict[str, Any]:
    statuses = item.get("currentStatuses") or []
    for status in statuses:
        if status.get("isCurrent"):
            return status
    return statuses[0] if statuses else {}


def listing_to_initiative(item: dict[str, Any], listed_at: datetime) -> dict[str, Any]:
    status = current_status(item)
    return {
        "id": int(item["id"]),
        "reference": clean_text(item.get("reference")),
        "short_title": clean_text(item.get("shortTitle")),
        "act_type": item.get("foreseenActType"),
        "status": item.get("initiativeStatus"),
        "stage": status.get("frontEndStage"),
        "feedback_status": status.get("receivingFeedbackStatus"),
        "feedback_start": parse_hys_datetime(status.get("feedbackStartDate")),
        "feedback_end": parse_hys_datetime(status.get("feedbackEndDate")),
        "topics": [t["label"] for t in item.get("topics") or [] if t.get("label")],
        "listed_at": listed_at,
    }


def fetch_listing(
    fetcher: Fetcher, cache: RawCache, settings: Settings, log: Log = _noop
) -> list[dict]:
    """All initiatives from the search listing, page by page, cached per page."""
    url = f"{settings.hys_base}/brpapi/searchInitiatives"
    items: list[dict] = []
    page = 0
    total_pages: int | None = None
    while total_pages is None or page < total_pages:
        name = f"page-{page:04d}.json"
        payload = cache.get_json("listing", name)
        if payload is None:
            payload = fetcher.get_json(url, {"size": PAGE_SIZE, "page": page, "language": "EN"})
            cache.put_json(payload, "listing", name)
        block = payload["initiativeResultDtoPage"]
        if total_pages is None:
            total_pages = int(block["totalPages"])
            log(f"listing: {block['totalElements']} initiatives in {total_pages} pages")
        items.extend(block["content"])
        page += 1
    return items


_INITIATIVE_COLS = (
    "id", "reference", "short_title", "act_type", "status", "stage", "feedback_status",
    "feedback_start", "feedback_end", "topics", "listed_at",
)  # fmt: skip
_INITIATIVE_UPSERT = (
    f"INSERT INTO initiatives ({', '.join(_INITIATIVE_COLS)}) "
    f"VALUES ({', '.join('%s' for _ in _INITIATIVE_COLS)}) ON CONFLICT (id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _INITIATIVE_COLS if c != "id")
)


def store_initiatives(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> int:
    unique = {r["id"]: r for r in rows}
    with conn.cursor() as cur:
        cur.executemany(
            _INITIATIVE_UPSERT, [[r[c] for c in _INITIATIVE_COLS] for r in unique.values()]
        )
    conn.commit()
    return len(unique)


# --------------------------------------------------------------------------- scope


@dataclass
class ScopePlan:
    open_ids: list[int] = field(default_factory=list)
    closed_candidate_ids: list[int] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def plan_scope(initiatives: list[dict[str, Any]], now: datetime, settings: Settings) -> ScopePlan:
    """Pick the initiatives whose detail is worth fetching, from listing data alone."""
    plan = ScopePlan()
    horizon = now + timedelta(days=settings.scope_open_days)
    lookback = now - timedelta(days=settings.scope_closed_lookback_days)
    status_counts: dict[str, int] = {}
    closed: list[tuple[datetime, int]] = []
    for row in initiatives:
        status = row["feedback_status"] or "NONE"
        status_counts[status] = status_counts.get(status, 0) + 1
        end = row["feedback_end"]
        if end is None:
            continue
        if status == "OPEN" and now <= end <= horizon:
            plan.open_ids.append(row["id"])
        elif end < now and end >= lookback:
            closed.append((end, row["id"]))
    closed.sort(reverse=True)
    plan.closed_candidate_ids = [i for _, i in closed[: settings.scope_closed_scan_limit]]
    plan.counts = {
        "listed": len(initiatives),
        "open_within_window": len(plan.open_ids),
        "closed_in_lookback": len(closed),
        "closed_scanned": len(plan.closed_candidate_ids),
        **{f"status_{k}": v for k, v in sorted(status_counts.items())},
    }
    return plan


# --------------------------------------------------------------------------- detail


def fetch_detail(
    fetcher: Fetcher, cache: RawCache, settings: Settings, initiative_id: int, *, forever: bool
) -> dict[str, Any]:
    name = f"{initiative_id}.json"
    payload = cache.get_json("initiatives", name, forever=forever)
    if payload is None:
        raw = fetcher.get_json(f"{settings.hys_base}/brpapi/groupInitiatives/{initiative_id}")
        payload = scrub_initiative_detail(raw)
        cache.put_json(payload, "initiatives", name)
    return payload


def _labels(items: Any) -> list[str]:
    return [i["label"] for i in items or [] if isinstance(i, dict) and i.get("label")]


def detail_to_rows(detail: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Initiative fields that only the detail has, plus one row per publication."""
    initiative = {
        "id": int(detail["id"]),
        "dg": detail.get("dg"),
        "unit": detail.get("unit"),
        "dossier_summary": clean_text(detail.get("dossierSummary")),
        "policy_areas": _labels(detail.get("policyAreas")),
        "published_at": parse_hys_datetime(detail.get("publishedDate")),
        "modified_at": parse_hys_datetime(detail.get("modifiedDate")),
    }
    publications = []
    for pub in detail.get("publications") or []:
        attachments = [
            {
                "documentId": a.get("documentId"),
                "filename": a.get("filename") or a.get("ersFileName"),
                "title": clean_text(a.get("title")),
                "language": a.get("language"),
                "category": a.get("category"),
                "pages": a.get("pages"),
                "size": a.get("size"),
                "isOriginal": a.get("isOriginal"),
            }
            for a in pub.get("attachments") or []
            if a.get("documentId")
        ]
        publications.append(
            {
                "id": int(pub["id"]),
                "initiative_id": initiative["id"],
                "type": pub.get("type"),
                "stage": pub.get("stage") or pub.get("frontEndStage"),
                "reference": clean_text(pub.get("reference")),
                "title": clean_text(pub.get("title")),
                "feedback_status": pub.get("receivingFeedbackStatus"),
                "published_at": parse_hys_datetime(pub.get("publishedDate")),
                "end_date": parse_hys_datetime(pub.get("endDate")),
                "total_feedback": pub.get("totalFeedback"),
                "attachments": attachments,
            }
        )
    return initiative, publications


_PUB_COLS = (
    "id", "initiative_id", "type", "stage", "reference", "title", "feedback_status",
    "published_at", "end_date", "total_feedback", "attachments",
)  # fmt: skip
_PUB_UPSERT = (
    f"INSERT INTO publications ({', '.join(_PUB_COLS)}) "
    f"VALUES ({', '.join('%s' for _ in _PUB_COLS)}) ON CONFLICT (id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _PUB_COLS if c not in ("id", "initiative_id"))
    # A grouped call for evidence arrives once per parent initiative: keep one stable parent.
    + ", initiative_id = LEAST(publications.initiative_id, EXCLUDED.initiative_id)"
)
_PUB_LINK = (
    "INSERT INTO publication_initiatives (publication_id, initiative_id) VALUES (%s, %s) "
    "ON CONFLICT DO NOTHING"
)


def store_detail(conn: psycopg.Connection, detail: dict[str, Any]) -> list[dict[str, Any]]:
    initiative, publications = detail_to_rows(detail)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE initiatives SET dg = %s, unit = %s, dossier_summary = %s, policy_areas = %s, "
            "published_at = %s, modified_at = %s, detail_fetched_at = now() WHERE id = %s",
            (
                initiative["dg"], initiative["unit"], initiative["dossier_summary"],
                initiative["policy_areas"], initiative["published_at"],
                initiative["modified_at"], initiative["id"],
            ),
        )  # fmt: skip
        if cur.rowcount == 0:
            # Detail fetched for an initiative that is not in the listing: insert a stub.
            cur.execute(
                "INSERT INTO initiatives (id, reference, short_title, act_type, status, dg, unit, "
                "dossier_summary, policy_areas, published_at, modified_at, detail_fetched_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (id) DO NOTHING",
                (
                    initiative["id"], clean_text(detail.get("reference")),
                    clean_text(detail.get("shortTitle")), detail.get("foreseenActType"),
                    detail.get("initiativeStatus"), initiative["dg"], initiative["unit"],
                    initiative["dossier_summary"], initiative["policy_areas"],
                    initiative["published_at"], initiative["modified_at"],
                ),
            )  # fmt: skip
        for pub in publications:
            values = [Jsonb(pub[c]) if c == "attachments" else pub[c] for c in _PUB_COLS]
            cur.execute(_PUB_UPSERT, values)
            cur.execute(_PUB_LINK, (pub["id"], initiative["id"]))
    conn.commit()
    return publications


# --------------------------------------------------------------------------- feedback


def feedback_row_to_db(row: dict[str, Any], publication_id: int) -> dict[str, Any]:
    """A scrubbed allFeedback row to a `feedback` table row. Refuses unscrubbed input."""
    assert_safe(row)
    citizen = bool(row["isCitizen"])
    attachments = row.get("attachments") or []
    return {
        "id": int(row["id"]),
        "publication_id": publication_id,
        "user_type": row.get("userType") or "UNKNOWN",
        "is_citizen": citizen,
        "country": row.get("country"),
        "language": row.get("language"),
        "date_feedback": parse_hys_datetime(row.get("dateFeedback")),
        "organization": None if citizen else clean_text(row.get("organization")),
        "tr_number": None if citizen else (row.get("trNumber") or row.get("tr_number") or None),
        "company_size": None if citizen else row.get("companySize"),
        "scope": None if citizen else row.get("scope"),
        "governance_level": None if citizen else row.get("governanceLevel"),
        "visibility": row.get("publication"),
        "feedback_text": None if citizen else clean_text(row.get("feedback")),
        "feedback_text_en": None if citizen else clean_text(row.get("feedbackTextUserLanguage")),
        "attachment_count": row.get("attachmentCount", len(attachments)),
        "attachments": [] if citizen else attachments,
    }


_FB_COLS = (
    "id", "publication_id", "user_type", "is_citizen", "country", "language", "date_feedback",
    "organization", "tr_number", "company_size", "scope", "governance_level", "visibility",
    "feedback_text", "feedback_text_en", "attachment_count",
)  # fmt: skip
_FB_UPSERT = (
    f"INSERT INTO feedback ({', '.join(_FB_COLS)}) "
    f"VALUES ({', '.join('%s' for _ in _FB_COLS)}) ON CONFLICT (id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _FB_COLS if c != "id")
    + ", embedding = CASE WHEN feedback.feedback_text IS DISTINCT FROM EXCLUDED.feedback_text "
    "THEN NULL ELSE feedback.embedding END"
)
_ATT_UPSERT = (
    "INSERT INTO documents (document_id, kind, publication_id, feedback_id, filename, "
    "pages_declared, size_bytes) VALUES (%s, 'feedback_attachment', %s, %s, %s, %s, %s) "
    "ON CONFLICT (document_id) DO UPDATE SET filename = EXCLUDED.filename, "
    "pages_declared = EXCLUDED.pages_declared"
)


@dataclass
class FeedbackResult:
    publication_id: int
    total: int
    fetched: int
    organisations: int
    citizens: int
    truncated: bool
    pages: int


def fetch_feedback(
    fetcher: Fetcher,
    cache: RawCache,
    settings: Settings,
    publication_id: int,
    *,
    forever: bool,
    max_rows: int | None = None,
) -> tuple[list[dict[str, Any]], int, bool, int]:
    """All scrubbed rows of a publication: (rows, totalElements, truncated, pages)."""
    max_rows = settings.feedback_max_rows if max_rows is None else max_rows
    url = f"{settings.hys_base}/api/allFeedback"
    rows: list[dict[str, Any]] = []
    page = 0
    total = 0
    total_pages: int | None = None
    while total_pages is None or page < total_pages:
        name = f"page-{page:04d}.json"
        payload = cache.get_json("feedback", str(publication_id), name, forever=forever)
        if payload is None:
            raw = fetcher.get_json(
                url,
                {
                    "publicationId": publication_id,
                    "size": PAGE_SIZE,
                    "page": page,
                    "sort": "dateFeedback,ASC",
                },
            )
            payload = scrub_feedback_page(raw)
            cache.put_json(payload, "feedback", str(publication_id), name)
        total = int(payload.get("totalElements") or 0)
        total_pages = int(payload.get("totalPages") or 0)
        rows.extend(payload.get("content") or [])
        page += 1
        if len(rows) >= max_rows:
            break
    rows = rows[:max_rows]
    return rows, total, len(rows) < total, page


def store_feedback(
    conn: psycopg.Connection,
    publication_id: int,
    rows: list[dict[str, Any]],
    total: int,
    truncated: bool,
) -> FeedbackResult:
    db_rows = [feedback_row_to_db(r, publication_id) for r in rows]
    unique = {r["id"]: r for r in db_rows}
    orgs = sum(1 for r in unique.values() if not r["is_citizen"])
    with conn.cursor() as cur:
        cur.executemany(_FB_UPSERT, [[r[c] for c in _FB_COLS] for r in unique.values()])
        cur.executemany(
            _ATT_UPSERT,
            [
                (
                    a["documentId"], publication_id, r["id"],
                    a.get("fileName") or a.get("ersFileName"), a.get("pages"),
                    a.get("pdfSize") or a.get("size"),
                )
                for r in unique.values()
                for a in r["attachments"]
                if a.get("documentId")
            ],
        )  # fmt: skip
        cur.execute(
            "UPDATE publications SET feedback_fetched_at = now(), feedback_truncated = %s, "
            "org_feedback_count = %s, citizen_feedback_count = %s, total_feedback = %s "
            "WHERE id = %s",
            (truncated, orgs, len(unique) - orgs, total, publication_id),
        )
    conn.commit()
    return FeedbackResult(
        publication_id=publication_id,
        total=total,
        fetched=len(unique),
        organisations=orgs,
        citizens=len(unique) - orgs,
        truncated=truncated,
        pages=0,
    )


# --------------------------------------------------------------------------- orchestration


def choose_proposal_documents(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The Commission documents worth reading: English where it exists, else one
    fallback language per category, in a fixed preference order."""
    preferred = ("EN", "FR", "DE", "NL")
    by_category: dict[str, list[dict[str, Any]]] = {}
    for a in attachments:
        by_category.setdefault(a.get("category") or "OTHER", []).append(a)
    chosen: list[dict[str, Any]] = []
    for group in by_category.values():
        for lang in preferred:
            hit = [a for a in group if a.get("language") == lang]
            if hit:
                chosen.extend(hit)
                break
        else:
            chosen.append(group[0])
    return chosen


def register_proposal_documents(
    conn: psycopg.Connection, publication_id: int, attachments: list[dict[str, Any]]
) -> int:
    chosen = choose_proposal_documents(attachments)
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO documents (document_id, kind, publication_id, title, filename, language, "
            "category, pages_declared, size_bytes) "
            "VALUES (%s, 'proposal', %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (document_id) DO UPDATE SET title = EXCLUDED.title, "
            "language = EXCLUDED.language, pages_declared = EXCLUDED.pages_declared",
            [
                (
                    a["documentId"], publication_id, a.get("title"), a.get("filename"),
                    a.get("language"), a.get("category"), a.get("pages"), a.get("size"),
                )
                for a in chosen
            ],
        )  # fmt: skip
    conn.commit()
    return len(chosen)


def mark_scope(conn: psycopg.Connection, publication_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE publications SET in_scope = true, scope_reason = %s WHERE id = %s",
        (reason, publication_id),
    )
    conn.commit()


def run_hys(
    conn: psycopg.Connection,
    fetcher: Fetcher | None = None,
    cache: RawCache | None = None,
    *,
    now: datetime | None = None,
    skip_closed: bool = False,
    log: Log = _noop,
) -> dict[str, Any]:
    """The whole V1 snapshot: listing, scope, details, feedback. Resumable via the cache."""
    settings = get_settings()
    fetcher = fetcher or Fetcher()
    cache = cache or RawCache()
    now = now or portal_now()

    items = fetch_listing(fetcher, cache, settings, log)
    rows = [listing_to_initiative(i, now) for i in items]
    stored = store_initiatives(conn, rows)
    log(f"stored {stored} initiatives")

    plan = plan_scope(rows, now, settings)
    log(f"scope plan: {plan.counts}")
    stats: dict[str, Any] = {"initiatives": stored, **plan.counts}
    stats.update(
        open_publications=0, closed_publications=0, feedback_rows=0, feedback_orgs=0,
        feedback_citizens=0, truncated_publications=0, proposal_documents=0, details_fetched=0,
    )  # fmt: skip

    # A grouped publication shows up under each of its initiatives: handle it once.
    seen: set[int] = set()

    # Open: every publication that is open and closes inside the window.
    horizon = now + timedelta(days=settings.scope_open_days)
    for initiative_id in plan.open_ids:
        detail = fetch_detail(fetcher, cache, settings, initiative_id, forever=False)
        stats["details_fetched"] += 1
        for pub in store_detail(conn, detail):
            end = pub["end_date"]
            if pub["feedback_status"] != "OPEN" or end is None or not (now <= end <= horizon):
                continue
            if pub["id"] in seen:
                continue
            seen.add(pub["id"])
            rows_fb, total, truncated, _ = fetch_feedback(
                fetcher, cache, settings, pub["id"], forever=False
            )
            result = store_feedback(conn, pub["id"], rows_fb, total, truncated)
            mark_scope(conn, pub["id"], f"open, closes {end:%Y-%m-%d}")
            stats["proposal_documents"] += register_proposal_documents(
                conn, pub["id"], pub["attachments"]
            )
            _bump(stats, result, "open_publications")
            log(
                f"open {pub['id']} {pub['type']} ends {end:%Y-%m-%d}: {result.fetched} rows, "
                f"{result.organisations} organisations"
            )

    # Closed: most recent first, keep the ones with enough organisation submissions.
    if not skip_closed:
        kept = 0
        lookback = now - timedelta(days=settings.scope_closed_lookback_days)
        for initiative_id in plan.closed_candidate_ids:
            if kept >= settings.scope_closed_max:
                break
            detail = fetch_detail(fetcher, cache, settings, initiative_id, forever=True)
            stats["details_fetched"] += 1
            for pub in store_detail(conn, detail):
                if kept >= settings.scope_closed_max:
                    break
                end = pub["end_date"]
                if end is None or not (lookback <= end < now):
                    continue
                if (pub["total_feedback"] or 0) < settings.scope_closed_min_org:
                    continue
                if pub["id"] in seen:
                    continue
                seen.add(pub["id"])
                rows_fb, total, truncated, _ = fetch_feedback(
                    fetcher, cache, settings, pub["id"], forever=True
                )
                result = store_feedback(conn, pub["id"], rows_fb, total, truncated)
                if result.organisations < settings.scope_closed_min_org:
                    log(
                        f"closed {pub['id']} skipped: {result.organisations} organisations "
                        f"of {result.fetched}"
                    )
                    continue
                kept += 1
                mark_scope(
                    conn, pub["id"], f"closed {end:%Y-%m-%d}, {result.organisations} organisations"
                )
                stats["proposal_documents"] += register_proposal_documents(
                    conn, pub["id"], pub["attachments"]
                )
                _bump(stats, result, "closed_publications")
                log(
                    f"closed {pub['id']} {pub['type']} ended {end:%Y-%m-%d}: "
                    f"{result.fetched} rows, {result.organisations} organisations (kept {kept})"
                )

    final = refresh_closed_since_fetch(conn, fetcher, cache, settings, now=now, seen=seen, log=log)
    stats["closed_since_fetch"] = len(final)

    stats["requests"] = fetcher.requests
    stats["cache_hits"] = cache.hits
    stats["cache_misses"] = cache.misses
    return stats


def refresh_closed_since_fetch(
    conn: psycopg.Connection,
    fetcher: Fetcher,
    cache: RawCache,
    settings: Settings,
    *,
    now: datetime,
    seen: set[int] | None = None,
    log: Log = _noop,
) -> list[FeedbackResult]:
    """In-scope publications whose feedback period ended after their last fetch get one
    last fetch that bypasses the page cache, so their final counts and deadline are true.
    They stay in scope; the pages and the matcher treat them as closed by their end date."""
    seen = seen if seen is not None else set()
    rows = conn.execute(
        "SELECT id, initiative_id FROM publications WHERE in_scope AND end_date < %s "
        "AND (feedback_fetched_at IS NULL "
        "OR feedback_fetched_at < (end_date AT TIME ZONE 'Europe/Brussels')) ORDER BY id",
        (now,),
    ).fetchall()
    uncached = RawCache(root=cache.root, max_age_hours=0)
    out: list[FeedbackResult] = []
    details: set[int] = set()
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        if row["initiative_id"] not in details:
            details.add(row["initiative_id"])
            store_detail(
                conn, fetch_detail(fetcher, uncached, settings, row["initiative_id"], forever=False)
            )
        end = conn.execute(
            "SELECT end_date FROM publications WHERE id = %s", (row["id"],)
        ).fetchone()["end_date"]
        rows_fb, total, truncated, _ = fetch_feedback(
            fetcher, uncached, settings, row["id"], forever=False
        )
        result = store_feedback(conn, row["id"], rows_fb, total, truncated)
        if end is not None and end < now:
            mark_scope(
                conn, row["id"], f"closed {end:%Y-%m-%d}, {result.organisations} organisations"
            )
        out.append(result)
        log(
            f"closed since last fetch {row['id']}: {result.fetched} rows, "
            f"{result.organisations} organisations"
        )
    return out


def _bump(stats: dict[str, Any], result: FeedbackResult, key: str) -> None:
    stats[key] += 1
    stats["feedback_rows"] += result.fetched
    stats["feedback_orgs"] += result.organisations
    stats["feedback_citizens"] += result.citizens
    stats["truncated_publications"] += int(result.truncated)
