"""Against the docker compose Postgres (database inspraak_test). Skipped when it is down."""

from __future__ import annotations

import json

import psycopg
import pytest

from inspraak import db, store
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

pytestmark = pytest.mark.db


def test_migrate_is_idempotent(db_url):
    assert db.migrate(db_url) == []
    with db.session(db_url) as conn:
        names = [r["name"] for r in conn.execute("SELECT name FROM schema_migrations")]
    assert names == [p.name for p in db.migration_files()]


def test_register_load_and_lookup(conn, fixtures):
    stats = load_register(conn, fixtures / "register_small.xml")
    assert stats["stored"] == 3 and stats["declared"] == 3
    hits = store.find_organisations(conn, "greenpeace")
    assert hits[0]["tr_id"] == "9832909575-41"
    assert store.find_organisations(conn, "GPEU")[0]["tr_id"] == "9832909575-41"
    assert store.find_organisations(conn, "9832909575-41")[0]["name"] == "Greenpeace European Unit"
    org = store.get_organisation(conn, "763743132433-49")
    assert org["category"] == "Companies & groups"


def test_register_reload_keeps_vector_when_text_unchanged(conn, fixtures):
    load_register(conn, fixtures / "register_small.xml")
    embed_target(conn, TARGETS["organisations"], HashEmbedder())
    assert store.organisation_vector(conn, "9832909575-41") is not None
    load_register(conn, fixtures / "register_small.xml")
    assert store.organisation_vector(conn, "9832909575-41") is not None


def test_citizen_content_is_rejected_by_the_database(conn, load_json):
    page = load_json("hys_listing_page.json")["initiativeResultDtoPage"]["content"]
    from datetime import datetime

    store_initiatives(conn, [listing_to_initiative(i, datetime.now()) for i in page])
    store_detail(conn, load_json("hys_detail_19418.json"))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO feedback (id, publication_id, user_type, is_citizen, feedback_text) "
            "VALUES (1, 26173, 'EU_CITIZEN', true, 'a citizen wrote this')"
        )
    conn.rollback()
    # and there is simply no column a name could go into
    cols = {
        r["column_name"]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'feedback'"
        )
    }
    assert not cols & {"first_name", "firstname", "surname", "login"}


def test_feedback_pipeline_end_to_end(conn, load_json):
    from datetime import datetime

    page = load_json("hys_listing_page.json")["initiativeResultDtoPage"]["content"]
    store_initiatives(conn, [listing_to_initiative(i, datetime.now()) for i in page])
    pubs = store_detail(conn, load_json("hys_detail_19418.json"))
    assert pubs[0]["id"] == 26173
    scrubbed = scrub_feedback_page(load_json("hys_feedback_page_raw.json"))
    result = store_feedback(conn, 26173, scrubbed["content"], scrubbed["totalElements"], False)
    assert result.fetched == 5 and result.organisations == 2 and result.citizens == 3
    assert register_proposal_documents(conn, 26173, pubs[0]["attachments"]) == 1  # FR fallback

    counts = store.feedback_counts(conn, 26173)
    by_type = {(r["user_type"], r["is_citizen"]): r["n"] for r in counts["by_type"]}
    assert by_type == {
        ("EU_CITIZEN", True): 1,
        ("NGO", False): 1,
        ("NGO", True): 1,  # NGO row without an organisation name counts as a citizen
        ("COMPANY", False): 1,
        ("SOMETHING_NEW", True): 1,
    }
    orgs = store.organisation_feedback(conn, 26173)
    assert sorted(o["user_type"] for o in orgs) == ["COMPANY", "NGO"]
    ngo = next(o for o in orgs if o["user_type"] == "NGO")
    assert ngo["tr_number"] == "9832909575-41"
    atts = store.feedback_attachments(conn, ngo["id"])
    assert atts[0]["document_id"] == "0900000000000002" and atts[0]["parse_status"] == "pending"

    dumped = json.dumps(conn.execute("SELECT * FROM feedback").fetchall(), default=str)
    for name in ("Testa", "Zorblatt", "Pearsonic", "Contact", "A citizen wrote this"):
        assert name not in dumped

    pub = store.get_publication(conn, 26173)
    assert pub["org_feedback_count"] == 2 and pub["in_scope"] is False
    docs = store.publication_documents(conn, 26173)
    assert docs[0]["language"] == "FR"


def test_document_pages_chunks_and_similarity(conn, load_json):
    from datetime import datetime

    import pymupdf

    page = load_json("hys_listing_page.json")["initiativeResultDtoPage"]["content"]
    store_initiatives(conn, [listing_to_initiative(i, datetime.now()) for i in page])
    pubs = store_detail(conn, load_json("hys_detail_19418.json"))
    register_proposal_documents(conn, 26173, pubs[0]["attachments"])
    document_id = store.publication_documents(conn, 26173)[0]["document_id"]
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Article 5 Age assurance obligations for platforms.")
    doc.new_page().insert_text((72, 72), "Article 12 Fisheries quota are not in this act.")
    result = store_pages(conn, document_id, doc.tobytes())
    assert result["status"] == "ok" and result["pages"] == 2 and result["chunks"] == 2
    assert store.document_page(conn, document_id, 2).startswith("Article 12")

    embedder = HashEmbedder()
    assert embed_target(conn, TARGETS["chunks"], embedder) == 2
    query = embedder.embed(["age assurance platforms"])[0]
    hits = store.similar_chunks(conn, 26173, query, limit=2)
    assert hits[0]["page_no"] == 1 and "Age assurance" in hits[0]["text"]
    assert embed_target(conn, TARGETS["chunks"], embedder) == 0  # idempotent


def test_publication_match_text_and_meetings(conn, fixtures, load_json):
    from datetime import datetime

    page = load_json("hys_listing_page.json")["initiativeResultDtoPage"]["content"]
    store_initiatives(conn, [listing_to_initiative(i, datetime.now()) for i in page])
    store_detail(conn, load_json("hys_detail_19418.json"))
    conn.execute("UPDATE publications SET in_scope = true WHERE id = 26173")
    conn.commit()
    embedder = HashEmbedder()
    assert embed_target(conn, TARGETS["publications"], embedder) == 1
    pub = store.get_publication(conn, 26173)
    assert pub["match_text"].startswith("Child Safety Online")
    hits = store.similar_publications(conn, embedder.embed(["child safety online"])[0], limit=3)
    assert hits[0]["id"] == 26173 and hits[0]["score"] > 0

    stats = load_meetings(conn, fixtures / "meetings_small.xlsx")
    assert stats["meetings"] == 23
    assert embed_target(conn, TARGETS["meetings"], embedder) == 23
    first_subject = conn.execute("SELECT subject, id FROM meetings LIMIT 1").fetchone()
    hits = store.similar_meetings(conn, embedder.embed([first_subject["subject"]])[0], limit=3)
    assert hits[0]["id"] == first_subject["id"]
    assert hits[0]["orgs"][0]["tr_id"]
    load_meetings(conn, fixtures / "meetings_small.xlsx")  # reload keeps the vectors
    assert (
        conn.execute("SELECT count(*) AS n FROM meetings WHERE embedding IS NULL").fetchone()["n"]
        == 0
    )

    snapshot = store.snapshot_stats(conn)
    assert snapshot["meetings"] == 23 and snapshot["publications"] == 1


def test_ingest_runs_record(conn):
    run_id = db.start_run(conn, "test")
    db.finish_run(conn, run_id, True, {"rows": 1})
    row = conn.execute("SELECT * FROM ingest_runs WHERE id = %s", (run_id,)).fetchone()
    assert row["ok"] is True and row["stats"] == {"rows": 1} and row["finished_at"]


def test_run_hys_handles_a_grouped_publication_once(conn, load_json, tmp_path, monkeypatch):
    """One call for evidence shared by two initiatives: fetched, counted and kept once,
    linked to both parents, and its match text carries both titles."""
    from datetime import datetime

    import httpx

    from inspraak.http import Fetcher
    from inspraak.ingest.cache import RawCache
    from inspraak.ingest.hys import run_hys

    monkeypatch.setenv("SCOPE_CLOSED_MIN_ORG", "2")
    now = datetime(2026, 10, 3, 12, 0, 0)

    def listing_item(i, title):
        return {
            "id": float(i),
            "reference": f"Ares(2026){i}",
            "shortTitle": title,
            "foreseenActType": "REG_DEL",
            "initiativeStatus": "ACTIVE",
            "topics": [{"label": "Food safety"}],
            "currentStatuses": [
                {
                    "isCurrent": True,
                    "frontEndStage": "ISC_WORKFLOW",
                    "receivingFeedbackStatus": "CLOSED",
                    "feedbackStartDate": "2026/09/01 00:00:00",
                    "feedbackEndDate": "2026/09/28 23:59:59",
                }
            ],
        }

    def detail(i, title):
        return {
            "id": i,
            "shortTitle": title,
            "dg": "SANTE",
            "dossierSummary": f"Summary of {title}",
            "authorName": "Staff",
            "publications": [
                {
                    "id": 500,
                    "type": "CALL_FOR_EVIDENCE",
                    "title": "Shared call for evidence",
                    "receivingFeedbackStatus": "CLOSED",
                    "endDate": "2026/09/28 23:59:59",
                    "totalFeedback": 5,
                    "groupId": i,
                    "attachments": [
                        {"documentId": "090000000000aaaa", "language": "EN", "category": "ACT"}
                    ],
                }
            ],
        }

    titles = {101: "Genomic techniques: information", 102: "Genomic techniques: applications"}
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/searchInitiatives"):
            content = [listing_item(i, t) for i, t in titles.items()]
            return httpx.Response(
                200,
                json={
                    "initiativeResultDtoPage": {
                        "content": content,
                        "totalPages": 1,
                        "totalElements": 2,
                    }
                },
            )
        if "/groupInitiatives/" in path:
            i = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=detail(i, titles[i]))
        if path.endswith("/allFeedback"):
            return httpx.Response(200, json=load_json("hys_feedback_page_raw.json"))
        return httpx.Response(404)

    fetcher = Fetcher(interval=0, client=httpx.Client(transport=httpx.MockTransport(handler)))
    cache = RawCache(root=tmp_path, max_age_hours=1)
    stats = run_hys(conn, fetcher, cache, now=now)

    assert stats["closed_publications"] == 1 and stats["feedback_rows"] == 5
    assert sum(1 for c in calls if c.endswith("/allFeedback")) == 1
    pub = store.get_publication(conn, 500)
    assert pub["in_scope"] and pub["initiative_id"] == 101
    assert [i["id"] for i in store.publication_initiatives(conn, 500)] == [101, 102]
    assert conn.execute("SELECT count(*) AS n FROM feedback").fetchone()["n"] == 5
    assert conn.execute("SELECT count(*) AS n FROM documents").fetchone()["n"] == 2

    embed_target(conn, TARGETS["publications"], HashEmbedder())
    text = store.get_publication(conn, 500)["match_text"]
    assert "information" in text and "applications" in text

    # A second run is served from the cache and changes nothing.
    before = len(calls)
    again = run_hys(conn, fetcher, cache, now=now)
    assert len(calls) == before and again["closed_publications"] == 1


def test_match_baseline_ranks_the_answered_publication(conn, fixtures, load_json):
    """An organisation whose profile vector equals a publication vector, and that filed
    feedback on it, gets rank 1 for that pair."""
    from datetime import datetime

    from inspraak.evaluate import match_baseline

    load_register(conn, fixtures / "register_small.xml")
    page = load_json("hys_listing_page.json")["initiativeResultDtoPage"]["content"]
    store_initiatives(conn, [listing_to_initiative(i, datetime.now()) for i in page])
    store_detail(conn, load_json("hys_detail_19418.json"))
    scrubbed = scrub_feedback_page(load_json("hys_feedback_page_raw.json"))
    store_feedback(conn, 26173, scrubbed["content"], scrubbed["totalElements"], False)
    conn.execute("UPDATE publications SET in_scope = true")
    assert match_baseline(conn)["pairs"] == 0  # no vectors yet

    embedder = HashEmbedder()
    embed_target(conn, TARGETS["organisations"], embedder)
    embed_target(conn, TARGETS["publications"], embedder)
    result = match_baseline(conn)
    assert result["pairs"] == 1 and result["organisations"] == 1 and result["publications"] == 1
    assert result["embedding"] == {
        "recall_at_5": 1.0,
        "recall_at_10": 1.0,
        "mrr": 1.0,
        "median_rank": 1.0,
    }
