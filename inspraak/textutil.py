"""Text cleaning and page-bound chunking."""

from __future__ import annotations

import re
from datetime import datetime

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def clean_text(value: str | None) -> str | None:
    """Strip control characters (Postgres refuses NUL), normalise line ends and spaces."""
    if value is None:
        return None
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES.sub("\n\n", text).strip()
    return text or None


def parse_hys_datetime(value: str | None) -> datetime | None:
    """Have Your Say uses 'YYYY/MM/DD HH:MM:SS' in the listing and ISO in attachments."""
    if not value:
        return None
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _split_long(piece: str, max_chars: int) -> list[str]:
    """Split one oversized paragraph on sentence ends, then hard on spaces."""
    parts: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?;:])\s+", piece):
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            parts_head = sentence[:cut].strip()
            if current:
                parts.append(current)
                current = ""
            parts.append(parts_head)
            sentence = sentence[cut:].strip()
        if len(current) + len(sentence) + 1 > max_chars and current:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def chunk_page(text: str, max_chars: int = 900, min_chars: int = 40) -> list[str]:
    """Split the text of one page into chunks of at most `max_chars`.

    Paragraphs are packed greedily; nothing crosses the page, so the page number of a
    chunk is exact. Fragments under `min_chars` (page numbers, lone headers) are dropped
    unless they are the only content of the page.
    """
    cleaned = clean_text(text)
    if not cleaned:
        return []
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", cleaned):
        paragraph = " ".join(paragraph.split())
        if not paragraph:
            continue
        if len(paragraph) > max_chars:
            pieces.extend(_split_long(paragraph, max_chars))
        else:
            pieces.append(paragraph)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) + 1 > max_chars:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n{piece}".strip()
    if current:
        chunks.append(current)
    kept = [c for c in chunks if len(c) >= min_chars]
    return kept or chunks[:1]


_DASHES = re.compile("[" + "".join(chr(c) for c in (0x2012, 0x2013, 0x2014, 0x2015, 0x2212)) + "]")


def plain_dashes(value: str) -> str:
    """Replace long dashes and the Unicode minus by an ASCII hyphen. For text the system
    writes itself (pages, drafts, exports); stored source text stays verbatim so quotes
    can be verified by string match."""
    return _DASHES.sub("-", value)


# The drafter is shown passages as "[chunk:<id> p.<n>]" and sometimes copies those
# markers into its text ("... 2024/1689 [chunk:2770]."). They are retrieval ids, never
# content: they are cut from every draft before it is stored or shown.
_CHUNK_GROUP = re.compile(
    r"\s*[\[(]\s*chunks?\s*:\s*\d+(?:\s*p\.\s*\d+)?"
    r"(?:\s*[,;]\s*(?:chunks?\s*:\s*)?\d+(?:\s*p\.\s*\d+)?)*\s*[\])]",
    re.IGNORECASE,
)
_CHUNK_BARE = re.compile(r"\s*\bchunks?\s*:\s*\d+\b", re.IGNORECASE)
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([.,;:!?])")


def strip_chunk_markers(value: str | None) -> str:
    """Remove "[chunk:12]", "[chunk:12, chunk:13]", "(chunk:12 p.4)" and bare "chunk:12"."""
    if not value:
        return value or ""
    text = _CHUNK_GROUP.sub("", value)
    text = _CHUNK_BARE.sub("", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()
