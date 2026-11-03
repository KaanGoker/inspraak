"""Web layer: every page on the small snapshot, the HTMX search partial, a run started and
reviewed through the forms (go/no-go, then an edited draft), profile corrections, the form
to decision mapping, charts and source links."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from inspraak.web import links
from inspraak.web.app import create_app, decision_from_form
from inspraak.web.charts import bar_chart

GREENPEACE = "9832909575-41"
PUB = 26173


@pytest.fixture
def client(snapshot, agent_env, monkeypatch):
    # hash vectors give low scores: open the matcher so the fixture publication matches
    monkeypatch.setenv("MATCH_MIN_SCORE", "-1")
    monkeypatch.setenv("MATCH_UNCERTAIN_SCORE", "0.99")
    monkeypatch.setenv("MATCH_TOP_K", "3")
    return TestClient(create_app(), follow_redirects=False)


# --------------------------------------------------------------------------- pages


@pytest.mark.db
def test_pages_render_from_the_snapshot(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Open consultations" in r.text and "Pick your organisation" in r.text
    assert "/consultation/26173" in r.text

    r = client.get("/search", params={"q": "greenpeace"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and f"/org/{GREENPEACE}" in r.text
    assert "<html" not in r.text  # partial for HTMX
    r = client.get("/search", params={"q": "greenpeace"})
    assert "<html" in r.text  # full page without the header

    r = client.get(f"/org/{GREENPEACE}")
    assert r.status_code == 200
    assert "About " in r.text and "Open consultations for" in r.text
    assert links.organisation(GREENPEACE) in r.text
    assert "subject match, approximate" in r.text
    assert "No draft" in r.text

    r = client.get(f"/consultation/{PUB}")
    assert r.status_code == 200
    assert "Who has already answered" in r.text and "By stakeholder type" in r.text
    assert links.initiative(19418) in r.text
    assert '<svg class="bars"' in r.text

    assert client.get("/inbox").status_code == 200
    assert "Not official, and it can be wrong" in client.get("/about").text
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/org/000-00").status_code == 404
    assert client.get("/consultation/1").status_code == 404
    assert client.get("/run/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/static/style.css").status_code == 200


@pytest.mark.db
def test_citizen_rows_are_counted_not_shown(client):
    r = client.get(f"/consultation/{PUB}")
    assert "EU citizen" in r.text  # counted in the chart
    # the fixture's invented citizen names never reach a page
    assert "Zorblatt" not in r.text and "Pearsonic" not in r.text


# --------------------------------------------------------------------------- run and review


@pytest.mark.db
def test_run_started_and_reviewed_through_the_forms(client):
    r = client.post(f"/org/{GREENPEACE}/run", data={"requested_by": "tester"})
    assert r.status_code == 303 and r.headers["location"].startswith("/run/")
    run_url = r.headers["location"]
    run_id = run_url.rsplit("/", 1)[1]

    r = client.get(run_url)
    assert r.status_code == 200
    assert "waiting for the go / no-go decision" in r.text
    assert "What each agent did" in r.text and "profiler" in r.text and "matcher" in r.text
    status = client.get(f"{run_url}/status")
    assert status.status_code == 200 and "waiting" in status.text

    inbox = client.get("/inbox").text
    assert "go / no-go decision" in inbox and f"decision_{PUB}" in inbox
    item_id = _first_item_id(inbox, "go_no_go")

    r = client.post(
        f"/review/{item_id}",
        data={f"decision_{PUB}": "go", "decided_by": "tester"},
    )
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    r = client.get(run_url)
    assert "waiting for the draft review" in r.text
    assert "machine draft, awaiting human review" in r.text
    assert "removed by the critic" in r.text  # the fake drafter's unsupported sentence

    org = client.get(f"/org/{GREENPEACE}").text
    assert "machine draft, awaiting human review" in org
    assert "reviewer: go" not in org  # decisions reach memory when the run finishes

    inbox = client.get("/inbox").text
    assert f"text_{PUB}" in inbox
    review_id = _first_item_id(inbox, "draft_review")
    r = client.post(
        f"/review/{review_id}",
        data={
            f"action_{PUB}": "edit",
            f"text_{PUB}": "Our edited answer. Short.",
            f"note_{PUB}": "tightened",
            "decided_by": "tester",
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200 and "<html" not in r.text  # partial swap
    assert "decided" in r.text and "done" in r.text

    r = client.get(run_url)
    assert "finished" in r.text and "Our edited answer." in r.text
    org = client.get(f"/org/{GREENPEACE}").text
    assert "edited and approved by the reviewer" in org and "reviewer: go" in org
    assert "draft edit" in org  # memory table

    # the decided item cannot be decided twice
    assert client.post(f"/review/{review_id}", data={"decided_by": "x"}).status_code == 409
    assert run_id in client.get(f"/consultation/{PUB}").text


@pytest.mark.db
def test_relevance_flag_not_our_topic_teaches_the_matcher(client):
    client.post(f"/org/{GREENPEACE}/run", data={"requested_by": "tester"})
    inbox = client.get("/inbox").text
    assert "relevance in the uncertain band" in inbox
    item_id = _first_item_id(inbox, "relevance")
    r = client.post(f"/review/{item_id}", data={"action": "not_our_topic", "decided_by": "t"})
    assert r.status_code == 303
    org = client.get(f"/org/{GREENPEACE}").text
    assert "not our topic" in org
    assert "No open in-scope consultation scores above the threshold" in org


@pytest.mark.db
def test_profile_correction_through_the_form(client):
    r = client.post(
        f"/org/{GREENPEACE}/profile",
        data={"goals": "Corrected goals", "interests": "Energy, Climate", "by": "tester"},
    )
    assert r.status_code == 303
    page = client.get(f"/org/{GREENPEACE}").text
    assert "reviewer corrections applied" in page and "Corrected goals" in page
    assert "Register values before correction" in page


# --------------------------------------------------------------------------- units


def test_decision_from_form_shapes():
    consultations = [{"publication_id": 1}, {"publication_id": 2}, {"publication_id": 3}]
    go_item = {"kind": "go_no_go", "payload": {"consultations": consultations}}
    d = decision_from_form(go_item, {"decision_1": "go", "decision_3": "not_our_topic"})
    assert d == {"go": [1], "no_go": [2], "not_our_topic": [3]}

    review_item = {
        "kind": "draft_review",
        "payload": {"drafts": [{"publication_id": 1, "text": "Same text."}]},
    }
    # an "edit" that did not change the text is an approve
    d = decision_from_form(review_item, {"action_1": "edit", "text_1": "Same text."})
    assert d["items"]["1"]["action"] == "approve" and d["items"]["1"]["text"] is None
    d = decision_from_form(review_item, {"action_1": "edit", "text_1": "New text."})
    assert d["items"]["1"] == {"action": "edit", "text": "New text.", "note": None}
    d = decision_from_form(review_item, {})
    assert d["items"]["1"]["action"] == "reject"

    d = decision_from_form(
        {"kind": "stance", "payload": {}}, {"action": "acknowledge", "stance": "opposes"}
    )
    assert d == {"action": "acknowledge", "note": None, "stance": "opposes"}


def test_bar_chart_escapes_and_handles_empty():
    svg = bar_chart([("A <b>", 3), ("B", 1)], title="t")
    assert svg.startswith("<svg") and "&lt;b&gt;" in svg and "<title>t</title>" in svg
    assert svg.count("<rect") == 2
    assert "No rows" in bar_chart([])


def test_source_links():
    assert links.initiative(19418).endswith("/initiatives/19418_en")
    assert links.feedback(19418, 5).endswith("/initiatives/19418/F5_en")
    assert links.document("abc", 7).endswith("/api/download/abc#page=7")
    assert "organisation-detail_en?id=1-2" in links.organisation("1-2")


def _first_item_id(html: str, kind: str) -> int:
    """The id of the first open item of that kind, read from the rendered inbox."""
    import re

    m = re.search(rf'id="item-(\d+)" data-kind="{kind}" data-status="open"', html)
    if m is None:
        raise AssertionError(f"no open {kind} item in the inbox")
    return int(m.group(1))


def test_rendered_pages_carry_no_long_dash():
    """Register names and titles can hold an en dash; the page shows a hyphen."""
    from jinja2 import DictLoader, Environment

    from inspraak.web.render import Renderer

    renderer = Renderer("static")
    renderer.env = Environment(loader=DictLoader({"t.html": "{{ name }}"}))
    name = "EUROCHAMBRES " + chr(0x2013) + " Chambers " + chr(0x2014) + " x " + chr(0x2212) + "1"
    assert renderer.render("t.html", name=name) == "EUROCHAMBRES - Chambers - x -1"


@pytest.mark.db
def test_write_actions_are_guarded(snapshot, agent_env, monkeypatch):
    import base64

    monkeypatch.setenv("REVIEW_TOKEN", "s3cret")
    monkeypatch.setenv("WRITE_RATE_PER_10MIN", "2")
    client = TestClient(create_app(), follow_redirects=False)
    # with a token every page asks for it: the app shows drafts and decisions
    assert client.get("/").status_code == 401
    assert client.get(f"/org/{GREENPEACE}").status_code == 401
    assert client.get("/inbox").status_code == 401
    assert client.post(f"/org/{GREENPEACE}/profile", data={"note": "x"}).status_code == 401
    assert client.get("/healthz").status_code == 200  # the container health check stays open
    assert client.get("/static/style.css").status_code == 200
    auth = {"Authorization": "Basic " + base64.b64encode(b"reviewer:s3cret").decode()}
    wrong = {"Authorization": "Basic " + base64.b64encode(b"reviewer:nope").decode()}
    assert client.get("/inbox", headers=wrong).status_code == 401
    assert client.get("/", headers=auth).status_code == 200
    assert client.get("/inbox", headers=auth).status_code == 200
    evil = {**auth, "Origin": "https://evil.example"}
    r = client.post(f"/org/{GREENPEACE}/profile", data={"note": "x"}, headers=evil)
    assert r.status_code == 403
    same = {**auth, "Origin": "http://testserver"}
    for _ in range(2):
        r = client.post(f"/org/{GREENPEACE}/profile", data={"note": "x"}, headers=same)
        assert r.status_code == 303
    r = client.post(f"/org/{GREENPEACE}/profile", data={"note": "x"}, headers=same)
    assert r.status_code == 429


# --------------------------------------------------------------------------- public text


def test_model_non_answers_are_left_off_the_page():
    from inspraak.web.views import clean_summary, is_non_answer

    assert is_non_answer("The provided text does not contain a consultation question.", asked=True)
    assert is_non_answer(
        "The text does not specify any questions directed at respondents.", asked=True
    )
    assert is_non_answer("The provided text excerpts do not contain questions.", asked=True)
    assert is_non_answer("", asked=True) and is_non_answer(None, asked=True)
    assert not is_non_answer("Respondents are asked for data on costs and benefits.", asked=True)
    # a key point about the proposal stays, even with "does not"
    assert not is_non_answer("The proposal does not include fees for SMEs.")
    assert not is_non_answer("This document evaluates the tariff scheme.")

    row = clean_summary(
        {
            "publication_id": 1,
            "summary": {
                "one_paragraph": "A proposal.",
                "what_is_asked": "The provided text does not contain a call for comments.",
                "key_points": ["Sets a reporting duty.", "The provided text is a draft act."],
            },
        }
    )
    assert row["summary"]["what_is_asked"] is None
    assert row["summary"]["key_points"] == ["Sets a reporting duty."]
    assert clean_summary(None) is None


def test_about_numbers_come_from_the_eval_file(tmp_path):
    """The about page, the README and docs/EVAL.md quote one file: docs/eval.json."""
    import json
    from collections import defaultdict
    from pathlib import Path

    from inspraak.web.render import Renderer
    from inspraak.web.views import EVAL_PATH, load_evaluation

    assert load_evaluation(tmp_path / "missing.json") is None
    ev = load_evaluation()
    assert ev is not None, f"{EVAL_PATH} is missing; run inspraak eval-report --write"
    m, man = ev["measured"], ev["manual"]
    html = Renderer("static").render(
        "about.html",
        title="About",
        evaluation=ev,
        stats=defaultdict(int),
        llm_calls=[],
        sources={"register": "", "hys": "", "meetings": ""},
    )
    critic = f"{m['critic']['supported']} of {m['critic']['sentences']} draft sentences"
    assert critic in html
    assert f"{man['precision']['relevant']} of {man['precision']['pairs']}" in html
    assert "Who built it" in html and 'id="method"' in html

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    eval_md = (root / "docs" / "EVAL.md").read_text(encoding="utf-8")
    numbers = {
        "critic": f"{m['critic']['supported']} of {m['critic']['sentences']}",
        "held back": f"{m['analyses']['held_back']:,}",
        "quotes": f"{m['analyses']['quotes_verified']:,} of {m['analyses']['quotes_total']:,}",
        "precision": f"{man['precision']['relevant']} of {man['precision']['pairs']}",
    }
    for what, text in numbers.items():
        assert text in readme, f"README does not quote the {what} number {text}"
        assert text in eval_md, f"docs/EVAL.md does not quote the {what} number {text}"
    assert json.loads((root / "docs" / "eval.json").read_text("utf-8")) == ev
