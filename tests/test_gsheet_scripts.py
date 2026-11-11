"""Tests for push_to_gsheet.py and pull_gsheet_overrides.py.

Uses unittest.mock to avoid any real network calls.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import push_to_gsheet as pgs
import pull_gsheet_overrides as pgso


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _write_events_csv(tmp_path: Path, rows: list[dict]) -> Path:
    """Write a minimal fsa_closure_events_*.csv for testing."""
    all_cols = pgs._EDITORIAL_COLS + [
        c for c in pgs._PUBLIC_COLS if c not in pgs._EDITORIAL_COLS
    ]
    p = tmp_path / "fsa_closure_events_2026-01-01.csv"
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=all_cols)
        w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in all_cols})
    return p


def _sample_events():
    return [
        {
            "opeid8": "00100200", "school_name": "Alpha College",
            "city": "Boston", "state": "MA", "control_label": "public",
            "institution_category": "public_4yr", "enrollment_band": "1000-4999",
            "enrollment_headcount_total": "2000", "closed_date_iso": "2020-06-01",
            "closed_year": "2020", "approach2_main_campus_only": "true",
            "is_traditional_college": "true",
            "closure_type": "institutional_closure", "closure_type_basis": "auto",
        },
        {
            "opeid8": "00200300", "school_name": "Beta Institute",
            "city": "Denver", "state": "CO", "control_label": "for-profit",
            "institution_category": "for_profit_4yr", "enrollment_band": "under 1,000",
            "enrollment_headcount_total": "400", "closed_date_iso": "2018-05-01",
            "closed_year": "2018", "approach2_main_campus_only": "true",
            "is_traditional_college": "false",
            "closure_type": "for_profit_closure", "closure_type_basis": "auto",
        },
    ]


# ---------------------------------------------------------------------------
# Tests for push_to_gsheet._load_rows
# ---------------------------------------------------------------------------

class TestLoadRows:
    def test_returns_header_plus_rows(self, tmp_path):
        _write_events_csv(tmp_path, _sample_events())
        p = tmp_path / "fsa_closure_events_2026-01-01.csv"
        cols = ["opeid8", "school_name", "closed_date_iso"]
        result = pgs._load_rows(p, cols)
        assert result[0] == cols
        assert len(result) == 3  # header + 2 rows

    def test_sorted_newest_first(self, tmp_path):
        _write_events_csv(tmp_path, _sample_events())
        p = tmp_path / "fsa_closure_events_2026-01-01.csv"
        cols = ["opeid8", "closed_date_iso"]
        result = pgs._load_rows(p, cols)
        dates = [row[1] for row in result[1:]]
        assert dates == sorted(dates, reverse=True)

    def test_missing_column_gives_empty_string(self, tmp_path):
        _write_events_csv(tmp_path, _sample_events())
        p = tmp_path / "fsa_closure_events_2026-01-01.csv"
        cols = ["opeid8", "nonexistent_col"]
        result = pgs._load_rows(p, cols)
        assert all(row[1] == "" for row in result[1:])


class TestLatestEventsCsv:
    def test_picks_latest_by_name(self, tmp_path):
        (tmp_path / "fsa_closure_events_2024-01-01.csv").write_text(
            "opeid8\n00001\n", encoding="utf-8"
        )
        (tmp_path / "fsa_closure_events_2026-01-01.csv").write_text(
            "opeid8\n00002\n", encoding="utf-8"
        )
        with patch.object(pgs, "DERIVED", tmp_path):
            p = pgs._latest_events_csv()
        assert "2026" in p.name

    def test_raises_when_no_csv(self, tmp_path):
        with patch.object(pgs, "DERIVED", tmp_path):
            with pytest.raises(FileNotFoundError):
                pgs._latest_events_csv()


# ---------------------------------------------------------------------------
# Tests for push_to_gsheet tab columns
# ---------------------------------------------------------------------------

class TestTabColumns:
    def test_closure_list_includes_closure_type_basis(self):
        assert "closure_type_basis" in pgs.TAB_COLUMNS["closure_list"]

    def test_published_does_not_include_closure_type_basis(self):
        assert "closure_type_basis" not in pgs.TAB_COLUMNS["published"]

    def test_both_tabs_include_opeid8(self):
        for cols in pgs.TAB_COLUMNS.values():
            assert "opeid8" in cols


# ---------------------------------------------------------------------------
# Tests for push_to_gsheet.push (mocked gspread)
# ---------------------------------------------------------------------------

class TestPush:
    def _make_mock_sheet(self, existing_opeid8s=None):
        ws = MagicMock()
        ws.col_values.return_value = ["opeid8"] + list(existing_opeid8s or [])
        sh = MagicMock()
        sh.worksheet.return_value = ws
        gc = MagicMock()
        gc.open_by_key.return_value = sh
        return gc, sh, ws

    def test_closure_list_counts_new(self, tmp_path):
        _write_events_csv(tmp_path, _sample_events())

        gc, sh, ws = self._make_mock_sheet(existing_opeid8s=["00100200"])
        with patch.object(pgs, "DERIVED", tmp_path), \
             patch("push_to_gsheet._get_gspread_client", return_value=gc):
            count = pgs.push("FAKE_SHEET_ID", "closure_list")

        assert count == 1  # 00200300 is new; 00100200 already existed

    def test_published_always_returns_zero(self, tmp_path):
        _write_events_csv(tmp_path, _sample_events())

        gc, sh, ws = self._make_mock_sheet()
        with patch.object(pgs, "DERIVED", tmp_path), \
             patch("push_to_gsheet._get_gspread_client", return_value=gc):
            count = pgs.push("FAKE_SHEET_ID", "published")

        assert count == 0

    def test_clear_and_update_called(self, tmp_path):
        _write_events_csv(tmp_path, _sample_events())

        gc, sh, ws = self._make_mock_sheet()
        with patch.object(pgs, "DERIVED", tmp_path), \
             patch("push_to_gsheet._get_gspread_client", return_value=gc):
            pgs.push("FAKE_SHEET_ID", "closure_list")

        ws.clear.assert_called_once()
        ws.update.assert_called_once()


# ---------------------------------------------------------------------------
# Tests for pull_gsheet_overrides._validate_rows
# ---------------------------------------------------------------------------

class TestValidateRows:
    def test_valid_row_passes(self):
        rows = [{"opeid8": "00219300", "closure_type": "merger_absorption",
                 "closure_type_basis": "Editorial", "editor_note": "Mount Ida"}]
        valid, errors = pgso._validate_rows(rows)
        assert len(valid) == 1
        assert errors == 0

    def test_empty_opeid8_skipped(self):
        rows = [{"opeid8": "", "closure_type": "merger_absorption",
                 "closure_type_basis": "x", "editor_note": ""}]
        valid, errors = pgso._validate_rows(rows)
        assert len(valid) == 0
        assert errors == 1

    def test_empty_closure_type_skipped(self):
        rows = [{"opeid8": "00219300", "closure_type": "",
                 "closure_type_basis": "x", "editor_note": ""}]
        valid, errors = pgso._validate_rows(rows)
        assert errors == 1

    def test_unknown_closure_type_skipped(self):
        rows = [{"opeid8": "00219300", "closure_type": "banana",
                 "closure_type_basis": "x", "editor_note": ""}]
        valid, errors = pgso._validate_rows(rows)
        assert errors == 1

    def test_all_valid_event_types_accepted(self):
        from classify_closure_events import EVENT_TYPES
        rows = [{"opeid8": f"0000000{i}", "closure_type": et,
                 "closure_type_basis": "x", "editor_note": ""}
                for i, et in enumerate(EVENT_TYPES)]
        valid, errors = pgso._validate_rows(rows)
        assert len(valid) == len(EVENT_TYPES)
        assert errors == 0

    def test_mixed_valid_and_invalid(self):
        rows = [
            {"opeid8": "00219300", "closure_type": "merger_absorption",
             "closure_type_basis": "x", "editor_note": ""},
            {"opeid8": "", "closure_type": "institutional_closure",
             "closure_type_basis": "x", "editor_note": ""},
        ]
        valid, errors = pgso._validate_rows(rows)
        assert len(valid) == 1
        assert errors == 1

    def test_output_has_all_fields(self):
        rows = [{"opeid8": "00219300", "closure_type": "merger_absorption",
                 "closure_type_basis": "Editorial: merged", "editor_note": "Mount Ida"}]
        valid, _ = pgso._validate_rows(rows)
        assert set(valid[0].keys()) == {"opeid8", "closure_type", "closure_type_basis", "editor_note"}


# ---------------------------------------------------------------------------
# Tests for pull_gsheet_overrides._write_csv
# ---------------------------------------------------------------------------

class TestWriteCsv:
    def test_writes_expected_rows(self, tmp_path):
        rows = [{"opeid8": "00219300", "closure_type": "merger_absorption",
                 "closure_type_basis": "Editorial", "editor_note": "Mount Ida"}]
        out = tmp_path / "overrides.csv"
        pgso._write_csv(rows, out)
        with out.open(encoding="utf-8") as fh:
            reader = list(csv.DictReader(fh))
        assert len(reader) == 1
        assert reader[0]["opeid8"] == "00219300"

    def test_creates_parent_directory(self, tmp_path):
        out = tmp_path / "subdir" / "overrides.csv"
        pgso._write_csv([], out)
        assert out.exists()
