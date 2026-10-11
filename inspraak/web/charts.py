"""Inline SVG bar charts. No script, so they render in the static export and in print.
One hue, the value printed next to every bar, labels in plain text."""

from __future__ import annotations

from html import escape
from typing import Any

ROW_H = 22
LABEL_W = 250
VALUE_W = 48
BAR_W = 300
PAD = 4
LABEL_CHARS = 38


def bar_chart(
    rows: list[tuple[str, int | float]],
    *,
    title: str | None = None,
    color: str = "var(--bar)",
    max_rows: int = 12,
) -> str:
    """Horizontal bars, longest first as given. Returns an <svg> string (already escaped)."""
    rows = [(str(label), float(value)) for label, value in rows if value is not None][:max_rows]
    if not rows:
        return '<p class="muted">No rows.</p>'
    top = max(v for _, v in rows) or 1.0
    width = LABEL_W + BAR_W + VALUE_W + 2 * PAD
    height = ROW_H * len(rows) + 2 * PAD
    parts: list[str] = [
        f'<svg class="bars" viewBox="0 0 {width} {height}" width="100%" '
        f'preserveAspectRatio="xMinYMin meet" role="img" '
        f'aria-label="{escape(title or "bar chart")}">'
    ]
    if title:
        parts.append(f"<title>{escape(title)}</title>")
    for i, (label, value) in enumerate(rows):
        y = PAD + i * ROW_H
        w = max(1.0, BAR_W * value / top)
        shown = f"{int(value):,}" if float(value).is_integer() else f"{value:.2f}"
        parts.append(
            f'<text x="{LABEL_W - 6}" y="{y + 15}" text-anchor="end" class="bar-label">'
            f"{escape(_shorten(label, LABEL_CHARS))}</text>"
            f'<rect x="{LABEL_W}" y="{y + 3}" width="{w:.1f}" height="{ROW_H - 7}" '
            f'fill="{color}" rx="1"></rect>'
            f'<text x="{LABEL_W + w + 6:.1f}" y="{y + 15}" class="bar-value">{shown}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "."


def counts_to_rows(rows: list[dict[str, Any]], label: str, value: str) -> list[tuple[str, int]]:
    return [(str(r.get(label) or "unknown"), int(r.get(value) or 0)) for r in rows]
