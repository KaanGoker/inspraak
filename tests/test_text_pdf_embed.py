from __future__ import annotations

from datetime import datetime

import httpx
import numpy as np
import pymupdf
import pytest

from inspraak.embed import HashEmbedder
from inspraak.http import Fetcher, FetchError
from inspraak.pdf import UnparseableDocument, extract_pages, sniff_filetype
from inspraak.textutil import chunk_page, clean_text, parse_hys_datetime

# --------------------------------------------------------------------------- textutil


def test_clean_text_strips_nul_and_normalises():
    assert clean_text("a\x00b\r\nc  \t d\n\n\n\ne") == "ab\nc d\n\ne"
    assert clean_text("   ") is None
    assert clean_text(None) is None


def test_parse_hys_datetime_formats():
    assert parse_hys_datetime("2026/11/26 23:59:59") == datetime(2026, 11, 26, 23, 59, 59)
    assert parse_hys_datetime("2026-10-01T12:55:29.79") == datetime(2026, 10, 1, 12, 55, 29, 790000)
    assert parse_hys_datetime("") is None and parse_hys_datetime("yesterday") is None


def test_chunk_page_respects_max_and_drops_fragments():
    paragraph = "This is a sentence about consultations. " * 60  # about 2400 chars
    chunks = chunk_page(paragraph + "\n\n17\n\nAnother paragraph here, short but kept.", 900)
    assert all(len(c) <= 900 for c in chunks)
    assert "17" not in chunks  # page number fragment dropped
    assert any("Another paragraph" in c for c in chunks)
    assert chunk_page("", 900) == []
    assert chunk_page("7", 900) == ["7"]  # only content of the page is kept


# --------------------------------------------------------------------------- pdf


def _pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=11)
    return doc.tobytes()


def test_extract_pages_keeps_page_numbers():
    data = _pdf(["Article 1\nScope of this regulation.", "", "Article 2\nDefinitions."])
    assert sniff_filetype(data) == "pdf"
    pages = extract_pages(data)
    assert [p.page_no for p in pages] == [1, 2, 3]
    assert "Scope of this regulation" in pages[0].text
    assert pages[1].text == ""
    assert pages[2].text.startswith("Article 2")


def test_extract_pages_refuses_unknown_bytes():
    assert sniff_filetype(b"<html>") is None
    with pytest.raises(UnparseableDocument):
        extract_pages(b"<html>not a document</html>")


# --------------------------------------------------------------------------- embed


def test_hash_embedder_is_normalised_and_meaningful():
    emb = HashEmbedder(dim=64)
    vectors = emb.embed(["child safety online", "online safety of children", "fisheries quota"])
    assert vectors.shape == (3, 64)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)
    close = float(vectors[0] @ vectors[1])
    far = float(vectors[0] @ vectors[2])
    assert close > far
    assert emb.embed([]).shape == (0, 64)


# --------------------------------------------------------------------------- http


def test_fetcher_retries_then_succeeds_and_checks_content_type():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503)
        if request.url.path.endswith("/html"):
            return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})
        return httpx.Response(200, json={"ok": True})

    sleeps: list[float] = []
    fetcher = Fetcher(
        interval=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )
    assert fetcher.get_json("https://x.test/json") == {"ok": True}
    assert attempts["n"] == 2 and sleeps == [2]
    with pytest.raises(FetchError, match="not JSON"):
        fetcher.get_json("https://x.test/html")


def test_fetcher_gives_up_on_404_without_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    fetcher = Fetcher(
        interval=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
    )
    with pytest.raises(FetchError, match="HTTP 404"):
        fetcher.get("https://x.test/missing")
    assert calls["n"] == 1


# --------------------------------------------------------------------------- documents


def _zip(names: list[str]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "x")
    return buffer.getvalue()


def test_sniff_tells_a_word_file_from_a_reply_archive():
    assert sniff_filetype(_zip(["[Content_Types].xml", "word/document.xml"])) == "docx"
    archive = _zip(["contributions.csv", "attachments/a.pdf"])
    assert sniff_filetype(archive) is None
    with pytest.raises(UnparseableDocument):
        extract_pages(archive)
    assert sniff_filetype(b"PK\x03\x04 broken") is None


def _document_fetcher(payloads: dict[str, bytes]):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        calls.append(name)
        return httpx.Response(
            200, content=payloads[name], headers={"content-type": "application/pdf"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Fetcher(interval=0, client=client), calls


def test_download_document_caches_documents_but_never_archives(tmp_path):
    from inspraak.ingest.cache import RawCache
    from inspraak.ingest.documents import download_document

    pdf = _pdf(["Article 1"])
    fetcher, calls = _document_fetcher(
        {"doc1": pdf, "doc2": pdf, "zip1": _zip(["contributions.csv"])}
    )
    cache = RawCache(root=tmp_path)

    assert download_document(fetcher, cache, "doc1") == pdf
    assert download_document(fetcher, cache, "doc1") == pdf  # second read from disk
    assert calls == ["doc1"]
    assert (tmp_path / "documents" / "doc1.bin").exists()

    download_document(fetcher, cache, "doc2", keep=False)  # feedback attachment default
    assert not (tmp_path / "documents" / "doc2.bin").exists()

    download_document(fetcher, cache, "zip1")
    assert not (tmp_path / "documents" / "zip1.bin").exists()


def test_download_document_refuses_the_html_shell(tmp_path):
    from inspraak.ingest.cache import RawCache
    from inspraak.ingest.documents import download_document

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>shell</html>", headers={"content-type": "text/html"})

    fetcher = Fetcher(interval=0, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(FetchError, match="HTML"):
        download_document(fetcher, RawCache(root=tmp_path), "gone")
    assert not (tmp_path / "documents").exists()


def test_windows_cover_a_long_text_in_order():
    from inspraak.embed import MAX_WINDOWS, WINDOW_CHARS, windows

    assert windows("short  text\nhere") == ["short text here"]
    words = [f"w{i}" for i in range(400)]  # about 1,900 characters
    parts = windows(" ".join(words))
    assert all(len(p) <= WINDOW_CHARS for p in parts)
    assert " ".join(parts).split() == words  # nothing lost, nothing repeated
    capped = windows("x" * (WINDOW_CHARS * (MAX_WINDOWS + 3)))
    assert len(capped) == MAX_WINDOWS and all(len(p) == WINDOW_CHARS for p in capped)
