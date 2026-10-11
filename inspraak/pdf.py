"""Document bytes to text per page (PyMuPDF)."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

import pymupdf

from inspraak.textutil import clean_text


class UnparseableDocument(RuntimeError):
    pass


@dataclass(frozen=True)
class PageText:
    page_no: int  # 1-based, as a reader would cite it
    text: str


def sniff_filetype(data: bytes) -> str | None:
    """File type from magic bytes. The download endpoint mostly serves a PDF rendition,
    whatever the original file name says."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        # A zip container is only a document when it holds a Word body. The Commission
        # also publishes zip exports of consultation replies; those are not read or kept.
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return "docx" if "word/document.xml" in archive.namelist() else None
        except zipfile.BadZipFile:
            return None
    return None


def extract_pages(data: bytes) -> list[PageText]:
    """Text of every page. Pages without a text layer come back as empty strings, so a
    scanned PDF yields pages with no text; the caller decides what that means."""
    filetype = sniff_filetype(data)
    if filetype is None:
        raise UnparseableDocument("unknown file type (not a PDF, not a Word document)")
    try:
        with pymupdf.open(stream=data, filetype=filetype) as doc:
            return [
                PageText(page_no=i + 1, text=clean_text(page.get_text("text")) or "")
                for i, page in enumerate(doc)
            ]
    except UnparseableDocument:
        raise
    except Exception as exc:  # PyMuPDF raises several unrelated error types
        raise UnparseableDocument(f"{type(exc).__name__}: {exc}") from exc
