"""A small but complete snapshot from the recorded fixtures under tests/fixtures, so the
app has something to show a few minutes after `make up`, without the full ingest:

- 3 registrants from a register excerpt (Greenpeace European Unit among them),
- initiative 19418 with publication 26173 made open and in scope,
- 5 feedback rows (2 from organisations, the citizens counted only),
- a two-page proposal document written on the fly,
- 4 cabinet meetings,
- deterministic hash vectors everywhere (no model download).

`inspraak demo-seed` loads it, precomputes the publication with the deterministic
provider and starts one run that stops at the go / no-go. The test suite builds the same
snapshot in its own database (`tests/conftest.py`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from inspraak.config import PROJECT_ROOT

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
SEED_ORGANISATION = "9832909575-41"  # Greenpeace European Unit, in the register excerpt
SEED_PUBLICATION = 26173

SNAPSHOT_TABLES = (
    "chunks, document_pages, documents, feedback, publications, initiatives, meeting_orgs, "
    "meetings, organisations, ingest_runs, llm_calls, publication_summaries, "
    "feedback_analyses, publication_meetings, runs, run_traces, review_items, org_memory, "
    "drafts, match_reranks"
)


def clear_snapshot(conn: psycopg.Connection) -> None:
    """Empty every snapshot and agent table (the checkpointer's tables stay)."""
    conn.execute(f"TRUNCATE {SNAPSHOT_TABLES} RESTART IDENTITY CASCADE")
    conn.commit()


def snapshot_is_empty(conn: psycopg.Connection) -> bool:
    row = conn.execute("SELECT count(*) AS n FROM organisations").fetchone()
    return row["n"] == 0


def seed_snapshot(
    conn: psycopg.Connection, fixtures: Path | None = None, embedder: Any = None
) -> dict[str, Any]:
    """Load the fixture snapshot into `conn`. The caller empties the tables first."""
    import json

    import pymupdf

    from inspraak.embed import HashEmbedder
    from inspraak.embeddings import TARGETS, embed_target
    from inspraak.ingest.documents import store_pages
    from inspraak.ingest.hys import (
        listing_to_initiative,
        register_proposal_documents,
        store_detail,
        store_feedback,
        store_initiatives,
    )
    from inspraak.ingest.meetings import load_meetings
    from inspraak.ingest.register import load_register
    from inspraak.privacy import scrub_feedback_page
    from inspraak.store import publication_documents

    fixtures = fixtures or FIXTURES

    def load_json(name: str) -> Any:
        return json.loads((fixtures / name).read_text(encoding="utf-8"))

    load_register(conn, fixtures / "register_small.xml")
    page = load_json("hys_listing_page.json")["initiativeResultDtoPage"]["content"]
    store_initiatives(conn, [listing_to_initiative(i, datetime.now()) for i in page])
    pubs = store_detail(conn, load_json("hys_detail_19418.json"))
    scrubbed = scrub_feedback_page(load_json("hys_feedback_page_raw.json"))
    store_feedback(conn, SEED_PUBLICATION, scrubbed["content"], scrubbed["totalElements"], False)
    register_proposal_documents(conn, SEED_PUBLICATION, pubs[0]["attachments"])
    conn.execute(
        "UPDATE publications SET in_scope = true, feedback_status = 'OPEN', end_date = %s "
        "WHERE id = %s",
        (datetime.now() + timedelta(days=30), SEED_PUBLICATION),
    )
    document_id = publication_documents(conn, SEED_PUBLICATION)[0]["document_id"]
    doc = pymupdf.open()
    doc.new_page().insert_text(
        (72, 72),
        "Article 5 Age assurance obligations for platforms. Providers shall verify age "
        "with privacy preserving methods. Children must not be profiled for advertising.",
    )
    doc.new_page().insert_text(
        (72, 72), "Article 12 Fisheries quota are not in this act. Member States report yearly."
    )
    store_pages(conn, document_id, doc.tobytes())
    load_meetings(conn, fixtures / "meetings_small.xlsx")
    embedder = embedder or HashEmbedder()
    for name in TARGETS:
        embed_target(conn, TARGETS[name], embedder)
    conn.commit()
    counts = {
        "organisations": conn.execute("SELECT count(*) AS n FROM organisations").fetchone()["n"],
        "publications_in_scope": conn.execute(
            "SELECT count(*) AS n FROM publications WHERE in_scope"
        ).fetchone()["n"],
        "feedback": conn.execute("SELECT count(*) AS n FROM feedback").fetchone()["n"],
        "meetings": conn.execute("SELECT count(*) AS n FROM meetings").fetchone()["n"],
        "chunks": conn.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"],
    }
    return counts


def seed_demo(conn: psycopg.Connection, *, log: Any = None) -> dict[str, Any]:
    """The whole local demo: fixture snapshot, publication layer with the deterministic
    provider, one run up to the go / no-go. Returns what was made."""
    from inspraak.agents.service import start_run
    from inspraak.jobs import precompute_publication
    from inspraak.llm import make_provider

    say = log or (lambda _m: None)
    counts = seed_snapshot(conn)
    say(f"snapshot: {counts}")
    provider = make_provider("fake")
    stats = precompute_publication(conn, provider, SEED_PUBLICATION, log=log)
    say(f"publication layer: {stats}")
    run = start_run(SEED_ORGANISATION, requested_by="demo-seed", inline=True, provider=provider)
    say(f"run {run['id']}: {run['status']}, {run['stage']}")
    return {"snapshot": counts, "publication_layer": stats, "run": run["id"]}
