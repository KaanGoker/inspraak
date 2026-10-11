"""The organisation-layer graph end to end with the fake provider: supervisor routing, both
interrupts, resume from the Postgres checkpointer with a fresh graph, memory writes,
corrections reweighting the matcher, traces, escalations, service API."""

from __future__ import annotations

import pytest

from inspraak import db
from inspraak.agents import service
from inspraak.agents.graph import build_graph, open_checkpointer
from inspraak.agents.nodes import route
from inspraak.agents.state import WORKERS
from inspraak.llm.base import db_recorder
from inspraak.llm.fake import FakeProvider

pytestmark = pytest.mark.db

GREENPEACE = "9832909575-41"
PUB = 26173
OPTIONS = {"min_score": -1.0, "uncertain_score": 0.99, "top_k": 3}


def test_supervisor_routing_is_deterministic():
    state = {"run_id": "r", "tr_id": "t"}
    seen = []
    for key in ("profile", "matches", "landscape", "lobby", "reading"):
        seen.append(route(state))
        state[key] = {"x": 1} if key != "matches" else [{"publication_id": 1}]
    assert seen == ["profiler", "matcher", "landscape", "lobby", "reader"]
    assert route(state) == "go_no_go"
    state["go_decision"] = {"go": []}
    assert route(state) == "memory"
    state["go_decision"] = {"go": [1]}
    assert route(state) == "drafter"
    state["drafts"] = {"1": {}}
    assert route(state) == "critic"
    state["critiques"] = {"1": {}}
    assert route(state) == "review"
    state["review_decision"] = {"items": {}}
    assert route(state) == "memory"
    assert route({"run_id": "r", "tr_id": "t", "profile": {}, "matches": []}) == "memory"


def test_graph_has_every_worker_and_two_interrupt_nodes():
    graph = build_graph(FakeProvider()).get_graph()
    names = set(graph.nodes)
    assert {"supervisor", *WORKERS} <= names
    mermaid = build_graph(FakeProvider()).get_graph().draw_mermaid()
    assert "go_no_go" in mermaid and "review" in mermaid


def test_full_run_with_interrupts_resume_and_memory(snapshot, agent_env):
    provider = FakeProvider(recorder=db_recorder(agent_env))
    run = service.start_run(GREENPEACE, requested_by="test", options=OPTIONS, provider=provider)
    assert run["status"] == "waiting" and run["stage"] == "go_no_go"
    matches = run["result"]["matches"]
    assert [m["publication_id"] for m in matches] == [PUB]
    agents = [t["agent"] for t in run["traces"]]
    assert agents == [
        "supervisor", "profiler", "supervisor", "matcher", "supervisor", "landscape",
        "supervisor", "lobby", "supervisor", "reader", "supervisor", "go_no_go",
    ]  # fmt: skip
    reader_trace = next(t for t in run["traces"] if t["agent"] == "reader")
    assert reader_trace["llm_calls"] == 1 and reader_trace["cache_hit"] is False
    matcher_trace = next(t for t in run["traces"] if t["agent"] == "matcher")
    assert matcher_trace["llm_calls"] == 1  # one batched rerank call, then cached
    assert matches[0]["reranked"] and matches[0]["reason"]
    items = service.list_review_items(run_id=run["id"])
    blocking = [i for i in items if i["blocking"]]
    assert len(blocking) == 1 and blocking[0]["kind"] == "go_no_go"
    payload = blocking[0]["payload"]
    assert payload["consultations"][0]["publication_id"] == PUB
    assert payload["consultations"][0]["summary"]["one_paragraph"]
    # the matcher flagged the uncertain band (hash vectors give low scores)
    assert "relevance in the uncertain band" in {e["trigger"] for e in run["result"]["escalations"]}

    # resume with a fresh provider and graph, as a worker process would
    provider2 = FakeProvider(recorder=db_recorder(agent_env))
    out = service.decide(blocking[0]["id"], {"go": [PUB]}, decided_by="kaan", provider=provider2)
    run = out["run"]
    assert run["status"] == "waiting" and run["stage"] == "draft_review"
    assert provider2.calls == 2  # drafter + critic, nothing else
    draft = run["drafts"][0]
    assert draft["status"] == "machine_draft" and draft["chars"] <= 4000
    assert "moon landing" not in draft["text"]  # the critic removed the unsupported sentence
    assert "moon landing" in run["result"]["drafts"][str(PUB)]["machine_text"]
    # the fake drafter copies "[chunk:<id>]" markers like a real one; none may survive
    assert "chunk:" not in draft["text"]
    assert "chunk:" not in run["result"]["drafts"][str(PUB)]["machine_text"]
    with db.session(agent_env) as conn:
        stored = conn.execute("SELECT text FROM drafts WHERE run_id = %s", (run["id"],)).fetchall()
    assert stored and all("chunk:" not in r["text"] for r in stored)
    critique = run["result"]["critiques"][str(PUB)]
    assert critique["supported"] == critique["sentences"] - 1 and len(critique["removed"]) == 1
    triggers = {e["trigger"] for e in run["result"]["escalations"]}
    assert "critic found unsupported claims" in triggers
    items = service.list_review_items(run_id=run["id"])
    review = next(i for i in items if i["blocking"] and i["status"] == "open")
    assert review["kind"] == "draft_review" and "every draft" in review["triggers"]
    assert review["payload"]["label"] == "machine draft, awaiting human review"

    out = service.decide(
        review["id"],
        {"items": {str(PUB): {"action": "edit", "text": "Our edited answer. Short."}}},
        decided_by="kaan",
        provider=provider2,
    )
    run = out["run"]
    assert run["status"] == "done" and run["result"]["finished"] is True
    assert provider2.calls == 2  # memory writes need no model
    kinds = {(m["kind"], m["publication_id"]) for m in service.organisation_memory(GREENPEACE)}
    assert {("go", PUB), ("draft_edit", PUB), ("edited", PUB), ("position", PUB)} <= kinds
    versions = [(d["version"], d["status"]) for d in run["drafts"]]
    assert versions == [(1, "edited"), (2, "edited")]
    # rerank + reader summary + drafter + critic, from llm_calls
    assert run["llm"]["calls"] == 4
    still_open = service.list_review_items(run_id=run["id"])
    assert all(not i["blocking"] for i in still_open)  # only escalations stay in the inbox
    assert {i["kind"] for i in still_open} == {"relevance", "critic"}
    assert isinstance(still_open[0]["run_id"], str)
    assert [r["id"] for r in service.list_runs(GREENPEACE)] == [run["id"]]

    with pytest.raises(ValueError):
        service.decide(review["id"], {"items": {}}, provider=provider2)  # already decided


def test_no_go_ends_without_drafts_and_correction_reweights_matching(snapshot, agent_env):
    provider = FakeProvider(recorder=db_recorder(agent_env))
    run = service.start_run(GREENPEACE, options=OPTIONS, provider=provider)
    item = service.list_review_items(run_id=run["id"], kind="go_no_go")[0]
    out = service.decide(item["id"], {"not_our_topic": [PUB]}, provider=provider)
    run = out["run"]
    assert run["status"] == "done" and run["drafts"] == []
    assert run["result"]["go_decision"]["not_our_topic"] == [PUB]
    assert [t["agent"] for t in run["traces"]][-2:] == ["supervisor", "memory"]

    # second run: the corrected publication is excluded by the matcher, run ends at once
    calls_before = provider.calls
    run2 = service.start_run(GREENPEACE, options=OPTIONS, provider=provider)
    assert run2["status"] == "done" and run2["result"]["matches"] == []
    assert provider.calls == calls_before  # no reader, no draft


def test_demo_set_starts_missing_runs_only(snapshot, agent_env):
    provider = FakeProvider(recorder=db_recorder(agent_env))
    other = snapshot.execute(
        "SELECT tr_id FROM organisations WHERE tr_id <> %s AND profile_embedding IS NOT NULL "
        "ORDER BY tr_id LIMIT 1",
        (GREENPEACE,),
    ).fetchone()["tr_id"]
    out = service.start_demo([GREENPEACE, other, "000-00"], options=OPTIONS, provider=provider)
    assert [o.get("skipped") for o in out] == [None, None, "not in the snapshot"]
    assert all(o["status"] == "waiting" and o["stage"] == "go_no_go" for o in out[:2])
    assert out[0]["matches"][0][0] == PUB
    calls = provider.calls
    again = service.start_demo([GREENPEACE, other], options=OPTIONS, provider=provider)
    assert all(o["skipped"] == "has a run" for o in again)
    assert [o["run_id"] for o in again] == [o["run_id"] for o in out[:2]]
    assert provider.calls == calls


def test_profile_edits_reach_the_profiler(snapshot, agent_env):
    service.edit_profile(GREENPEACE, {"goals": "Corrected goals text", "note": "by reviewer"})
    with pytest.raises(ValueError):
        service.edit_profile(GREENPEACE, {"name": "x"})
    provider = FakeProvider()
    run = service.start_run(GREENPEACE, options=OPTIONS, provider=provider)
    with open_checkpointer(agent_env) as saver:
        graph = build_graph(provider, saver)
        state = graph.get_state({"configurable": {"thread_id": run["id"]}}).values
    assert state["profile"]["goals"] == "Corrected goals text"
    assert state["profile"]["edited_fields"] == ["goals", "note"]
    assert state["profile"]["register_values"]["goals"] != "Corrected goals text"
    assert state["profile"]["past_submissions"][0]["publication_id"] == PUB


def test_unknown_organisation_is_refused(snapshot, agent_env):
    with pytest.raises(service.RunNotFound):
        service.start_run("000-00", provider=FakeProvider())


def test_malformed_decision_is_refused_before_anything_is_stored(snapshot, agent_env):
    provider = FakeProvider(recorder=db_recorder(agent_env))
    run = service.start_run(GREENPEACE, options=OPTIONS, provider=provider)
    item = service.list_review_items(run_id=run["id"], kind="go_no_go")[0]
    out = service.decide(item["id"], {"go": [PUB]}, provider=provider)
    review = next(i for i in service.list_review_items(run_id=out["run"]["id"]) if i["blocking"])
    for bad in (
        {"items": {str(PUB): {"action": "bogus"}}},
        {"items": {str(PUB): {"action": "edit", "text": "   "}}},
        ["not", "a", "dict"],
    ):
        with pytest.raises(ValueError):
            service.decide(review["id"], bad, provider=provider)
    # nothing stored, the run still waits, a valid decision goes through
    assert service.list_review_items(run_id=run["id"], kind="draft_review")[0]["status"] == "open"
    assert service.get_run(run["id"])["status"] == "waiting"
    done = service.decide(
        review["id"], {"items": {str(PUB): {"action": "approve"}}}, provider=provider
    )
    assert done["run"]["status"] == "done"


def test_double_submit_dispatches_one_resume(snapshot, agent_env, monkeypatch):
    import threading

    provider = FakeProvider(recorder=db_recorder(agent_env))
    run = service.start_run(GREENPEACE, options=OPTIONS, provider=provider)
    item = service.list_review_items(run_id=run["id"], kind="go_no_go")[0]
    dispatched: list[str] = []
    monkeypatch.setattr(
        service, "_dispatch", lambda action, run_id, *a, **k: dispatched.append(run_id) or "x"
    )
    barrier = threading.Barrier(2)
    results: list[str] = []

    def submit():
        barrier.wait()
        try:
            service.decide(item["id"], {"no_go": [PUB]})
            results.append("ok")
        except service.AlreadyDecided:
            results.append("conflict")

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["conflict", "ok"] and len(dispatched) == 1


def test_resume_through_the_queue_shows_a_position(snapshot, agent_env, monkeypatch):
    from inspraak import queue

    provider = FakeProvider(recorder=db_recorder(agent_env))
    run = service.start_run(GREENPEACE, options=OPTIONS, provider=provider)
    item = service.list_review_items(run_id=run["id"], kind="go_no_go")[0]
    monkeypatch.setenv("RUN_INLINE", "0")
    monkeypatch.setattr(queue, "enqueue_resume", lambda run_id, decision: 3)
    monkeypatch.setattr(queue, "queue_position", lambda run_id, client=None: 3)
    out = service.decide(item["id"], {"go": [PUB]})
    assert out["dispatch"] == "celery:brief"
    assert out["run"]["status"] == "queued" and out["run"]["stage"] == "resuming"
    assert out["run"]["queue_position"] == 3


def test_failed_run_continues_from_its_last_checkpoint(snapshot, agent_env):
    class Flaky(FakeProvider):
        fail = True

        def complete_json(self, prompt, schema, *, purpose, **kwargs):
            if purpose == "draft" and Flaky.fail:
                Flaky.fail = False
                raise RuntimeError("model down")
            return super().complete_json(prompt, schema, purpose=purpose, **kwargs)

    provider = Flaky(recorder=db_recorder(agent_env))
    run = service.start_run(GREENPEACE, options=OPTIONS, provider=provider)
    item = service.list_review_items(run_id=run["id"], kind="go_no_go")[0]
    with pytest.raises(RuntimeError):
        service.decide(item["id"], {"go": [PUB]}, provider=provider)
    assert service.get_run(run["id"])["status"] == "failed"
    run = service.retry_run(run["id"], provider=provider)
    assert run["status"] == "waiting" and run["stage"] == "draft_review"
    assert len(run["drafts"]) == 1
