"""EU Transparency Register: streamed XML export to the organisations table.

Traps handled here:
- The file declares XML 1.1 and carries a handful of character references to control
  characters (&#x2; &#xb; &#x1d;) that libxml2 rejects. Parsing it with recover=True
  silently drops `&amp;` in thousands of later records ("Companies  groups"), so the
  bytes are sanitised on the way in (version 1.0, bad references replaced by a space)
  and the parser runs strict.
- The download takes minutes and a cut download still parses, so the parsed count is
  checked against metaData/numberOfIR and a mismatch aborts the load.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any, BinaryIO

import psycopg
from lxml import etree
from psycopg.types.json import Jsonb

from inspraak.config import get_settings
from inspraak.http import Fetcher
from inspraak.textutil import clean_text

RECORD_TAG = "interestRepresentative"

_CHAR_REF = re.compile(rb"&#(x[0-9A-Fa-f]{1,6}|[0-9]{1,7});")
_XML11 = re.compile(rb"""(<\?xml[^>]*version=["'])1\.1(["'])""")


class RegisterIncomplete(RuntimeError):
    pass


def _valid_xml10_char(code: int) -> bool:
    return (
        code in (0x9, 0xA, 0xD)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or (0x10000 <= code <= 0x10FFFF)
    )


def _fix_ref(match: re.Match[bytes]) -> bytes:
    body = match.group(1)
    code = int(body[1:], 16) if body[:1] == b"x" else int(body)
    return match.group(0) if _valid_xml10_char(code) else b" "


class SanitisedXML(io.RawIOBase):
    """Read-only byte stream over the export that makes it valid XML 1.0.

    Only character references are touched, so byte offsets inside text are not
    otherwise changed. A tail of 12 bytes is held back between reads so a reference
    split across two blocks is still seen whole.
    """

    TAIL = 12

    def __init__(self, raw: BinaryIO, block_size: int = 1 << 20) -> None:
        self._raw = raw
        self._block_size = block_size
        self._held = b""  # unprocessed tail from the previous block
        self._out = b""  # sanitised bytes not yet handed out
        self._eof = False
        self._header_done = False
        self.replaced = 0

    def readable(self) -> bool:
        return True

    def _count_fix(self, match: re.Match[bytes]) -> bytes:
        fixed = _fix_ref(match)
        if fixed != match.group(0):
            self.replaced += 1
        return fixed

    def _fill(self) -> None:
        block = self._raw.read(self._block_size)
        if not block:
            self._eof = True
            pending, self._held = self._held, b""
        else:
            data = self._held + block
            cut = max(len(data) - self.TAIL, 0)
            pending, self._held = data[:cut], data[cut:]
        if not self._header_done:
            pending = _XML11.sub(rb"\g<1>1.0\g<2>", pending, count=1)
            self._header_done = True
        self._out += _CHAR_REF.sub(self._count_fix, pending)

    def read(self, size: int = -1) -> bytes:
        while not self._eof and (size < 0 or len(self._out) < size):
            self._fill()
        if size < 0:
            out, self._out = self._out, b""
        else:
            out, self._out = self._out[:size], self._out[size:]
        return out

    def readinto(self, b) -> int:
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


def sanitise_bytes(data: bytes) -> bytes:
    """Whole-buffer version for small inputs and tests."""
    return _CHAR_REF.sub(_fix_ref, _XML11.sub(rb"\g<1>1.0\g<2>", data, count=1))


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(node: etree._Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    if found is None or found.text is None:
        return None
    return clean_text(found.text)


def _number(node: etree._Element | None, path: str) -> float | None:
    value = _text(node, path)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def element_to_obj(node: etree._Element) -> Any:
    """Generic XML to JSON: attributes, children (repeated tags become lists), text."""
    children = [c for c in node if isinstance(c.tag, str)]
    if not children:
        text = clean_text(node.text)
        if node.attrib and text is not None:
            return {**dict(node.attrib), "value": text}
        if node.attrib:
            return dict(node.attrib)
        return text
    out: dict[str, Any] = dict(node.attrib)
    for child in children:
        key = _local(child.tag)
        value = element_to_obj(child)
        if key in out:
            if not isinstance(out[key], list):
                out[key] = [out[key]]
            out[key].append(value)
        else:
            out[key] = value
    return out


def build_profile_text(org: dict[str, Any]) -> str:
    """The text that is embedded for matching: what the organisation says about itself."""
    parts = [org["name"]]
    if org.get("acronym"):
        parts.append(f"({org['acronym']})")
    lines = [" ".join(parts)]
    if org.get("category"):
        lines.append(org["category"])
    if org.get("interests"):
        lines.append("Interests: " + ", ".join(org["interests"]))
    if org.get("goals"):
        lines.append(org["goals"])
    if org.get("eu_legislative_proposals"):
        lines.append(org["eu_legislative_proposals"])
    return "\n".join(lines)


def parse_record(node: etree._Element, export_date: datetime | None = None) -> dict[str, Any]:
    """One interestRepresentative element to a row dict for `organisations`."""
    closed = node.find("financialData/closedYear")
    costs_min = _number(closed, "costs/range/min")
    costs_max = _number(closed, "costs/range/max")
    absolute_costs = _number(closed, "costs/absoluteCost")
    if absolute_costs is not None and costs_min is None and costs_max is None:
        costs_min = costs_max = absolute_costs
    financial = node.find("financialData")
    org: dict[str, Any] = {
        "tr_id": _text(node, "identificationCode"),
        "name": _text(node, "name/originalName"),
        "name_latin": _text(node, "name/nameInLatinAlphabet"),
        "acronym": _text(node, "acronym"),
        "entity_form": _text(node, "entityForm"),
        "website": _text(node, "webSiteURL"),
        "category": _text(node, "registrationCategory"),
        "registration_date": _datetime(_text(node, "registrationDate")),
        "last_update": _datetime(_text(node, "lastUpdateDate")),
        "head_country": _text(node, "headOffice/country"),
        "head_city": _text(node, "headOffice/city"),
        "head_postcode": _text(node, "headOffice/postCode"),
        "eu_country": _text(node, "EUOffice/country"),
        "eu_city": _text(node, "EUOffice/city"),
        "goals": _text(node, "goals"),
        "levels_of_interest": [
            t
            for t in (
                clean_text(e.text)
                for e in node.findall("levelsOfInterest/levelOfInterest/levelOfInterest")
            )
            if t
        ],
        "interests": [
            t for t in (clean_text(e.text) for e in node.findall("interests/interest/name")) if t
        ],
        "eu_legislative_proposals": _text(node, "EULegislativeProposals"),
        "eu_forums": _text(node, "EUSupportedForumsAndPlatforms"),
        "communication_activities": _text(node, "communicationActivities"),
        "groupings": _text(node, "interOrUnofficalGroupings"),
        "interest_represented": _text(node, "interestRepresented"),
        "is_member_of": _text(node, "structure/isMemberOf"),
        "organisation_members": _text(node, "structure/organisationMembers"),
        "members_count": _number(node, "members/members"),
        "members_fte": _number(node, "members/membersFTE"),
        "ep_accredited": int(_number(node, "EPAccreditedNumber") or 0),
        "fin_year_start": _date(_text(closed, "startDate")),
        "fin_year_end": _date(_text(closed, "endDate")),
        "costs_min": costs_min,
        "costs_max": costs_max,
        "total_budget": _number(closed, "totalBudget/absoluteCost"),
        "financial": element_to_obj(financial) if financial is not None else None,
        "export_date": export_date,
    }
    org["profile_text"] = build_profile_text(org)
    return org


def iter_register(source: str | Path | BinaryIO) -> Iterator[tuple[str, Any]]:
    """Yield ('meta', {...}) once, then ('org', row) per registrant, then ('done', stats)."""
    export_date: datetime | None = None
    if isinstance(source, str | Path):
        raw: BinaryIO = open(source, "rb")  # noqa: SIM115 - closed in finally
        own = True
    else:
        raw, own = source, False
    stream = SanitisedXML(raw)
    try:
        yield from _iter_nodes(stream, export_date)
    finally:
        if own:
            raw.close()
    yield "done", {"char_refs_replaced": stream.replaced}


def _iter_nodes(stream: SanitisedXML, export_date: datetime | None) -> Iterator[tuple[str, Any]]:
    context = etree.iterparse(
        stream,
        events=("end",),
        huge_tree=True,
        remove_comments=True,
    )
    for _, node in context:
        tag = _local(node.tag)
        if tag == "metaData":
            export_date = _datetime(_text(node, "exportDate"))
            declared = _number(node, "numberOfIR")
            yield (
                "meta",
                {
                    "export_date": export_date,
                    "number_of_ir": int(declared) if declared is not None else None,
                },
            )
        elif tag == RECORD_TAG:
            row = parse_record(node, export_date)
            if row["tr_id"] and row["name"]:
                yield "org", row
            else:
                yield "skipped", row
            node.clear()
            parent = node.getparent()
            if parent is not None:
                while node.getprevious() is not None:
                    del parent[0]


COLUMNS = [
    "tr_id", "name", "name_latin", "acronym", "entity_form", "website", "category",
    "registration_date", "last_update", "head_country", "head_city", "head_postcode",
    "eu_country", "eu_city", "goals", "levels_of_interest", "interests",
    "eu_legislative_proposals", "eu_forums", "communication_activities", "groupings",
    "interest_represented", "is_member_of", "organisation_members", "members_count",
    "members_fte", "ep_accredited", "fin_year_start", "fin_year_end", "costs_min",
    "costs_max", "total_budget", "financial", "profile_text", "export_date",
]  # fmt: skip

_UPSERT = (
    f"INSERT INTO organisations ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join('%s' for _ in COLUMNS)}) "
    "ON CONFLICT (tr_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c != "tr_id")
    # A changed profile text invalidates the stored vector.
    + ", profile_embedding = CASE WHEN organisations.profile_text IS DISTINCT FROM "
    "EXCLUDED.profile_text THEN NULL ELSE organisations.profile_embedding END"
)


def _params(row: dict[str, Any]) -> list[Any]:
    values = []
    for column in COLUMNS:
        value = row[column]
        if column == "financial" and value is not None:
            value = Jsonb(value)
        values.append(value)
    return values


def download_register(fetcher: Fetcher | None = None, dest: Path | None = None) -> Path:
    """Stream the export to disk. Written to a .part file first, renamed when complete."""
    settings = get_settings()
    dest = dest or settings.raw_dir / "register" / "register.xml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".xml.part")
    fetcher = fetcher or Fetcher()
    with fetcher.client.stream("GET", settings.register_url, timeout=900.0) as response:
        response.raise_for_status()
        with part.open("wb") as handle:
            for block in response.iter_bytes(1 << 20):
                handle.write(block)
    part.replace(dest)
    return dest


def load_register(conn: psycopg.Connection, path: Path, *, prune: bool = True) -> dict[str, Any]:
    """Parse the file fully, verify the count, then upsert in one transaction.

    Registrants that left the register are deleted when `prune` is set, so the table
    mirrors the export.
    """
    declared: int | None = None
    export_date: datetime | None = None
    rows: list[dict[str, Any]] = []
    skipped = 0
    char_refs_replaced = 0
    for kind, payload in iter_register(path):
        if kind == "meta":
            declared = payload["number_of_ir"]
            export_date = payload["export_date"]
        elif kind == "org":
            rows.append(payload)
        elif kind == "done":
            char_refs_replaced = payload["char_refs_replaced"]
        else:
            skipped += 1
    parsed = len(rows) + skipped
    if declared is None:
        raise RegisterIncomplete("metaData/numberOfIR not found")
    if parsed != declared:
        raise RegisterIncomplete(
            f"parsed {parsed} registrants, the file declares {declared}: download is incomplete"
        )
    unique = {r["tr_id"]: r for r in rows}
    with conn.cursor() as cur:
        cur.executemany(_UPSERT, [_params(r) for r in unique.values()])
        removed = 0
        if prune:
            cur.execute("DELETE FROM organisations WHERE NOT (tr_id = ANY(%s))", (list(unique),))
            removed = cur.rowcount
    conn.commit()
    return {
        "declared": declared,
        "parsed": parsed,
        "stored": len(unique),
        "skipped_no_id_or_name": skipped,
        "duplicate_ids": len(rows) - len(unique),
        "removed": removed,
        "char_refs_replaced": char_refs_replaced,
        "export_date": export_date.isoformat() if export_date else None,
        "file_bytes": path.stat().st_size,
    }
