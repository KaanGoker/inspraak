"""Reader, publication layer: one summary per publication, cached forever and shared by
every organisation. One model call per publication, ever.

Material for the call: the publication title, the dossier summary, the opening of the
Commission document (explanatory memorandum, typically) and the chunks closest to the
publication's own match text. About 14,000 characters at most.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from inspraak import store
from inspraak.llm.base import Provider
from inspraak.llm.schemas import PublicationSummary

OPENING_CHARS = 9000
SIMILAR_CHUNKS = 8

SYSTEM = (
    "You summarise European Commission consultation documents for an EU affairs audience. "
    "Neutral, factual, no advocacy. Write only what the text says. Use plain hyphens, never "
    "long dashes."
)


def get_summary(conn: psycopg.Connection, publication_id: int) -> dict | None:
    return conn.execute(
        "SELECT publication_id, provider, model, summary, source_chunks, created_at "
        "FROM publication_summaries WHERE publication_id = %s",
        (publication_id,),
    ).fetchone()


def _opening_chunks(conn: psycopg.Connection, publication_id: int, budget: int) -> list[dict]:
    rows = conn.execute(
        "SELECT c.id, c.document_id, c.page_no, c.chunk_no, c.text FROM chunks c "
        "JOIN documents d ON d.document_id = c.document_id "
        "WHERE d.publication_id = %s AND d.kind = 'proposal' "
        "ORDER BY d.language = 'EN' DESC, d.document_id, c.page_no, c.chunk_no LIMIT 60",
        (publication_id,),
    ).fetchall()
    out: list[dict] = []
    used = 0
    for row in rows:
        if used + len(row["text"]) > budget:
            break
        out.append(row)
        used += len(row["text"])
    return out


def reader_material(conn: psycopg.Connection, publication_id: int) -> dict[str, Any]:
    pub = store.get_publication(conn, publication_id)
    if pub is None:
        raise ValueError(f"publication {publication_id} not in the snapshot")
    chunks = _opening_chunks(conn, publication_id, OPENING_CHARS)
    seen = {c["id"] for c in chunks}
    vector_row = conn.execute(
        "SELECT embedding FROM publications WHERE id = %s", (publication_id,)
    ).fetchone()
    if vector_row and vector_row["embedding"] is not None:
        for hit in store.similar_chunks(
            conn, publication_id, store.to_array(vector_row["embedding"]), limit=SIMILAR_CHUNKS
        ):
            if hit["id"] not in seen:
                chunks.append(hit)
                seen.add(hit["id"])
    return {"publication": pub, "chunks": chunks}


def build_prompt(pub: dict, chunks: list[dict]) -> str:
    parts = [
        f"Publication: {pub.get('title') or ''}",
        f"Initiative: {pub.get('short_title') or ''}",
        f"Type: {pub.get('type') or ''}. Reference: {pub.get('reference') or ''}.",
    ]
    if pub.get("dossier_summary"):
        parts.append(f"Dossier summary from the portal:\n{pub['dossier_summary']}")
    if chunks:
        parts.append("Passages of the Commission document (document id, page, text):")
        for c in chunks:
            parts.append(f"[{c['document_id']} p.{c['page_no']}] {c['text']}")
    else:
        parts.append("No Commission document is attached; work from the title and summary.")
    parts.append(
        "Return JSON: one_paragraph (3 to 5 sentences on what is proposed), key_points (at most "
        "6), affected_groups (as named in the text), what_is_asked (what respondents should "
        "comment on)."
    )
    return "\n\n".join(parts)


def summarise_publication(
    conn: psycopg.Connection, provider: Provider, publication_id: int, *, force: bool = False
) -> tuple[dict, bool]:
    """Cached summary, or one model call. Returns (row, cache_hit)."""
    if not force:
        cached = get_summary(conn, publication_id)
        if cached is not None:
            return cached, True
    material = reader_material(conn, publication_id)
    pub, chunks = material["publication"], material["chunks"]
    result = provider.complete_json(
        build_prompt(pub, chunks),
        PublicationSummary,
        purpose="reader_summary",
        system=SYSTEM,
        context={"title": pub.get("title"), "chunks": [c["text"] for c in chunks]},
        cache_key=f"publication:{publication_id}",
    )
    summary = result.data.model_dump()
    summary["key_points"] = summary["key_points"][:6]
    conn.execute(
        "INSERT INTO publication_summaries (publication_id, provider, model, summary, "
        "source_chunks) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (publication_id) DO UPDATE SET provider = "
        "EXCLUDED.provider, model = EXCLUDED.model, summary = EXCLUDED.summary, "
        "source_chunks = EXCLUDED.source_chunks, created_at = now()",
        (publication_id, result.provider, result.model, Jsonb(summary), len(chunks)),
    )
    conn.commit()
    return get_summary(conn, publication_id), False
