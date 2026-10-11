from __future__ import annotations

from datetime import date

import pytest

from inspraak.ingest.meetings import (
    MeetingsFormatError,
    parse_meetings,
    split_names,
    split_tr_ids,
)


def test_parse_fixture(fixtures):
    file_date, rows = parse_meetings(fixtures / "meetings_small.xlsx")
    assert file_date == date(2026, 10, 2)
    assert len(rows) == 23  # 23 data rows; the portal counts an identical row twice too
    copies = [r for r in rows if r["id"].endswith("-2")]
    assert len(copies) == 1 and copies[0]["id"][:20] in {r["id"] for r in rows}
    first = rows[0]
    assert first["cabinet"].startswith("Cabinet of")
    assert isinstance(first["meeting_date"], date)
    assert first["subject"]
    assert len(first["id"]) == 20 or first["id"].endswith("-2")
    multi = [r for r in rows if len(r["orgs"]) > 1]
    assert len(multi) >= 3
    for m in multi:
        ids = [tr for tr, _ in m["orgs"]]
        assert len(ids) == len(set(ids))
        assert all(split_tr_ids(tr) == [tr] for tr in ids)


def test_split_tr_ids_ignores_junk():
    assert split_tr_ids("79239225070-38, 9832909575-41") == ["79239225070-38", "9832909575-41"]
    assert split_tr_ids("n/a") == []
    assert split_tr_ids(None) == []


def test_split_names_only_when_unambiguous():
    assert split_names("A, B", 2) == ["A", "B"]
    assert split_names("Atlantic Council of the United States, Inc, Other", 2) == [None, None]
    assert split_names("Single Org", 1) == ["Single Org"]
    assert split_names(None, 2) == [None, None]


def test_missing_header_is_an_error(tmp_path):
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.append(["wrong", "header"])
    path = tmp_path / "bad.xlsx"
    wb.save(path)
    with pytest.raises(MeetingsFormatError):
        parse_meetings(path)
