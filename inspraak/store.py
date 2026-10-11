"""Read-side queries over the snapshot. Later stages (matcher, landscape, lobby mapper,
web, export) call these instead of writing SQL. Every function takes an open
connection from `inspraak.db` and returns plain dicts.

Nothing here calls the live endpoints: the demo runs from the local snapshot.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import psycopg

from inspraak.config import portal_now

CITIZEN_TYPES = ("EU_CITIZEN", "NON_EU_CITIZEN")


def to_array(value: Any) -> np.ndarray:
    """A vector column value as a float32 numpy array. The driver hands back its own
    Vector type here, not an array; both are accepted as query parameters."""
    if hasattr(value, "to_numpy"):
        value = value.to_numpy()
    return np.asarray(value, dtype=np.float32)


# --------------------------------------------------------------------------- organisations


def find_organisations(conn: psycopg.Connection, query: str, *, limit: int = 20) -> list[dict]:
    """Name, acronym or register ID lookup. Exact ID first, then exact acronyms, then
    trigram similarity. Brussels is the point: an office with its head office in Belgium
    gets a small boost, so "WWF" finds the European Policy Office before national ones."""
    q = query.strip()
    if not q:
        return []
    exact = conn.execute(
        "SELECT tr_id, name, acronym, category, head_country, head_city FROM organisations "
        "WHERE tr_id = %s OR lower(acronym) = lower(%s) "
        "ORDER BY (head_country = 'BELGIUM') DESC, name",
        (q, q),
    ).fetchall()
    if exact and q == exact[0]["tr_id"]:
        return exact
    fuzzy = conn.execute(
        "SELECT tr_id, name, acronym, category, head_country, head_city, "
        "similarity(lower(name), lower(%s)) AS score FROM organisations "
        "WHERE lower(name) %% lower(%s) OR lower(name) LIKE '%%' || lower(%s) || '%%' "
        "ORDER BY (lower(name) = lower(%s)) DESC, score DESC, name LIMIT %s",
        (q, q, q, q, limit * 4),
    ).fetchall()
    seen = {r["tr_id"] for r in exact}
    rows = exact + [r for r in fuzzy if r["tr_id"] not in seen]
    ql = q.lower()

    def rank(r: dict) -> int:
        belgian = r.get("head_country") == "BELGIUM"
        acronym = (r.get("acronym") or "").lower() == ql
        named = (r.get("name") or "").lower().startswith(ql)
        if (r.get("name") or "").lower() == ql:
            return 0
        if belgian and (acronym or named):
            return 1
        return 2 if acronym else 3

    return sorted(rows, key=rank)[:limit]  # stable: trigram order inside each rank


def get_organisation(conn: psycopg.Connection, tr_id: str) -> dict | None:
    return conn.execute(
        "SELECT tr_id, name, name_latin, acronym, entity_form, website, category, "
        "registration_date, last_update, head_country, head_city, eu_country, eu_city, goals, "
        "levels_of_interest, interests, eu_legislative_proposals, interest_represented, "
        "members_fte, ep_accredited, fin_year_start, fin_year_end, costs_min, costs_max, "
        "total_budget, profile_text, profile_embedding IS NOT NULL AS has_embedding "
        "FROM organisations WHERE tr_id = %s",
        (tr_id,),
    ).fetchone()


def organisation_vector(conn: psycopg.Connection, tr_id: str) -> np.ndarray | None:
    row = conn.execute(
        "SELECT profile_embedding FROM organisations WHERE tr_id = %s", (tr_id,)
    ).fetchone()
    if row is None or row["profile_embedding"] is None:
        return None
    return to_array(row["profile_embedding"])


def organisation_submissions(conn: psycopg.Connection, tr_id: str) -> list[dict]:
    """Past feedback this organisation filed (its own voice), newest first."""
    return conn.execute(
        "SELECT f.id, f.publication_id, p.title AS publication_title, i.short_title, "
        "f.date_feedback, f.language, f.feedback_text, f.feedback_text_en, f.attachment_count "
        "FROM feedback f JOIN publications p ON p.id = f.publication_id "
        "JOIN initiatives i ON i.id = p.initiative_id "
        "WHERE f.tr_number = %s ORDER BY f.date_feedback DESC",
        (tr_id,),
    ).fetchall()


# --------------------------------------------------------------------------- consultations


def list_publications(
    conn: psycopg.Connection, *, in_scope: bool = True, open_only: bool = False
) -> list[dict]:
    sql = (
        "SELECT p.id, p.initiative_id, i.reference, i.short_title, p.type, p.title, "
        "p.feedback_status, p.published_at, p.end_date, p.total_feedback, "
        "p.org_feedback_count, p.citizen_feedback_count, p.in_scope, p.scope_reason, "
        "i.topics, i.dg, p.feedback_truncated "
        "FROM publications p JOIN initiatives i ON i.id = p.initiative_id WHERE true"
    )
    params: list[Any] = []
    if in_scope:
        sql += " AND p.in_scope"
    if open_only:
        sql += " AND p.feedback_status = 'OPEN' AND p.end_date >= %s"
        params.append(portal_now())
    sql += " ORDER BY p.end_date"
    return conn.execute(sql, params).fetchall()


def get_publication(conn: psycopg.Connection, publication_id: int) -> dict | None:
    return conn.execute(
        "SELECT p.*, i.reference AS initiative_reference, i.short_title, i.dossier_summary, "
        "i.topics, i.policy_areas, i.dg, i.unit, i.act_type, i.stage AS initiative_stage "
        "FROM publications p JOIN initiatives i ON i.id = p.initiative_id WHERE p.id = %s",
        (publication_id,),
    ).fetchone()


def publication_initiatives(conn: psycopg.Connection, publication_id: int) -> list[dict]:
    """Every initiative a publication belongs to (a grouped call for evidence has several)."""
    return conn.execute(
        "SELECT i.id, i.reference, i.short_title, i.act_type, i.stage, i.dg, i.topics "
        "FROM publication_initiatives pi JOIN initiatives i ON i.id = pi.initiative_id "
        "WHERE pi.publication_id = %s ORDER BY i.id",
        (publication_id,),
    ).fetchall()


def similar_publications(
    conn: psycopg.Connection,
    vector: np.ndarray,
    *,
    limit: int = 10,
    in_scope: bool = True,
    open_only: bool = False,
) -> list[dict]:
    """Cosine similarity of the publication match text against a profile vector."""
    sql = (
        "SELECT p.id, p.initiative_id, i.short_title, p.type, p.title, p.end_date, "
        "p.feedback_status, p.org_feedback_count, p.citizen_feedback_count, "
        "1 - (p.embedding <=> %s) AS score "
        "FROM publications p JOIN initiatives i ON i.id = p.initiative_id "
        "WHERE p.embedding IS NOT NULL"
    )
    params: list[Any] = [vector]
    if in_scope:
        sql += " AND p.in_scope"
    if open_only:
        sql += " AND p.feedback_status = 'OPEN' AND p.end_date >= %s"
        params.append(portal_now())
    sql += " ORDER BY p.embedding <=> %s LIMIT %s"
    params += [vector, limit]
    return conn.execute(sql, params).fetchall()


# --------------------------------------------------------------------------- landscape


def feedback_counts(conn: psycopg.Connection, publication_id: int) -> dict[str, list[dict]]:
    """Who submitted: by stakeholder type and by country. Citizens are counted, never read."""
    by_type = conn.execute(
        "SELECT user_type, is_citizen, count(*) AS n FROM feedback WHERE publication_id = %s "
        "GROUP BY user_type, is_citizen ORDER BY n DESC",
        (publication_id,),
    ).fetchall()
    by_country = conn.execute(
        "SELECT country, count(*) AS n, count(*) FILTER (WHERE NOT is_citizen) AS organisations "
        "FROM feedback WHERE publication_id = %s GROUP BY country ORDER BY n DESC",
        (publication_id,),
    ).fetchall()
    return {"by_type": by_type, "by_country": by_country}


def organisation_feedback(conn: psycopg.Connection, publication_id: int) -> list[dict]:
    """Organisation submissions with the register category joined in where the ID matches."""
    return conn.execute(
        "SELECT f.id, f.user_type, f.country, f.language, f.date_feedback, f.organization, "
        "f.tr_number, f.company_size, f.feedback_text, f.feedback_text_en, f.attachment_count, "
        "o.category AS register_category, o.head_country AS register_country, "
        "o.name AS register_name "
        "FROM feedback f LEFT JOIN organisations o ON o.tr_id = f.tr_number "
        "WHERE f.publication_id = %s AND NOT f.is_citizen ORDER BY f.date_feedback",
        (publication_id,),
    ).fetchall()


def feedback_attachments(conn: psycopg.Connection, feedback_id: int) -> list[dict]:
    return conn.execute(
        "SELECT document_id, filename, pages_declared, parse_status, pages_extracted, "
        "chars_extracted FROM documents WHERE feedback_id = %s ORDER BY document_id",
        (feedback_id,),
    ).fetchall()


# --------------------------------------------------------------------------- reader


def publication_documents(conn: psycopg.Connection, publication_id: int) -> list[dict]:
    return conn.execute(
        "SELECT document_id, kind, title, filename, language, category, pages_declared, "
        "parse_status, parse_error, pages_extracted, chars_extracted "
        "FROM documents WHERE publication_id = %s AND kind = 'proposal' ORDER BY document_id",
        (publication_id,),
    ).fetchall()


def document_page(conn: psycopg.Connection, document_id: str, page_no: int) -> str | None:
    row = conn.execute(
        "SELECT text FROM document_pages WHERE document_id = %s AND page_no = %s",
        (document_id, page_no),
    ).fetchone()
    return None if row is None else row["text"]


def similar_chunks(
    conn: psycopg.Connection,
    publication_id: int,
    vector: np.ndarray,
    *,
    limit: int = 12,
    kind: str = "proposal",
) -> list[dict]:
    """Chunks of this publication's documents closest to a vector, with page numbers."""
    return conn.execute(
        "SELECT c.id, c.document_id, d.title AS document_title, d.language, c.page_no, "
        "c.chunk_no, c.text, 1 - (c.embedding <=> %s) AS score "
        "FROM chunks c JOIN documents d ON d.document_id = c.document_id "
        "WHERE d.publication_id = %s AND d.kind = %s AND c.embedding IS NOT NULL "
        "ORDER BY c.embedding <=> %s LIMIT %s",
        (vector, publication_id, kind, vector, limit),
    ).fetchall()


# --------------------------------------------------------------------------- lobby mapper


def meetings_for_organisation(conn: psycopg.Connection, tr_id: str) -> list[dict]:
    return conn.execute(
        "SELECT m.id, m.cabinet, m.ec_representative, m.ec_title, m.meeting_date, m.location, "
        "m.subject, mo.org_name FROM meeting_orgs mo JOIN meetings m ON m.id = mo.meeting_id "
        "WHERE mo.tr_id = %s ORDER BY m.meeting_date DESC",
        (tr_id,),
    ).fetchall()


def similar_meetings(
    conn: psycopg.Connection, vector: np.ndarray, *, limit: int = 50, min_score: float = 0.0
) -> list[dict]:
    """Meetings whose raw subject is close to a vector (approximate by construction).
    Each row carries the organisations with their register category when the ID joins."""
    return conn.execute(
        "SELECT m.id, m.cabinet, m.ec_representative, m.meeting_date, m.subject, "
        "1 - (m.embedding <=> %s) AS score, "
        "json_agg(json_build_object('tr_id', mo.tr_id, 'name', COALESCE(o.name, mo.org_name), "
        "'category', o.category, 'country', o.head_country) ORDER BY mo.position) AS orgs "
        "FROM meetings m JOIN meeting_orgs mo ON mo.meeting_id = m.id "
        "LEFT JOIN organisations o ON o.tr_id = mo.tr_id "
        "WHERE m.embedding IS NOT NULL AND 1 - (m.embedding <=> %s) >= %s "
        "GROUP BY m.id ORDER BY m.embedding <=> %s LIMIT %s",
        (vector, vector, min_score, vector, limit),
    ).fetchall()


def meeting_counts_by_category(conn: psycopg.Connection, meeting_ids: list[str]) -> list[dict]:
    if not meeting_ids:
        return []
    return conn.execute(
        "SELECT COALESCE(o.category, 'not in register export') AS category, "
        "count(DISTINCT mo.meeting_id) AS meetings, count(DISTINCT mo.tr_id) AS organisations "
        "FROM meeting_orgs mo LEFT JOIN organisations o ON o.tr_id = mo.tr_id "
        "WHERE mo.meeting_id = ANY(%s) GROUP BY 1 ORDER BY meetings DESC",
        (meeting_ids,),
    ).fetchall()


# --------------------------------------------------------------------------- stats


def snapshot_stats(conn: psycopg.Connection) -> dict[str, Any]:
    one = lambda sql: conn.execute(sql).fetchone()["n"]  # noqa: E731
    return {
        "organisations": one("SELECT count(*) AS n FROM organisations"),
        "organisations_belgium": one(
            "SELECT count(*) AS n FROM organisations WHERE head_country = 'BELGIUM'"
        ),
        "organisations_embedded": one(
            "SELECT count(*) AS n FROM organisations WHERE profile_embedding IS NOT NULL"
        ),
        "initiatives": one("SELECT count(*) AS n FROM initiatives"),
        "initiatives_with_detail": one(
            "SELECT count(*) AS n FROM initiatives WHERE detail_fetched_at IS NOT NULL"
        ),
        "publications": one("SELECT count(*) AS n FROM publications"),
        "publications_in_scope": one("SELECT count(*) AS n FROM publications WHERE in_scope"),
        "publications_open_in_scope": one(
            "SELECT count(*) AS n FROM publications WHERE in_scope AND feedback_status = 'OPEN'"
        ),
        "publications_embedded": one(
            "SELECT count(*) AS n FROM publications WHERE embedding IS NOT NULL"
        ),
        "feedback": one("SELECT count(*) AS n FROM feedback"),
        "feedback_organisations": one("SELECT count(*) AS n FROM feedback WHERE NOT is_citizen"),
        "feedback_citizens": one("SELECT count(*) AS n FROM feedback WHERE is_citizen"),
        "feedback_with_tr_id": one(
            "SELECT count(*) AS n FROM feedback WHERE tr_number IS NOT NULL"
        ),
        "feedback_tr_id_in_register": one(
            "SELECT count(*) AS n FROM feedback f JOIN organisations o ON o.tr_id = f.tr_number"
        ),
        "feedback_embedded": one("SELECT count(*) AS n FROM feedback WHERE embedding IS NOT NULL"),
        "documents": one("SELECT count(*) AS n FROM documents"),
        "documents_proposal": one("SELECT count(*) AS n FROM documents WHERE kind = 'proposal'"),
        "documents_attachment": one(
            "SELECT count(*) AS n FROM documents WHERE kind = 'feedback_attachment'"
        ),
        "documents_parsed_ok": one("SELECT count(*) AS n FROM documents WHERE parse_status = 'ok'"),
        "documents_empty": one("SELECT count(*) AS n FROM documents WHERE parse_status = 'empty'"),
        "documents_error": one("SELECT count(*) AS n FROM documents WHERE parse_status = 'error'"),
        "document_pages": one("SELECT count(*) AS n FROM document_pages"),
        "chunks": one("SELECT count(*) AS n FROM chunks"),
        "chunks_embedded": one("SELECT count(*) AS n FROM chunks WHERE embedding IS NOT NULL"),
        "meetings": one("SELECT count(*) AS n FROM meetings"),
        "meeting_org_links": one("SELECT count(*) AS n FROM meeting_orgs"),
        "meeting_orgs_in_register": one(
            "SELECT count(*) AS n FROM meeting_orgs mo JOIN organisations o ON o.tr_id = mo.tr_id"
        ),
        "meetings_embedded": one("SELECT count(*) AS n FROM meetings WHERE embedding IS NOT NULL"),
    }
