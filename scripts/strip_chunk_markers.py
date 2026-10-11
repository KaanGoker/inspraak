"""Cut "[chunk:<id>]" retrieval markers from stored drafts: the drafts table, the draft
texts kept in runs.result and in the draft_review payloads. Idempotent; prints counts.
Usage: uv run python scripts/strip_chunk_markers.py"""

import json
import re

from psycopg.types.json import Jsonb

from inspraak.db import connect
from inspraak.textutil import strip_chunk_markers

MARKER = re.compile(r"chunks?\s*:\s*\d", re.IGNORECASE)


def clean(value):
    """Strip markers from every string under a "text" or "machine_text" key."""
    if isinstance(value, dict):
        return {
            k: strip_chunk_markers(v)
            if k in ("text", "machine_text") and isinstance(v, str)
            else clean(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


with connect() as conn:
    counts = {"drafts": 0, "runs": 0, "review_items": 0}
    for row in conn.execute("SELECT id, text FROM drafts").fetchall():
        if MARKER.search(row["text"] or ""):
            conn.execute(
                "UPDATE drafts SET text = %s WHERE id = %s",
                (strip_chunk_markers(row["text"]), row["id"]),
            )
            counts["drafts"] += 1
    for table, column in (("runs", "result"), ("review_items", "payload")):
        for row in conn.execute(f"SELECT id, {column} AS v FROM {table}").fetchall():
            if row["v"] is None:
                continue
            new = clean(row["v"])
            if json.dumps(new, sort_keys=True) != json.dumps(row["v"], sort_keys=True):
                conn.execute(
                    f"UPDATE {table} SET {column} = %s WHERE id = %s", (Jsonb(new), row["id"])
                )
                counts[table] += 1
    conn.commit()
    print(counts)
