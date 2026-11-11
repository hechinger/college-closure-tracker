"""Tests for extracting Financial Health tracker closures."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fetch_financial_health_closures import FIELDS, extract_closures, main

DATA = {
    "generated_at": "2026-11-11",
    "schools": {
        "172264": {"unitid": "172264", "institution_name": "Siena Heights University", "state": "Michigan",
                   "cuts": [
                       {"cut_type": "institution_closure", "status": "confirmed",
                        "announcement_date": "2025-06-30", "effective_term": "End of 2026",
                        "source_url": "https://example.org/siena"},
                       {"cut_type": "staff_layoff", "status": "confirmed", "announcement_date": "2025-01-01"},
                   ]},
        "cut-qatar": {"unitid": "cut-qatar", "institution_name": "Abroad", "cuts": [
            {"cut_type": "institution_closure", "status": "confirmed", "announcement_date": "2024-02-15"}]},
    },
}


def test_extract_keeps_closures_with_numeric_unitid():
    rows = extract_closures(DATA)
    assert [(r["unitid"], r["cut_type"]) for r in rows] == [("172264", "institution_closure")]
    assert rows[0]["source_generated_at"] == "2026-11-11"


def test_main_writes_csv(tmp_path):
    src = tmp_path / "cuts.json"
    src.write_text(json.dumps(DATA), encoding="utf-8")
    out = tmp_path / "out.csv"
    assert main(["--json", str(src), "--output", str(out)]) == 0
    with out.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == FIELDS and rows[0]["unitid"] == "172264"


def test_main_refuses_to_overwrite_with_empty_fetch(tmp_path):
    src = tmp_path / "cuts.json"
    src.write_text(json.dumps({"schools": {}}), encoding="utf-8")
    out = tmp_path / "out.csv"
    out.write_text("keep", encoding="utf-8")
    assert main(["--json", str(src), "--output", str(out)]) == 1
    assert out.read_text(encoding="utf-8") == "keep"
