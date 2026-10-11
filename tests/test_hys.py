from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from inspraak.config import get_settings
from inspraak.http import Fetcher
from inspraak.ingest.cache import RawCache
from inspraak.ingest.hys import (
    choose_proposal_documents,
    detail_to_rows,
    fetch_feedback,
    fetch_listing,
    listing_to_initiative,
    plan_scope,
)

NOW = datetime(2026, 10, 3, 12, 0, 0)


def test_listing_item_to_initiative(load_json):
    items = load_json("hys_listing_page.json")["initiativeResultDtoPage"]["content"]
    row = listing_to_initiative(items[0], NOW)
    assert row["id"] == 19418 and isinstance(row["id"], int)
    assert row["reference"] == "COM(2026)681"
    assert row["short_title"] == "Child Safety Online"
    assert row["feedback_status"] == "OPEN"
    assert row["feedback_end"] == datetime(2026, 11, 26, 23, 59, 59)
    assert row["topics"] == ["Digital economy and society"]
    assert row["listed_at"] == NOW


def _row(i, status, end):
    return {"id": i, "feedback_status": status, "feedback_end": end}


def test_plan_scope_filters_client_side():
    settings = get_settings()
    rows = [
        _row(1, "OPEN", NOW + timedelta(days=10)),
        _row(2, "OPEN", NOW + timedelta(days=settings.scope_open_days + 1)),  # too far
        _row(3, "OPEN", NOW - timedelta(days=1)),  # says OPEN, already ended
        _row(4, "CLOSED", NOW - timedelta(days=5)),
        _row(5, "CLOSED", NOW - timedelta(days=settings.scope_closed_lookback_days + 1)),
        _row(6, "DISABLED", None),
        _row(7, "CLOSED", NOW - timedelta(days=2)),
    ]
    plan = plan_scope(rows, NOW, settings)
    assert plan.open_ids == [1]
    assert plan.closed_candidate_ids == [3, 7, 4]  # most recent first, "OPEN" past its end too
    assert plan.counts["status_DISABLED"] == 1
    assert plan.counts["open_within_window"] == 1


def test_detail_to_rows(load_json):
    initiative, pubs = detail_to_rows(load_json("hys_detail_19418.json"))
    assert initiative["id"] == 19418 and initiative["dg"] == "CNECT"
    assert initiative["policy_areas"] == ["Digital Economy and Society"]
    assert len(pubs) == 1
    pub = pubs[0]
    assert pub["id"] == 26173 and pub["type"] == "PROP_REG"
    assert pub["end_date"] == datetime(2026, 11, 26, 23, 59, 59)
    assert pub["total_feedback"] == 82
    assert {a["language"] for a in pub["attachments"]} == {"FR", "DE", "NL"}
    assert all(a["documentId"] for a in pub["attachments"])


def test_choose_proposal_documents_prefers_english_then_fallback():
    atts = [
        {"documentId": "a", "language": "DE", "category": "ACT"},
        {"documentId": "b", "language": "FR", "category": "ACT"},
        {"documentId": "c", "language": "BG", "category": "ANNEX"},
        {"documentId": "d", "language": "EN", "category": "ANNEX"},
    ]
    chosen = {a["documentId"] for a in choose_proposal_documents(atts)}
    assert chosen == {"b", "d"}  # FR for the act (no EN), EN for the annex
    only_bg = [{"documentId": "c", "language": "BG", "category": "ACT"}]
    assert choose_proposal_documents(only_bg)[0]["documentId"] == "c"


class _Transport(httpx.MockTransport):
    """Serves the fixtures and counts requests per URL path."""

    def __init__(self, load_json):
        self.calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(request.url.path)
            if request.url.path.endswith("/searchInitiatives"):
                return httpx.Response(200, json=load_json("hys_listing_page.json"))
            if request.url.path.endswith("/allFeedback"):
                return httpx.Response(200, json=load_json("hys_feedback_page_raw.json"))
            return httpx.Response(200, text="<html>shell</html>")

        super().__init__(handler)


def _fetcher(load_json):
    transport = _Transport(load_json)
    return Fetcher(interval=0, client=httpx.Client(transport=transport)), transport


def test_fetch_listing_uses_cache_on_second_run(tmp_path, load_json):
    fetcher, transport = _fetcher(load_json)
    cache = RawCache(root=tmp_path, max_age_hours=1)
    items = fetch_listing(fetcher, cache, get_settings())
    assert len(items) == 4
    assert transport.calls == ["/info/law/better-regulation/brpapi/searchInitiatives"]
    again = fetch_listing(fetcher, cache, get_settings())
    assert again == items
    assert len(transport.calls) == 1  # served from disk
    assert (tmp_path / "listing" / "page-0000.json").exists()


def test_fetch_feedback_scrubs_before_caching(tmp_path, load_json):
    fetcher, _ = _fetcher(load_json)
    cache = RawCache(root=tmp_path, max_age_hours=1)
    rows, total, truncated, pages = fetch_feedback(
        fetcher, cache, get_settings(), 26173, forever=True
    )
    assert total == 5 and len(rows) == 5 and not truncated and pages == 1
    cached = (tmp_path / "feedback" / "26173" / "page-0000.json").read_text(encoding="utf-8")
    for name in ("Testa", "Zorblatt", "Pearsonic", "Contact"):
        assert name not in cached
    assert "A citizen wrote this" not in cached
    assert "Our organisation welcomes" in cached


def test_fetch_feedback_truncates_at_max_rows(tmp_path, load_json):
    fetcher, _ = _fetcher(load_json)
    cache = RawCache(root=tmp_path, max_age_hours=1)
    rows, total, truncated, _ = fetch_feedback(
        fetcher, cache, get_settings(), 26173, forever=True, max_rows=2
    )
    assert len(rows) == 2 and total == 5 and truncated


def test_closed_since_last_fetch_gets_one_uncached_fetch(conn, tmp_path, load_json):
    """A publication that closed after its last fetch is fetched once more, past a fresh
    cache, and its scope reason says it closed; a second pass leaves it alone."""
    from inspraak.config import portal_now
    from inspraak.ingest.hys import (
        refresh_closed_since_fetch,
        store_detail,
        store_feedback,
        store_initiatives,
    )

    detail = load_json("hys_detail_19418.json")
    detail["publications"][0]["endDate"] = "2026/10/01 23:59:59"
    page = load_json("hys_listing_page.json")["initiativeResultDtoPage"]["content"]
    store_initiatives(conn, [listing_to_initiative(i, NOW) for i in page])
    store_detail(conn, detail)
    store_feedback(conn, 26173, [], 0, False)
    conn.execute(
        "UPDATE publications SET in_scope = true, scope_reason = 'open, closes 2026-10-01', "
        "feedback_fetched_at = '2026-09-30 08:00+00' WHERE id = 26173"
    )
    conn.commit()

    cache = RawCache(root=tmp_path, max_age_hours=1000)
    cache.put_json({"content": [], "totalElements": 0, "totalPages": 1}, "feedback", "26173",
                   "page-0000.json")  # fmt: skip

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "/groupInitiatives/" in request.url.path:
            return httpx.Response(200, json=detail)
        return httpx.Response(200, json=load_json("hys_feedback_page_raw.json"))

    fetcher = Fetcher(interval=0, client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = refresh_closed_since_fetch(conn, fetcher, cache, get_settings(), now=portal_now())
    assert [r.publication_id for r in out] == [26173]
    assert any("/groupInitiatives/19418" in c for c in calls)
    assert any(c.endswith("/allFeedback") for c in calls)
    row = conn.execute(
        "SELECT total_feedback, scope_reason FROM publications WHERE id = 26173"
    ).fetchone()
    assert row["total_feedback"] == 5
    assert row["scope_reason"].startswith("closed 2026-10-01")

    again = refresh_closed_since_fetch(conn, fetcher, cache, get_settings(), now=portal_now())
    assert again == []
