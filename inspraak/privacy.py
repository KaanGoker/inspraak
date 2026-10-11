"""Privacy guard for Have Your Say feedback.

Rules (SPEC, Principles):
- firstName, surname and login are dropped from every row, organisation rows included.
- Citizen rows keep only what is needed to count them: id, type, country, language, date.
  Their text, translated text, organisation field, register number and attachment
  metadata (file names can carry a name) never reach the disk cache or the database.
- Fail closed: a row whose userType is missing or unknown, or a non-citizen row with no
  organisation name, is treated as a citizen row.

`scrub_feedback_row` runs before the raw page is written to data/raw and again before
the insert. The feedback table has no name columns and a CHECK constraint on citizen rows.
"""

from __future__ import annotations

import re
from typing import Any

CITIZEN_TYPES = frozenset({"EU_CITIZEN", "NON_EU_CITIZEN"})

# Stakeholder types seen in the feedback payload. The Commission's own spelling
# (ACADEMIC_RESEARCH_INSTITTUTION) is kept as is.
ORGANISATION_TYPES = frozenset(
    {
        "NGO",
        "COMPANY",
        "BUSINESS_ASSOCIATION",
        "ACADEMIC_RESEARCH_INSTITTUTION",
        "ACADEMIC_RESEARCH_INSTITUTION",
        "TRADE_UNION",
        "CONSUMER_ORGANISATION",
        "ENVIRONMENTAL_ORGANISATION",
        "PUBLIC_AUTHORITY",
        "OTHER",
    }
)

ALWAYS_DROPPED = ("firstName", "surname", "login")
CITIZEN_DROPPED = (
    "feedback",
    "feedbackTextUserLanguage",
    "organization",
    "trNumber",
    "tr_number",
    "attachments",
    "companySize",
    "scope",
    "governanceLevel",
)
PERSONAL_KEYS = frozenset(ALWAYS_DROPPED)

# Commission staff contact fields on a publication: not needed, not stored.
PUBLICATION_DROPPED = ("authorName", "authorSurname", "authorMail", "authorPhoneNumber")


class PrivacyError(RuntimeError):
    """Raised when a row that should have been scrubbed still carries personal data."""


def is_citizen_row(row: dict[str, Any]) -> bool:
    """True when the row must be reduced to a count-only record."""
    user_type = row.get("userType")
    if user_type in CITIZEN_TYPES:
        return True
    if user_type not in ORGANISATION_TYPES:
        return True
    return not (row.get("organization") or "").strip()


def scrub_feedback_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return a copy that is safe to cache and store."""
    citizen = is_citizen_row(row)
    out = {k: v for k, v in row.items() if k not in ALWAYS_DROPPED}
    if citizen:
        attachment_count = len(row.get("attachments") or [])
        for key in CITIZEN_DROPPED:
            out.pop(key, None)
        out["attachmentCount"] = attachment_count
    out["isCitizen"] = citizen
    return out


def scrub_feedback_page(payload: dict[str, Any]) -> dict[str, Any]:
    """Scrub one allFeedback response, keeping only what the ingest needs."""
    return {
        "totalElements": payload.get("totalElements"),
        "totalPages": payload.get("totalPages"),
        "number": payload.get("number"),
        "content": [scrub_feedback_row(r) for r in payload.get("content") or []],
    }


def scrub_initiative_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Drop Commission staff contact fields from a groupInitiatives response."""
    out = dict(detail)
    pubs = []
    for pub in detail.get("publications") or []:
        pubs.append({k: v for k, v in pub.items() if k not in PUBLICATION_DROPPED})
    out["publications"] = pubs
    return out


def assert_safe(row: dict[str, Any]) -> None:
    """Refuse a row that still holds personal keys or citizen content."""
    leaked = PERSONAL_KEYS.intersection(row)
    if leaked:
        raise PrivacyError(f"personal keys present: {sorted(leaked)}")
    if "isCitizen" not in row:
        raise PrivacyError("row was not scrubbed")
    if row["isCitizen"]:
        content = [k for k in CITIZEN_DROPPED if row.get(k)]
        if content:
            raise PrivacyError(f"citizen row carries content: {content}")


# --------------------------------------------------------------------------- display

# A digit run this long in an organisation name is a tax, VAT or company number, which a
# one-person firm's number can tie to a person. It is masked on every page.
_LONG_DIGITS = re.compile(r"\d(?:[\s.\-/]?\d){7,}")


def mask_long_digits(name: str | None) -> str:
    """Replace runs of 8 or more digits (spaces, dots, dashes and slashes allowed between
    them) with a placeholder."""
    if not name:
        return name or ""
    return _LONG_DIGITS.sub("[number removed]", name)


def is_unregistered_micro(tr_number: str | None, company_size: str | None) -> bool:
    """A submitter with no register number that declared itself a micro company: often a
    sole trader whose trading name is a person's name."""
    return not (tr_number or "").strip() and (company_size or "").upper() == "MICRO"


def display_submitter(
    organization: str | None,
    *,
    tr_number: str | None,
    company_size: str | None,
    country: str | None,
    register_name: str | None = None,
) -> str:
    """The name a page shows for an organisation submission. Unregistered micro companies
    are shown as "Micro company, <country>"; long digit runs are masked everywhere."""
    if is_unregistered_micro(tr_number, company_size):
        return f"Micro company, {country}" if country else "Micro company"
    return mask_long_digits(organization or register_name or "(unnamed)")
