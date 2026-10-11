"""Static export: the files it writes, relative links only, no HTMX, the search index, the
identity on every page, and what the public site leaves out (drafts, decisions, review
items, model confidence, retrieval markers)."""

from __future__ import annotations

import json
import re

import pytest

from inspraak.agents import service
from inspraak.export import export_site
from inspraak.llm.base import db_recorder
from inspraak.llm.fake import FakeProvider

GREENPEACE = "9832909575-41"
PUB = 26173
OPTIONS = {"min_score": -1.0, "uncertain_score": 0.99, "top_k": 3}

pytestmark = pytest.mark.db


def test_export_writes_a_relative_static_site(snapshot, agent_env, tmp_path):
    provider = FakeProvider(recorder=db_recorder(agent_env))
    run = service.start_run(GREENPEACE, options=OPTIONS, provider=provider)
    item = service.list_review_items(run_id=run["id"], kind="go_no_go")[0]
    service.decide(item["id"], {"go": [PUB]}, decided_by="kaan", provider=provider)

    out = tmp_path / "site"
    stats = export_site(out)
    assert stats["organisations"] == 1 and stats["runs"] == 1 and stats["consultations"] >= 1
    assert (out / "index.html").exists() and (out / "about.html").exists()
    assert (out / ".nojekyll").exists() and (out / "static" / "style.css").exists()
    assert (out / "org" / f"{GREENPEACE}.html").exists()
    assert (out / "consultation" / f"{PUB}.html").exists()
    assert (out / "run" / f"{run['id']}.html").exists()

    index = json.loads((out / "search.json").read_text(encoding="utf-8"))
    assert index[0]["tr_id"] == GREENPEACE and index[0]["url"] == f"org/{GREENPEACE}.html"

    pages = list(out.rglob("*.html"))
    assert len(pages) == stats["pages"]
    for page in pages:
        html = page.read_text(encoding="utf-8")
        assert 'href="/' not in html and 'src="/' not in html, page  # relative links only
        assert "hx-" not in html and "htmx" not in html, page
        assert "Static snapshot" in html
        assert "Review inbox" not in html  # the inbox needs the app
        # who built it, on every page
        assert "Built by Kaan Goker" in html
        assert 'href="https://kaangoker.dev"' in html
        assert 'href="https://github.com/KaanGoker/inspraak"' in html
        assert 'href="https://linkedin.com/in/kaangoker"' in html
        assert "Not affiliated with any organisation shown" in html
        assert "mailto:nadirkaangoker@gmail.com" in html
        assert 'property="og:title"' in html and 'name="description"' in html
        # what stays in the local app
        lower = html.lower()
        for leak in (
            "chunk:",
            "machine draft",
            "draft response",
            "reviewer:",
            "decided by",
            "demo decision",
            ": no-go",
            ": go<",
            ">no-go<",
            "the reviewer decided",
            "requested by",
            "confidence 0",
            "confidence 1",
            "similarity 0",
            "relevance 0",
            "relevance 1",
            "moon landing",
        ):
            assert leak not in lower, (page, leak)
        assert "days left" not in html or "data-deadline" in html  # no frozen countdown

    org = (out / "org" / f"{GREENPEACE}.html").read_text(encoding="utf-8")
    noindex = '<meta name="robots" content="noindex, nofollow">'
    assert noindex in org
    # consultation pages carry model readings of named submissions: out of search engines too
    assert noindex in (out / "consultation" / f"{PUB}.html").read_text(encoding="utf-8")
    assert noindex not in (out / "about.html").read_text(encoding="utf-8")
    assert "model reads as:" in org  # a stance is the model's reading, not a label
    assert "Start a run" not in org and "<form" not in org
    assert "submissions in this snapshot" in org or "submission in this snapshot" in org
    assert 'data-deadline="' in org
    assert 'href="../consultation/26173.html"' in org
    assert 'href="../static/style.css"' in org
    home = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="static/style.css"' in home and f'href="org/{GREENPEACE}.html"' in home
    assert "search.json" in home
    assert '<meta name="robots"' not in home
    run_page = (out / "run" / f"{run['id']}.html").read_text(encoding="utf-8")
    # the brief stage only: the trace stops at the pause for the human go / no-go
    assert "brief stage, up to the human go / no-go" in run_page
    assert ">drafter<" not in run_page and ">critic<" not in run_page
    assert ">profiler<" in run_page and ">go_no_go<" in run_page

    # no internal link points at a page that was not exported
    for page in pages:
        html = page.read_text(encoding="utf-8")
        for href in re.findall(r'href="((?:\.\./)?(?:org|consultation|run)/[^"#]+\.html)', html):
            assert (page.parent / href).resolve().is_file(), (page, href)


def test_export_with_an_explicit_organisation_and_no_runs(snapshot, agent_env, tmp_path):
    edits = {"goals": "Corrected goals text", "note": "for the drafter"}
    service.edit_profile(GREENPEACE, edits, by="kaan")
    out = tmp_path / "site"
    stats = export_site(out, organisations=[GREENPEACE, "000-00"])
    assert stats["organisations"] == 1 and stats["runs"] == 0
    org = (out / "org" / f"{GREENPEACE}.html").read_text(encoding="utf-8")
    assert "No run trace in this snapshot" in org
    # a reviewer's correction is shown as the profile; the fact of the correction, the
    # register values before it and the note for the drafter stay in the app
    assert "Corrected goals text" in org
    private = (
        "reviewer corrections applied",
        "Register values before correction",
        "for the drafter",
        "Reviewer note",
    )
    for text in private:
        assert text not in org, text
    forbidden = "[" + "".join(chr(c) for c in (0x2012, 0x2013, 0x2014, 0x2015, 0x2212)) + "]"
    assert not re.search(forbidden, (out / "about.html").read_text("utf-8"))
