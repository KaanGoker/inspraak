"""Links to the public source of every number and quote.

Checked on 3 October 2026: the Have Your Say portal is a single-page app and answers 200
for `initiatives/{id}_en` and `initiatives/{id}/F{feedbackId}_en` (the paths the portal
itself uses; a slug between the id and the suffix is optional). The register's organisation
page is `organisation-detail_en?id=...` (200, titled "organisation detail"). The meetings
portal has no per-row or per-organisation deep link (`meeting.do`, `search.do` and
`organisation.do` all answer 404), so meeting rows link to the XLSX file they come from and
to the portal's commissioner listing.
"""

from __future__ import annotations

HYS = "https://ec.europa.eu/info/law/better-regulation"
REGISTER = "https://transparency-register.europa.eu"
MEETINGS_PORTAL = (
    "https://ec.europa.eu/transparency-initiative/meetings/listcommissioners?collegeid=0"
)
MEETINGS_FILE = (
    "https://ec.europa.eu/transparency-initiative/meetings/data/meetings/dataxlsx"
    "?name=meetingscommissionrepresentatives2429"
)
REGISTER_EXPORT = f"{REGISTER}/odplastorganisationxml_en"


def initiative(initiative_id: int | str) -> str:
    return f"{HYS}/have-your-say/initiatives/{initiative_id}_en"


def publication_feedback(initiative_id: int | str, publication_id: int | str) -> str:
    """The feedback list of one publication on the portal."""
    return f"{HYS}/have-your-say/initiatives/{initiative_id}/feedback_en?p_id={publication_id}"


def feedback(initiative_id: int | str, feedback_id: int | str) -> str:
    return f"{HYS}/have-your-say/initiatives/{initiative_id}/F{feedback_id}_en"


def document(document_id: str, page_no: int | None = None) -> str:
    url = f"{HYS}/api/download/{document_id}"
    return f"{url}#page={page_no}" if page_no else url


def organisation(tr_id: str) -> str:
    return f"{REGISTER}/searchregister-or-update/organisation-detail_en?id={tr_id}"
