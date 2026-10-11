"""Verbatim quote verification and the critic's sentence handling."""

from __future__ import annotations

from inspraak.agents.nodes import _normalise_go, _normalise_review, cut_to_limit, split_sentences
from inspraak.jobs.quotes import quote_found, verify_quotes

SOURCE = (
    "We welcome the proposal.  The Commission should   keep the 2030 target\n"
    "unchanged and “avoid new reporting duties” for small firms."
)


def test_quote_found_tolerates_whitespace_and_typographic_quotes():
    assert quote_found("The Commission should keep the 2030 target unchanged", SOURCE)
    assert quote_found('"avoid new reporting duties" for small firms', SOURCE)
    assert quote_found("the commission SHOULD keep the 2030 target", SOURCE)
    assert not quote_found("keep the 2035 target unchanged", SOURCE)
    assert not quote_found("We welcome", SOURCE)  # too short to count as a verbatim quote


def test_verify_quotes_drops_invented_and_counts():
    asks = [
        {"ask": "Keep the target", "quote": "keep the 2030 target unchanged"},
        {"ask": "Invented", "quote": "abolish the target entirely by 2027"},
        {"ask": "Empty", "quote": ""},
    ]
    kept, dropped = verify_quotes(asks, SOURCE)
    assert [k["ask"] for k in kept] == ["Keep the target"]
    assert kept[0]["verified"] is True and dropped == 2


def test_sentence_split_and_hard_limit():
    text = "First one. Second one! Third? Fourth."
    assert split_sentences(text) == ["First one.", "Second one!", "Third?", "Fourth."]
    long = ("A sentence that is long enough. " * 200).strip()
    cut = cut_to_limit(long, 4000)
    assert len(cut) <= 4000 and cut.endswith(".")
    assert cut_to_limit("short", 4000) == "short"


def test_decision_normalisation():
    go = _normalise_go({"go": [1, "2"], "not_our_topic": [3, 99]}, [1, 2, 3, 4])
    assert go == {"go": [1, 2], "no_go": [4], "not_our_topic": [3], "decided_by": None}
    review = _normalise_review(
        {
            "items": {"1": {"action": "approve"}, 2: {"action": "edit", "text": "x"}},
            "decided_by": "k",
        },
        ["1", "2", "3"],
    )
    assert review["items"]["1"]["action"] == "approve"
    assert review["items"]["2"] == {"action": "edit", "text": "x", "note": None}
    assert review["items"]["3"]["action"] == "reject"
    assert review["decided_by"] == "k"


def test_split_sentences_keeps_legal_abbreviations_inside_the_sentence():
    text = (
        "Under Art. 13 of Directive (EU) 2016/680, data subjects have a right to notice. "
        "We back this, e.g. for profiling. No. 5 matters. See p. 4 and i.e. the annex. "
        "Mr. Smith agrees. It ends here."
    )
    assert split_sentences(text) == [
        "Under Art. 13 of Directive (EU) 2016/680, data subjects have a right to notice.",
        "We back this, e.g. for profiling.",
        "No. 5 matters.",
        "See p. 4 and i.e. the annex.",
        "Mr. Smith agrees.",
        "It ends here.",
    ]


def test_quote_with_a_long_dash_survives_the_house_dash_filter():
    from inspraak.jobs.quotes import quote_found
    from inspraak.llm.base import filter_strings

    dash = chr(0x2013)
    quote = filter_strings(f"reach 55% by 2020{dash}2030 across all")
    source = f"The Commission should reach 55% by 2020{dash}2030 across all Member States."
    assert quote_found(quote, source)


def test_evaluative_wording_is_recognised():
    from inspraak.agents.nodes import is_evaluative

    assert is_evaluative("We strongly support the proposal.")
    assert is_evaluative("The costs are vastly outweighed by the benefits.")
    assert is_evaluative("EDRi welcomes the initiative.")
    assert not is_evaluative("Article 5 sets a reporting duty for operators.")
    assert not is_evaluative("BEUC welcomes the opportunity to respond.")


def test_chunk_markers_are_cut_from_draft_text():
    from inspraak.textutil import strip_chunk_markers

    text = (
        "It builds on Regulation (EU) 2024/1689 [chunk:2770], as notified [chunk:2763]. "
        "Systems must be proportionate [chunk:2849, chunk:2988]. See (chunk:12 p.4) and chunk:7."
    )
    out = strip_chunk_markers(text)
    assert "chunk" not in out.lower()
    assert out == (
        "It builds on Regulation (EU) 2024/1689, as notified. "
        "Systems must be proportionate. See and."
    )
    assert strip_chunk_markers("No marker here.") == "No marker here."


def test_normative_wording_is_recognised():
    from inspraak.agents.nodes import is_normative

    assert is_normative("We support a ban on mandatory age checks.")
    assert is_normative("We call for a review clause.")
    assert is_normative("Public spending must serve broader societal goals.")
    assert is_normative("The Commission should drop Article 6.")
    assert is_normative("BEUC welcomes the proposal.", ("BEUC", "Bureau"))
    assert not is_normative("Article 5 sets a reporting duty for operators.")
    assert not is_normative("Under the proposal, providers must verify the age of users.")
    assert not is_normative("The regulation requires that platforms must publish reports.")
    assert not is_normative("BEUC welcomes the opportunity to respond.", ("BEUC",))


def test_critic_needs_an_organisation_source_for_a_position():
    """A normative sentence backed only by a Commission passage is removed; the
    organisation's past submission or register goals make it the organisation's."""
    from inspraak.agents.nodes import judge_sentence, parse_sources

    known = {"chunk:1", "chunk:2", "submission:99", "profile"}
    must = "Public spending must serve broader societal goals."
    assert parse_sources("chunk:1, profile") == ["chunk:1", "profile"]
    assert parse_sources("Chunk: 2; submission:99") == ["chunk:2", "submission:99"]

    v = judge_sentence(must, ["chunk:1"], known, True)
    assert not v["supported"] and "Commission passage" in v["note"]
    assert judge_sentence(must, ["chunk:1", "submission:99"], known, True)["supported"]
    assert judge_sentence(must, ["chunk:1", "profile"], known, True)["supported"]
    # a submission the critic was not shown does not count
    assert not judge_sentence(must, ["chunk:1", "submission:5"], known, True)["supported"]

    # a plain description of the proposal may rest on the passage alone
    fact = "Article 5 sets a reporting duty for operators."
    assert judge_sentence(fact, ["chunk:2"], known, True)["supported"]
    # a stance verb with the organisation as subject is a position too
    welcome = "EDRi welcomes the proposal."
    assert not judge_sentence(welcome, ["chunk:2"], known, True, subjects=("EDRi",))["supported"]
    # an evaluation still needs more than the register profile
    assert not judge_sentence(welcome, ["profile"], known, True, subjects=("EDRi",))["supported"]
    assert judge_sentence(welcome, ["submission:99"], known, True, subjects=("EDRi",))["supported"]
    # the model's own "not supported" stands
    assert not judge_sentence(fact, ["chunk:2"], known, False)["supported"]
