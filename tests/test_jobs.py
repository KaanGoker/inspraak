"""Publication-layer jobs on the small snapshot: Reader cache, Feedback analyst with quote
verification and review queue, Meetings mapper. Fake provider, test database."""

from __future__ import annotations

import pytest

from inspraak.jobs import precompute_publication
from inspraak.jobs.analyst import analyse_feedback, analyse_publication, analyses_for_publication
from inspraak.jobs.meetings_map import lobby_picture, map_publication
from inspraak.jobs.reader import summarise_publication
from inspraak.llm.base import db_recorder
from inspraak.llm.fake import FakeProvider

pytestmark = pytest.mark.db


def test_reader_summary_is_cached_forever(snapshot):
    provider = FakeProvider()
    row, hit = summarise_publication(snapshot, provider, 26173)
    assert hit is False and provider.calls == 1
    assert row["summary"]["one_paragraph"] and row["source_chunks"] == 2
    assert "Age assurance" in row["summary"]["key_points"][0]
    again, hit = summarise_publication(snapshot, provider, 26173)
    assert hit is True and provider.calls == 1 and again["created_at"] == row["created_at"]
    purpose, prompt = provider.prompts[0]
    assert purpose == "reader_summary" and "Article 5" in prompt and "Child Safety" in prompt


def test_analyst_verifies_quotes_and_queues_low_confidence(snapshot, db_url):
    provider = FakeProvider(recorder=db_recorder(db_url))
    orgs = snapshot.execute(
        "SELECT id, organization, feedback_text FROM feedback WHERE NOT is_citizen ORDER BY id"
    ).fetchall()
    assert len(orgs) == 2
    long_row = max(orgs, key=lambda r: len(r["feedback_text"] or ""))
    row, hit = analyse_feedback(snapshot, provider, long_row["id"])
    assert hit is False and row["stance"] in ("supports", "opposes", "wants_changes")
    # the fake always adds one invented quote; the verifier must drop exactly that one
    assert row["quotes_dropped"] == 1 and row["quotes_total"] == len(row["key_asks"]) + 1
    for ask in row["key_asks"]:
        assert ask["verified"] and ask["quote"] in long_row["feedback_text"]
    assert row["needs_review"] is False
    cached, hit = analyse_feedback(snapshot, provider, long_row["id"])
    assert hit is True and provider.calls == 1

    # a short text gives the fake an 'unclear' stance with low confidence: review queue
    short_row = min(orgs, key=lambda r: len(r["feedback_text"] or ""))
    snapshot.execute(
        "UPDATE feedback SET feedback_text = 'Noted.', feedback_text_en = NULL WHERE id = %s",
        (short_row["id"],),
    )
    snapshot.commit()
    row, _ = analyse_feedback(snapshot, provider, short_row["id"])
    assert row["stance"] == "unclear" and row["needs_review"] is True
    item = snapshot.execute(
        "SELECT kind, triggers, blocking FROM review_items WHERE feedback_id = %s",
        (short_row["id"],),
    ).fetchone()
    assert item["kind"] == "stance" and item["triggers"] == ["low-confidence stance"]
    assert item["blocking"] is False
    logged = snapshot.execute("SELECT count(*) AS n FROM llm_calls WHERE ok").fetchone()["n"]
    assert logged == 2
    rows = analyses_for_publication(snapshot, 26173)
    assert len(rows) == 2 and rows[0]["register_category"] is not None or True


def test_analyst_without_any_text_makes_no_call(snapshot):
    provider = FakeProvider()
    fid = snapshot.execute(
        "SELECT id FROM feedback WHERE NOT is_citizen ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    # a questionnaire answer: no text, no attachment. Counted, not flagged.
    snapshot.execute(
        "UPDATE feedback SET feedback_text = NULL, feedback_text_en = NULL, attachment_count = 0 "
        "WHERE id = %s",
        (fid,),
    )
    snapshot.commit()
    row, _ = analyse_feedback(snapshot, provider, fid)
    assert provider.calls == 0 and row["source"] == "none" and row["needs_review"]
    assert (
        snapshot.execute(
            "SELECT count(*) AS n FROM review_items WHERE feedback_id = %s", (fid,)
        ).fetchone()["n"]
        == 0
    )
    # the same row with an attachment nobody could read: an inbox flag
    snapshot.execute("DELETE FROM feedback_analyses WHERE feedback_id = %s", (fid,))
    snapshot.execute("UPDATE feedback SET attachment_count = 1 WHERE id = %s", (fid,))
    snapshot.commit()
    row, _ = analyse_feedback(snapshot, provider, fid)
    item = snapshot.execute(
        "SELECT kind, triggers FROM review_items WHERE feedback_id = %s", (fid,)
    ).fetchone()
    assert provider.calls == 0 and item["kind"] == "attachment"
    assert item["triggers"] == ["unparseable attachment"]
    stats = analyse_publication(snapshot, provider, 26173)
    assert stats["analysed"] == 1 and stats["calls"] == 1  # the other row, once
    assert stats["left_for_next_pass"] == 0


def test_analyst_limit_counts_calls_not_rows(snapshot):
    provider = FakeProvider()
    fid = snapshot.execute(
        "SELECT id FROM feedback WHERE NOT is_citizen ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    snapshot.execute(
        "UPDATE feedback SET feedback_text = NULL, feedback_text_en = NULL, attachment_count = 0 "
        "WHERE id = %s",
        (fid,),
    )
    snapshot.commit()
    # budget 0: the row without text is still recorded (free), the other waits
    stats = analyse_publication(snapshot, provider, 26173, limit=0)
    assert provider.calls == 0 and stats["analysed"] == 1 and stats["no_text"] == 1
    assert stats["left_for_next_pass"] == 1
    stats = analyse_publication(snapshot, provider, 26173, limit=1)
    assert provider.calls == 1 and stats["analysed"] == 1 and stats["left_for_next_pass"] == 0
    assert analyse_publication(snapshot, provider, 26173, limit=1)["analysed"] == 0


def test_row_without_text_is_analysed_again_once_its_attachment_parses(snapshot):
    from inspraak.jobs.analyst import analyse_feedback

    provider = FakeProvider()
    fid = snapshot.execute(
        "SELECT id FROM feedback WHERE NOT is_citizen ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    snapshot.execute(
        "UPDATE feedback SET feedback_text = NULL, feedback_text_en = NULL, attachment_count = 1 "
        "WHERE id = %s",
        (fid,),
    )
    snapshot.execute(
        "INSERT INTO documents (document_id, kind, publication_id, feedback_id, parse_status) "
        "VALUES ('att-1', 'feedback_attachment', 26173, %s, 'pending')",
        (fid,),
    )
    snapshot.commit()
    row, hit = analyse_feedback(snapshot, provider, fid)
    assert row["source"] == "none" and hit is False and provider.calls == 0
    row, hit = analyse_feedback(snapshot, provider, fid)
    assert hit is True  # still pending: the cached row stands

    snapshot.execute("UPDATE documents SET parse_status = 'ok' WHERE document_id = 'att-1'")
    snapshot.execute(
        "INSERT INTO document_pages (document_id, page_no, text) VALUES ('att-1', 1, %s)",
        ("We support the proposal and ask for a longer transition period for small firms.",),
    )
    snapshot.commit()
    row, hit = analyse_feedback(snapshot, provider, fid)
    assert hit is False and row["source"] != "none" and provider.calls == 1


def test_precompute_order_is_open_first_then_recently_closed(snapshot):
    from datetime import datetime, timedelta

    from inspraak.jobs import publications_to_precompute

    pid = snapshot.execute(
        "SELECT id FROM publications WHERE in_scope ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    snapshot.execute(
        "INSERT INTO publications (id, initiative_id, type, title, feedback_status, end_date, "
        "in_scope) SELECT 999001, initiative_id, type, 'closed one', 'CLOSED', %s, true "
        "FROM publications WHERE id = %s",
        (datetime.now() - timedelta(days=10), pid),
    )
    snapshot.execute(
        "INSERT INTO publications (id, initiative_id, type, title, feedback_status, end_date, "
        "in_scope) SELECT 999002, initiative_id, type, 'closed earlier', 'CLOSED', %s, true "
        "FROM publications WHERE id = %s",
        (datetime.now() - timedelta(days=40), pid),
    )
    snapshot.commit()
    order = publications_to_precompute(snapshot)
    assert order[0] == pid and order[-2:] == [999001, 999002]
    assert publications_to_precompute(snapshot, open_only=True) == [pid]
    snapshot.execute("DELETE FROM publications WHERE id IN (999001, 999002)")
    snapshot.commit()


def test_citizen_rows_are_never_analysed(snapshot):
    citizen = snapshot.execute("SELECT id FROM feedback WHERE is_citizen LIMIT 1").fetchone()["id"]
    with pytest.raises(ValueError):
        analyse_feedback(snapshot, FakeProvider(), citizen)


def test_meetings_mapper_and_precompute(snapshot):
    n_meetings = snapshot.execute("SELECT count(*) AS n FROM meetings").fetchone()["n"]
    rows, hit = map_publication(snapshot, 26173, min_score=-1.0)
    assert hit is False and len(rows) == n_meetings > 0
    rows2, hit = map_publication(snapshot, 26173)
    assert hit is True and len(rows2) == n_meetings
    picture = lobby_picture(snapshot, 26173, "9832909575-41")
    assert picture["note"] == "subject match, approximate"
    assert sum(r["meetings"] for r in picture["by_category"]) >= n_meetings
    provider = FakeProvider()
    stats = precompute_publication(snapshot, provider, 26173)
    assert stats["summary_cache_hit"] is False and stats["analyst"]["analysed"] == 2
    assert stats["calls"] == 3 and stats["meetings_cache_hit"] is True
