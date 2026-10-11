from __future__ import annotations

import io

import pytest
from lxml import etree

from inspraak.ingest.register import (
    RegisterIncomplete,
    SanitisedXML,
    build_profile_text,
    iter_register,
    sanitise_bytes,
)


def _parse(path):
    meta = done = None
    orgs = []
    for kind, payload in iter_register(path):
        if kind == "meta":
            meta = payload
        elif kind == "org":
            orgs.append(payload)
        elif kind == "done":
            done = payload
    return meta, orgs, done


def test_sanitise_keeps_entities_and_drops_control_refs():
    raw = b"<?xml version='1.1' encoding='UTF-8'?><a>x &amp; y&#x2;&#xb;&#13;&#x96;</a>"
    fixed = sanitise_bytes(raw)
    assert fixed.startswith(b"<?xml version='1.0'")
    assert b"&amp;" in fixed
    assert b"&#x2;" not in fixed and b"&#xb;" not in fixed
    assert b"&#13;" in fixed and b"&#x96;" in fixed  # valid references stay
    assert etree.fromstring(fixed).text == "x & y  \r\x96"


def test_sanitised_stream_handles_reference_split_across_blocks():
    raw = b"<a>" + b"p" * 1000 + b"&#x2;" + b"q" * 1000 + b"&amp;</a>"
    stream = SanitisedXML(io.BytesIO(raw), block_size=1003)  # cuts inside &#x2;
    out = stream.read()
    assert out == sanitise_bytes(raw)
    assert stream.replaced == 1


def test_fixture_parses_strictly_with_ampersands(fixtures):
    meta, orgs, done = _parse(fixtures / "register_small.xml")
    assert meta["number_of_ir"] == 3
    assert meta["export_date"].year == 2026
    assert len(orgs) == 3
    assert done["char_refs_replaced"] == 2
    by_id = {o["tr_id"]: o for o in orgs}
    assert by_id["763743132433-49"]["category"] == "Companies & groups"
    crafted = by_id["000000000000-01"]
    assert crafted["name"] == "Test & Trial vzw"
    assert crafted["goals"].startswith("Research on consultation & participation.")
    assert crafted["interests"] == ["Democracy", "Digital economy and society"]
    assert crafted["head_country"] == "BELGIUM"


def test_greenpeace_record_fields(fixtures):
    _, orgs, _ = _parse(fixtures / "register_small.xml")
    gp = next(o for o in orgs if o["tr_id"] == "9832909575-41")
    assert gp["name"] == "Greenpeace European Unit"
    assert gp["acronym"] == "GPEU"
    assert gp["category"].startswith("Non-governmental")
    assert gp["head_city"] == "Brussels"
    assert "Climate action" in gp["interests"]
    assert gp["total_budget"] == 2160000.0
    assert gp["ep_accredited"] == 2
    assert gp["financial"]["closedYear"]["totalBudget"]["absoluteCost"] == "2160000"
    text = build_profile_text(gp)
    assert text.splitlines()[0] == "Greenpeace European Unit (GPEU)"
    assert "Interests:" in text


def test_incomplete_download_is_refused(fixtures, tmp_path):
    data = (fixtures / "register_small.xml").read_bytes()
    cut = data.replace(b"<numberOfIR>3</numberOfIR>", b"<numberOfIR>4</numberOfIR>")
    path = tmp_path / "register.xml"
    path.write_bytes(cut)

    class FakeConn:
        def cursor(self):
            raise AssertionError("must not reach the database")

    from inspraak.ingest.register import load_register

    with pytest.raises(RegisterIncomplete, match="declares 4"):
        load_register(FakeConn(), path)
