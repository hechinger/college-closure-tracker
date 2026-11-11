"""Tests for the SHEEO-method benchmark."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from benchmark_sheeo_method import parse_rt01_lines, reconcile, sheeo_closures


def _line(opeid8: str, close: str = "", sch_type: str = "3", pgm: str = "05",
          name: str = "Test College", state: str = "TX") -> str:
    """Build a rectype_01 fixed-width line with SHEEO's 1-indexed positions."""
    buf = [" "] * 470

    def put(start: int, text: str) -> None:
        for i, ch in enumerate(text):
            buf[start - 1 + i] = ch

    put(1, "01")
    put(3, opeid8)
    put(12, name)
    put(247, state)
    put(355, pgm)
    put(357, sch_type)
    put(382, close)
    return "".join(buf)


def _event(opeid8: str, year: str, decision: str = "include_institution", review: str = "false", **kw) -> dict:
    row = {"opeid8": opeid8, "opeid6": opeid8[:6], "location_suffix": opeid8[6:], "closed_year": year,
           "closed_date_iso": f"{year}-06-01", "publication_decision": decision,
           "review_required": review, "review_reason": "", "closure_type": "", "school_name": "X", "state": "TX"}
    row.update(kw)
    return row


def test_all_locations_closed_counts():
    rows = parse_rt01_lines([_line("00100200", "20180601"), _line("00100201", "20190301")])
    out = sheeo_closures(rows)
    assert out["001002"]["close_year"] == 2019  # last location's year


def test_one_open_location_drops_opeid6():
    rows = parse_rt01_lines([_line("00100200", "20180601"), _line("00100201", "")])
    assert sheeo_closures(rows) == {}


def test_numbering_gap_drops_opeid6():
    rows = parse_rt01_lines([_line("00100200", "20180601"), _line("00100202", "20180601")])
    assert sheeo_closures(rows) == {}


def test_sch_type_4_excluded():
    rows = parse_rt01_lines([_line("00100200", "20180601", sch_type="4")])
    assert sheeo_closures(rows) == {}


def test_reconcile_labels_disagreements():
    rt01 = parse_rt01_lines([
        _line("00100200", "20180601"),                          # both
        _line("00200300", "20170601"), _line("00200301", ""),   # stale open row -> ours_only
        _line("10300400", "20160601"),                          # satellite OPEID6 -> sheeo_only
    ])
    events = [_event("00100200", "2018"), _event("00200300", "2017")]
    rows, summary = reconcile(rt01, events, 2008, 2023)
    status = {r["opeid6"]: (r["status"], r["reason"]) for r in rows}
    assert status["001002"] == ("both", "")
    assert status["002003"][0] == "ours_only" and "stale" in status["002003"][1]
    assert status["103004"][0] == "sheeo_only" and "satellite" in status["103004"][1]
    assert summary["both"] == 1 and summary["sheeo_associate_plus"] == 2
