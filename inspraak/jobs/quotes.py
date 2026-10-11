"""Verbatim quote verification: a quote is kept only when it is found in the source text
by string match. Whitespace runs, typographic quotes and long dashes (the model output
passes the house dash filter before it gets here, the stored source does not) are
normalised on both sides; nothing else. No fuzzy matching: a quote that cannot be found
is dropped."""

from __future__ import annotations

import re

from inspraak.textutil import plain_dashes

_WS = re.compile(r"\s+")
_QUOTES = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "«": '"',
        "»": '"',
        "…": "...",
        " ": " ",
    }
)
MIN_QUOTE_CHARS = 15


def normalise(text: str) -> str:
    return _WS.sub(" ", plain_dashes(text.translate(_QUOTES))).strip()


def quote_found(quote: str, source: str) -> bool:
    q = normalise(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return False
    s = normalise(source)
    return q in s or q.lower() in s.lower()


def verify_quotes(asks: list[dict], source: str) -> tuple[list[dict], int]:
    """Keep the asks whose quote is found, mark them verified. Returns (kept, dropped)."""
    kept: list[dict] = []
    dropped = 0
    for ask in asks:
        quote = (ask.get("quote") or "").strip()
        if quote and quote_found(quote, source):
            kept.append({"ask": ask.get("ask", ""), "quote": quote, "verified": True})
        else:
            dropped += 1
    return kept, dropped
