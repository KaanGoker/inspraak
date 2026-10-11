"""The privacy guard: no citizen name or text may reach the cache or the database."""

from __future__ import annotations

import json

import pytest

from inspraak.ingest.hys import feedback_row_to_db
from inspraak.privacy import (
    PrivacyError,
    assert_safe,
    is_citizen_row,
    scrub_feedback_page,
    scrub_feedback_row,
    scrub_initiative_detail,
)

NAMES = ("Testa", "Zorblatt", "Contact", "Pearsonic")


def test_names_are_dropped_from_every_row(load_json):
    page = scrub_feedback_page(load_json("hys_feedback_page_raw.json"))
    dumped = json.dumps(page)
    for name in NAMES:
        assert name not in dumped
    for row in page["content"]:
        assert "firstName" not in row and "surname" not in row and "login" not in row


def test_citizen_row_keeps_only_counting_fields(load_json):
    raw = load_json("hys_feedback_page_raw.json")["content"][0]
    assert raw["userType"] == "EU_CITIZEN"
    row = scrub_feedback_row(raw)
    assert row["isCitizen"] is True
    assert "feedback" not in row and "attachments" not in row and "organization" not in row
    assert row["attachmentCount"] == 1
    assert row["country"] == raw["country"] and row["userType"] == "EU_CITIZEN"
    assert_safe(row)


def test_organisation_row_keeps_text_and_register_id(load_json):
    raw = load_json("hys_feedback_page_raw.json")["content"][1]
    row = scrub_feedback_row(raw)
    assert row["isCitizen"] is False
    assert row["feedback"].startswith("Our organisation welcomes")
    assert row["trNumber"] == "9832909575-41"
    assert row["attachments"][0]["documentId"] == "0900000000000002"
    assert "firstName" not in row


def test_unknown_type_and_missing_organisation_fail_closed(load_json):
    rows = load_json("hys_feedback_page_raw.json")["content"]
    unknown, no_org = rows[3], rows[4]
    assert is_citizen_row(unknown) and is_citizen_row(no_org)
    for raw in (unknown, no_org):
        row = scrub_feedback_row(raw)
        assert row["isCitizen"] is True
        assert "feedback" not in row


def test_assert_safe_rejects_leaks():
    with pytest.raises(PrivacyError, match="personal keys"):
        assert_safe({"isCitizen": False, "firstName": "x"})
    with pytest.raises(PrivacyError, match="not scrubbed"):
        assert_safe({"userType": "NGO"})
    with pytest.raises(PrivacyError, match="carries content"):
        assert_safe({"isCitizen": True, "feedback": "text"})


def test_db_row_for_citizen_has_no_content(load_json):
    page = scrub_feedback_page(load_json("hys_feedback_page_raw.json"))
    rows = [feedback_row_to_db(r, 26173) for r in page["content"]]
    citizens = [r for r in rows if r["is_citizen"]]
    assert len(citizens) == 3
    for r in citizens:
        assert r["feedback_text"] is None and r["feedback_text_en"] is None
        assert r["organization"] is None and r["tr_number"] is None
        assert r["attachments"] == []
        assert r["attachment_count"] in (0, 1)
    assert not any(k in ("first_name", "surname", "login") for r in rows for k in r)


def test_db_row_refuses_unscrubbed_input(load_json):
    raw = load_json("hys_feedback_page_raw.json")["content"][1]
    with pytest.raises(PrivacyError):
        feedback_row_to_db(raw, 26173)


def test_initiative_detail_drops_staff_contact(load_json):
    detail = load_json("hys_detail_19418.json")
    detail["publications"][0]["authorMail"] = "someone@ec.europa.eu"
    out = scrub_initiative_detail(detail)
    assert "authorMail" not in out["publications"][0]
    assert out["publications"][0]["id"] == 26173


# --------------------------------------------------------------------------- display


def test_long_digit_runs_are_masked_in_names():
    from inspraak.privacy import mask_long_digits

    assert mask_long_digits("Sklep NIP 7743242069") == "Sklep NIP [number removed]"
    assert mask_long_digits("Firma 971-051-03-99 sp.") == "Firma [number removed] sp."
    assert mask_long_digits("Shop 09-500 Gostynin") == "Shop 09-500 Gostynin"  # postcode, 5 digits
    assert mask_long_digits("Agora 2030") == "Agora 2030"
    assert mask_long_digits(None) == ""


def test_unregistered_micro_companies_are_shown_by_size_and_country():
    from inspraak.privacy import display_submitter

    name = "sklep firmowy agnieszka dzikowska 09-500Gostynin czapskiego 8 9710510399"
    assert (
        display_submitter(name, tr_number=None, company_size="MICRO", country="POL")
        == "Micro company, POL"
    )
    assert display_submitter(name, tr_number="", company_size="micro", country=None) == (
        "Micro company"
    )
    # a registered micro organisation keeps its name, numbers masked
    assert (
        display_submitter("Tiny NGO 12345678", tr_number="1-2", company_size="MICRO", country="BEL")
        == "Tiny NGO [number removed]"
    )
    # a larger company without a register number keeps its name
    assert (
        display_submitter("Acme SA", tr_number=None, company_size="LARGE", country="FRA")
        == "Acme SA"
    )
    assert (
        display_submitter(
            None, tr_number="1-2", company_size=None, country=None, register_name="Registered Name"
        )
        == "Registered Name"
    )


@pytest.mark.db
def test_consultation_page_hides_micro_company_names(snapshot, agent_env):
    """An analysed submission from an unregistered micro company reaches the page as
    "Micro company, <country>", with its stance and asks; the typed name never does."""
    from inspraak.web.views import consultation

    pub = 26173
    snapshot.execute(
        "INSERT INTO feedback (id, publication_id, user_type, is_citizen, country, language, "
        "organization, company_size, feedback_text) VALUES "
        "(990001, %s, 'COMPANY', false, 'POL', 'PL', "
        "'Sklep Jan Przykladowy NIP 7743242069', 'MICRO', 'Prosimy o zmiany.'), "
        "(990002, %s, 'COMPANY', false, 'DEU', 'DE', "
        "'Beispiel GmbH 123456789', 'SMALL', 'Bitte aendern.')",
        (pub, pub),
    )
    for fid, source in ((990001, "text"), (990002, "text_en")):
        snapshot.execute(
            "INSERT INTO feedback_analyses (feedback_id, publication_id, provider, model, "
            "stance, confidence, key_asks, source) VALUES (%s, %s, 'fake', 'fake', "
            "'wants_changes', 0.9, %s, %s)",
            (fid, pub, '[{"ask": "change it", "quote": "Prosimy"}]', source),
        )
    snapshot.commit()
    data = consultation(snapshot, pub)
    names = {s["feedback_id"]: s for s in data["landscape"]["submissions"]}
    assert names[990001]["organization"] == "Micro company, POL"
    assert names[990002]["organization"] == "Beispiel GmbH [number removed]"
    assert names[990002]["translated_from"] == "DE"
    assert names[990001]["translated_from"] is None

    from inspraak.web.render import Renderer

    html = Renderer("static").render("consultation.html", rel="../", title="t", **data)
    assert "Przykladowy" not in html and "7743242069" not in html and "123456789" not in html
    assert "Micro company, POL" in html
    assert "machine translation by the Commission, original in DE" in html
