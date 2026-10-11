"""Documents: download by documentId, text per page, chunks.

GET api/download/{documentId} serves a PDF rendition for most files, whatever the
original name says (a .docx submission comes back as PDF). The file type is sniffed
from the bytes, never from the name. Commission documents are cached forever under
data/raw/hys/documents/{documentId}.bin because a documentId is immutable. Feedback
attachments (about 1.1 GB for the V1 scope) are not kept on disk unless
KEEP_ATTACHMENT_FILES=1: their page text lives in Postgres and parse_status makes the
run resumable.

parse_status values: pending, ok, empty (no text layer, probably scanned), error.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import psycopg

from inspraak.config import get_settings
from inspraak.http import Fetcher, FetchError
from inspraak.ingest.cache import RawCache
from inspraak.pdf import UnparseableDocument, extract_pages, sniff_filetype
from inspraak.textutil import chunk_page

Log = Callable[[str], None]


def _noop(_: str) -> None:
    pass


def download_document(
    fetcher: Fetcher, cache: RawCache, document_id: str, *, keep: bool = True
) -> bytes:
    data = cache.get_bytes("documents", f"{document_id}.bin")
    if data is None:
        settings = get_settings()
        response = fetcher.get(f"{settings.hys_base}/api/download/{document_id}")
        data = response.content
        if "html" in response.headers.get("content-type", "") and data[:5] != b"%PDF-":
            raise FetchError(f"download/{document_id} returned an HTML page")
        if keep and sniff_filetype(data) is not None:
            cache.put_bytes(data, "documents", f"{document_id}.bin")
    return data


def store_pages(conn: psycopg.Connection, document_id: str, data: bytes) -> dict[str, Any]:
    """Extract, chunk and store one document. Returns the stats written to `documents`."""
    pages = extract_pages(data)
    chunks = [
        (document_id, page.page_no, i, text)
        for page in pages
        for i, text in enumerate(chunk_page(page.text))
    ]
    chars = sum(len(p.text) for p in pages)
    status = "ok" if chars > 0 else "empty"
    with conn.cursor() as cur:
        cur.execute("DELETE FROM document_pages WHERE document_id = %s", (document_id,))
        cur.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
        cur.executemany(
            "INSERT INTO document_pages (document_id, page_no, text) VALUES (%s, %s, %s)",
            [(document_id, p.page_no, p.text) for p in pages if p.text],
        )
        cur.executemany(
            "INSERT INTO chunks (document_id, page_no, chunk_no, text) VALUES (%s, %s, %s, %s)",
            chunks,
        )
        cur.execute(
            "UPDATE documents SET fetched_at = now(), size_bytes = %s, sha256 = %s, "
            "parse_status = %s, parse_error = NULL, pages_extracted = %s, chars_extracted = %s "
            "WHERE document_id = %s",
            (len(data), hashlib.sha256(data).hexdigest(), status, len(pages), chars, document_id),
        )
    conn.commit()
    return {"status": status, "pages": len(pages), "chars": chars, "chunks": len(chunks)}


def _fail(conn: psycopg.Connection, document_id: str, error: str) -> None:
    conn.execute(
        "UPDATE documents SET fetched_at = now(), parse_status = 'error', parse_error = %s "
        "WHERE document_id = %s",
        (error[:500], document_id),
    )
    conn.commit()


def pending_documents(
    conn: psycopg.Connection, kind: str, *, in_scope_only: bool = True, limit: int | None = None
) -> list[dict[str, Any]]:
    sql = (
        "SELECT d.document_id, d.kind, d.publication_id, d.filename, d.pages_declared, "
        "d.size_bytes FROM documents d JOIN publications p ON p.id = d.publication_id "
        "WHERE d.kind = %s AND d.parse_status = 'pending'"
    )
    if in_scope_only:
        sql += " AND p.in_scope"
    sql += " ORDER BY p.end_date DESC NULLS LAST, d.document_id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, (kind,)).fetchall()


def run_documents(
    conn: psycopg.Connection,
    kind: str,
    fetcher: Fetcher | None = None,
    cache: RawCache | None = None,
    *,
    limit: int | None = None,
    max_bytes: int = 40_000_000,
    log: Log = _noop,
) -> dict[str, Any]:
    """Download and parse every pending document of one kind for in-scope publications."""
    fetcher = fetcher or Fetcher()
    cache = cache or RawCache()
    keep = kind == "proposal" or get_settings().keep_attachment_files
    todo = pending_documents(conn, kind, limit=limit)
    stats = {"kind": kind, "pending": len(todo), "ok": 0, "empty": 0, "error": 0, "skipped_size": 0,
             "pages": 0, "chunks": 0}  # fmt: skip
    for row in todo:
        document_id = row["document_id"]
        if (row["size_bytes"] or 0) > max_bytes:
            _fail(conn, document_id, f"skipped: declared size {row['size_bytes']} over limit")
            stats["skipped_size"] += 1
            continue
        try:
            data = download_document(fetcher, cache, document_id, keep=keep)
            result = store_pages(conn, document_id, data)
        except (FetchError, UnparseableDocument) as exc:
            _fail(conn, document_id, str(exc))
            stats["error"] += 1
            log(f"{document_id} error: {exc}")
            continue
        stats[result["status"]] += 1
        stats["pages"] += result["pages"]
        stats["chunks"] += result["chunks"]
        log(f"{document_id} {row['filename']}: {result}")
    stats["requests"] = fetcher.requests
    return stats
