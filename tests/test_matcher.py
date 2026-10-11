"""The "not our topic" penalty demotes near neighbours of a corrected publication only.
EU texts are all fairly similar to each other (real corpus: mean pairwise cosine 0.45,
standard deviation 0.13), so a flat penalty used to push almost every match under the
real min_score of 0.45."""

from __future__ import annotations

import numpy as np
import pytest

from inspraak.agents.matcher import collapse_initiatives, correction_penalty, hub_scores, match

MIN_SCORE = 0.45
GREENPEACE = "9832909575-41"
PUB = 26173


def _unit(v: np.ndarray) -> np.ndarray:
    return (v / np.linalg.norm(v, axis=-1, keepdims=True)).astype(np.float32)


def _corpus(n: int = 30, seed: int = 7) -> np.ndarray:
    """Unit vectors sharing a common direction, pairwise cosine around 0.45."""
    rng = np.random.default_rng(seed)
    common = rng.normal(size=64)
    common /= np.linalg.norm(common)
    noise = _unit(rng.normal(size=(n, 64)))
    return _unit(0.9 * common + noise)


def test_corpus_resembles_the_real_similarity_level():
    m = _corpus()
    sims = (m @ m.T)[np.triu_indices(len(m), 1)]
    assert 0.35 < sims.mean() < 0.55


def test_one_correction_keeps_the_unrelated_matches():
    m = _corpus()
    rng = np.random.default_rng(1)
    profile = _unit(m.mean(axis=0) + 0.02 * rng.normal(size=64))
    scores = m @ profile
    kept_before = int((scores >= MIN_SCORE).sum())
    assert kept_before >= 10

    # correct the top match and, separately, the least similar publication
    for corrected in (int(np.argmax(scores)), int(np.argmin(scores))):
        penalty = correction_penalty(m, m[[corrected]])
        after = scores - penalty
        mask = np.ones(len(m), dtype=bool)
        mask[corrected] = False
        kept_after = int((after[mask] >= MIN_SCORE).sum())
        assert kept_after >= kept_before - 3, (corrected, kept_before, kept_after)


def test_a_near_duplicate_of_the_corrected_publication_is_demoted():
    m = _corpus()
    twin = _unit(m[0] + 0.05 * np.random.default_rng(3).normal(size=64))
    corpus = np.vstack([m, twin[None, :]])
    penalty = correction_penalty(corpus, corpus[[0]])
    assert penalty[-1] > 0.15  # the twin
    assert np.median(penalty[1:-1]) == 0.0  # typical publications untouched


# --------------------------------------------------------------------------- matcher v2


def _card(pid, initiative, score, end, status="OPEN", title=None):
    return {
        "publication_id": pid,
        "initiative_id": initiative,
        "short_title": title or f"initiative {initiative}",
        "feedback_status": status,
        "end_date": end,
        "score": score,
        "raw_score": score,
        "band": "match",
    }


def test_one_card_per_initiative_keeps_the_best_score_and_the_nearest_deadline():
    cards = [
        _card(1, 10, 0.70, "2026-11-03"),
        _card(2, 10, 0.75, "2026-11-20"),
        _card(3, 10, 0.60, "2026-10-01", status="CLOSED"),
        _card(4, 11, 0.65, "2026-11-01"),
        _card(5, 12, 0.50, "2026-11-01", title="initiative 11"),  # same title, other id
    ]
    out = collapse_initiatives(cards)
    assert [c["publication_id"] for c in out] == [1, 4]
    assert out[0]["score"] == 0.75 and out[0]["siblings"] == [2, 3]
    assert out[1]["siblings"] == [5]


def test_hub_scores_are_centred_and_flag_the_publication_close_to_everyone():
    rng = np.random.default_rng(0)
    orgs = _unit(rng.normal(size=(300, 64)))
    pubs = _unit(rng.normal(size=(6, 64)))
    pubs[0] = _unit(orgs.mean(axis=0))  # close to every profile
    hub = hub_scores(orgs, pubs, k=50)
    assert abs(float(hub.mean())) < 1e-6
    assert int(np.argmax(hub)) == 0


class StubReranker:
    def __init__(self, judged):
        self.judged = judged
        self.asked: list[int] = []

    def scores(self, ids):
        self.asked = list(ids)
        return {i: self.judged[i] for i in ids if i in self.judged}


def _extra_publications(conn):
    """Two more open publications next to 26173: 90001 in the same initiative with a later
    deadline, 90002 in another initiative."""
    other = conn.execute(
        "SELECT id FROM initiatives WHERE id <> (SELECT initiative_id FROM publications "
        "WHERE id = 26173) ORDER BY id LIMIT 1"
    ).fetchone()["id"]
    conn.execute("CREATE TEMP TABLE extra AS SELECT * FROM publications WHERE id = 26173")
    conn.execute("UPDATE extra SET id = 90001, end_date = end_date + interval '5 days'")
    conn.execute("INSERT INTO publications SELECT * FROM extra")
    conn.execute("UPDATE extra SET id = 90002, initiative_id = %s", (other,))
    conn.execute("INSERT INTO publications SELECT * FROM extra")
    conn.execute("DROP TABLE extra")
    conn.commit()


@pytest.mark.db
def test_match_collapses_siblings_and_follows_the_rerank(snapshot):
    from inspraak.store import organisation_vector

    _extra_publications(snapshot)
    vector = organisation_vector(snapshot, GREENPEACE)
    kwargs = {"top_k": 5, "min_score": -1.0, "uncertain_score": 0.99}
    plain = match(snapshot, vector, **kwargs)
    assert sorted(c["publication_id"] for c in plain) == [26173, 90002]
    card = next(c for c in plain if c["publication_id"] == 26173)
    assert card["siblings"] == [90001] and not card["reranked"]

    reranker = StubReranker(
        {
            26173: {"relevance": 0.4, "reason": "peripheral"},
            90002: {"relevance": 0.9, "reason": "core subject"},
        }
    )
    ranked = match(snapshot, vector, reranker=reranker, **kwargs)
    assert sorted(reranker.asked) == [26173, 90002]  # siblings are not judged twice
    assert [c["publication_id"] for c in ranked] == [90002, 26173]
    assert ranked[0]["band"] == "match" and ranked[1]["band"] == "uncertain"
    assert ranked[0]["reason"] == "core subject" and ranked[0]["rank"] == 1

    reranker.judged[26173] = {"relevance": 0.1, "reason": "unrelated"}
    assert [c["publication_id"] for c in match(snapshot, vector, reranker=reranker, **kwargs)] == [
        90002
    ]
    # a candidate the model skipped stays, at a neutral position
    reranker.judged = {90002: {"relevance": 0.2, "reason": "unrelated"}}
    assert [c["publication_id"] for c in match(snapshot, vector, reranker=reranker, **kwargs)] == [
        26173
    ]


@pytest.mark.db
def test_reranker_caches_per_profile_and_reads_without_a_provider(snapshot, agent_env):
    from inspraak.agents.rerank import Reranker
    from inspraak.llm.base import db_recorder
    from inspraak.llm.fake import FakeProvider
    from inspraak.store import get_organisation

    profile = dict(get_organisation(snapshot, GREENPEACE))
    provider = FakeProvider(recorder=db_recorder(agent_env))
    first = Reranker(snapshot, GREENPEACE, profile, provider)
    judged = first.scores([26173])
    assert first.calls == 1 and 0.4 <= judged[26173]["relevance"] < 0.6
    assert "26173" in provider.prompts[-1][1] or "[26173]" in provider.prompts[-1][1]

    again = Reranker(snapshot, GREENPEACE, profile, provider)
    assert again.scores([26173]) == judged and again.calls == 0 and again.cached == 1
    read_only = Reranker(snapshot, GREENPEACE, profile)  # a page read
    assert read_only.scores([26173]) == judged

    edited = Reranker(snapshot, GREENPEACE, {**profile, "goals": "Fisheries only."}, provider)
    assert edited.hash != first.hash
    assert Reranker(snapshot, GREENPEACE, {**profile, "goals": "x"}).scores([26173]) == {}
    edited.scores([26173])
    assert edited.calls == 1


@pytest.mark.db
def test_reranker_ignores_ids_the_prompt_did_not_give(snapshot, agent_env):
    from inspraak.agents.rerank import Reranker
    from inspraak.llm.fake import FakeProvider
    from inspraak.store import get_organisation

    def handler(prompt, schema, ctx):
        return {
            "items": [
                {"publication": 26173, "relevance": 0.8, "reason": "fits"},
                {"publication": 999, "relevance": 1.0, "reason": "invented"},
            ]
        }

    provider = FakeProvider(handlers={"match_rerank": handler})
    reranker = Reranker(
        snapshot, GREENPEACE, dict(get_organisation(snapshot, GREENPEACE)), provider
    )
    assert set(reranker.scores([26173])) == {26173}
    stored = snapshot.execute("SELECT publication_id FROM match_reranks").fetchall()
    assert [r["publication_id"] for r in stored] == [26173]


@pytest.mark.db
def test_demo_replace_supersedes_only_undecided_runs_without_a_draft(snapshot, agent_env):
    from inspraak.agents import service
    from inspraak.llm.fake import FakeProvider

    options = {"min_score": -1.0, "uncertain_score": 0.99, "top_k": 3}
    old = service.start_run(GREENPEACE, options=options, provider=FakeProvider())
    assert old["stage"] == "go_no_go"
    out = service.start_demo([GREENPEACE], options=options, provider=FakeProvider())
    assert out[0]["skipped"] == "has a run"

    out = service.start_demo([GREENPEACE], options=options, provider=FakeProvider(), replace=True)
    assert out[0]["replaced"] == old["id"] and out[0]["stage"] == "go_no_go"
    previous = service.get_run(old["id"])
    assert previous["status"] == "done" and previous["stage"] == "superseded"
    assert all(i["status"] == "decided" for i in previous["review_items"])
    open_items = service.list_review_items("open", tr_id=GREENPEACE, kind="go_no_go")
    assert [i["run_id"] for i in open_items] == [out[0]["run_id"]]

    # a decided go / no-go is kept
    service.decide(open_items[0]["id"], {"no_go": [PUB]}, provider=FakeProvider())
    again = service.start_demo([GREENPEACE], options=options, provider=FakeProvider(), replace=True)
    assert again[0]["skipped"] == "has a run"


@pytest.mark.db
def test_a_publication_with_a_draft_stays_on_the_list(snapshot):
    from inspraak.store import organisation_vector

    _extra_publications(snapshot)
    vector = organisation_vector(snapshot, GREENPEACE)
    kwargs = {"top_k": 5, "min_score": -1.0, "uncertain_score": 0.99}
    reranker = StubReranker(
        {
            26173: {"relevance": 0.1, "reason": "unrelated"},
            90002: {"relevance": 0.9, "reason": "core subject"},
        }
    )
    out = match(snapshot, vector, reranker=reranker, keep=[26173], **kwargs)
    assert [(c["publication_id"], c.get("kept")) for c in out] == [(90002, None), (26173, "draft")]
    # a draft on a later sibling makes that sibling the card of the initiative
    out = match(snapshot, vector, keep=[90001], **kwargs)
    ids = [c["publication_id"] for c in out]
    assert 90001 in ids and 26173 not in ids
    assert next(c for c in out if c["publication_id"] == 90001)["siblings"] == [26173]
