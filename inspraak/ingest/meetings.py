"""Commission meetings with interest representatives (XLSX, mandate 2024 to 2029).

Traps handled here:
- Row 1 is 'File generated on : YYYY-MM-DD', the header is row 2.
- One row can hold several organisations: names and register IDs are comma separated
  in two cells. Names can contain commas themselves ('Atlantic Council of the United
  States, Inc'), so only the IDs are split. A name is attached to an ID only when both
  lists have the same length; otherwise the name comes from the register join.
- The file is regenerated in full, so the table is replaced in one transaction. The row
  id is a hash of the row content, stable across reloads. Identical rows do occur (the
  portal counts each one), so the second and later copies get a "-2", "-3" suffix.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl
import psycopg

from inspraak.config import get_settings
from inspraak.http import Fetcher
from inspraak.textutil import clean_text

EXPECTED_HEADER = (
    "Name of cabinet",
    "Name of EC representative",
    "Title of EC representative",
    "Date of meeting",
    "Location",
    "Name of interest representative",
    "Transparency register ID",
    "Subject of the meeting",
)
TR_ID = re.compile(r"^\d{6,15}-\d{2}$")


class MeetingsFormatError(RuntimeError):
    pass


def split_tr_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in str(value).split(",") if TR_ID.match(p.strip())]


def split_names(value: str | None, n_ids: int) -> list[str | None]:
    """Names aligned with the IDs, or all None when the split is ambiguous."""
    if not value:
        return [None] * n_ids
    if n_ids == 1:
        return [value.strip()]
    parts = [p.strip() for p in value.split(",")]
    if len(parts) == n_ids:
        return parts
    return [None] * n_ids


def _to_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def parse_meetings(path: Path) -> tuple[date | None, list[dict[str, Any]]]:
    workbook = openpyxl.load_workbook(path, read_only=True)
    sheet = workbook.worksheets[0]
    file_date: date | None = None
    header_seen = False
    meetings: dict[str, dict[str, Any]] = {}
    for row in sheet.iter_rows(values_only=True):
        cells = [c.strip() if isinstance(c, str) else c for c in row[:8]]
        if not header_seen:
            first = cells[0] or ""
            if isinstance(first, str) and first.lower().startswith("file generated"):
                file_date = _to_date(first.split(":", 1)[-1])
                continue
            if tuple(cells) == EXPECTED_HEADER:
                header_seen = True
                continue
            if any(cells):
                raise MeetingsFormatError(f"unexpected row before header: {cells}")
            continue
        if not any(cells):
            continue
        cabinet, rep, title, when, location, names, ids, subject = cells
        key = hashlib.sha1(
            "\x1f".join("" if c is None else str(c) for c in cells).encode("utf-8")
        ).hexdigest()[:20]
        base, copy = key, 1
        while key in meetings:
            copy += 1
            key = f"{base}-{copy}"
        tr_ids = split_tr_ids(ids)
        meetings[key] = {
            "id": key,
            "cabinet": cabinet,
            "ec_representative": rep,
            "ec_title": title,
            "meeting_date": _to_date(when),
            "location": location,
            "org_names_raw": names,
            "tr_ids_raw": str(ids) if ids is not None else None,
            "subject": clean_text(subject) if isinstance(subject, str) else None,
            "file_date": file_date,
            "orgs": list(zip(tr_ids, split_names(names, len(tr_ids)), strict=True)),
        }
    workbook.close()
    if not header_seen:
        raise MeetingsFormatError("header row not found")
    return file_date, list(meetings.values())


def download_meetings(fetcher: Fetcher | None = None, dest: Path | None = None) -> Path:
    settings = get_settings()
    dest = dest or settings.raw_dir / "meetings" / "meetings2429.xlsx"
    dest.parent.mkdir(parents=True, exist_ok=True)
    fetcher = fetcher or Fetcher()
    response = fetcher.get(settings.meetings_url)
    if response.content[:2] != b"PK":
        raise MeetingsFormatError("download is not an XLSX file")
    dest.write_bytes(response.content)
    return dest


def load_meetings(conn: psycopg.Connection, path: Path) -> dict[str, Any]:
    file_date, rows = parse_meetings(path)
    if not rows:
        raise MeetingsFormatError("no meeting rows parsed")
    with conn.cursor() as cur:
        # Keep vectors of subjects that did not change.
        cur.execute("SELECT id, embedding FROM meetings WHERE embedding IS NOT NULL")
        kept = {r["id"]: r["embedding"] for r in cur.fetchall()}
        cur.execute("DELETE FROM meetings")
        cur.executemany(
            "INSERT INTO meetings (id, cabinet, ec_representative, ec_title, meeting_date, "
            "location, org_names_raw, tr_ids_raw, subject, file_date, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    m["id"], m["cabinet"], m["ec_representative"], m["ec_title"],
                    m["meeting_date"], m["location"], m["org_names_raw"], m["tr_ids_raw"],
                    m["subject"], m["file_date"], kept.get(m["id"]),
                )
                for m in rows
            ],
        )  # fmt: skip
        cur.executemany(
            "INSERT INTO meeting_orgs (meeting_id, position, tr_id, org_name) "
            "VALUES (%s, %s, %s, %s)",
            [(m["id"], i, tr_id, name) for m in rows for i, (tr_id, name) in enumerate(m["orgs"])],
        )
    conn.commit()
    dates = [m["meeting_date"] for m in rows if m["meeting_date"]]
    return {
        "file_date": file_date.isoformat() if file_date else None,
        "meetings": len(rows),
        "meeting_org_links": sum(len(m["orgs"]) for m in rows),
        "multi_org_meetings": sum(1 for m in rows if len(m["orgs"]) > 1),
        "meetings_without_id": sum(1 for m in rows if not m["orgs"]),
        "names_unaligned": sum(1 for m in rows if len(m["orgs"]) > 1 and m["orgs"][0][1] is None),
        "first_date": min(dates).isoformat() if dates else None,
        "last_date": max(dates).isoformat() if dates else None,
    }
