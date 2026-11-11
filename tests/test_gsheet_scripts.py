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

def _all_event_cols() -> list[str]:
    """Union of editorial and public cols so the test CSV covers both."""
    seen: list[str] = []
    for c in pgs._EDITORIAL_COLS + pgs._PUBLIC_COLS:
        if c not in seen:
            seen.append(c)
    # extra internal fields needed by _filter_published_rows
    for c in ["iclevel", "approach2_main_campus_only", "is_suspicious_location",
              "match_method", "closure_type_basis", "is_main_campus",
              "is_traditional_college"]:
        if c not in seen:
            seen.append(c)
    return seen


def _write_events_csv(tmp_path: Path, rows: list[dict]) -> Path:
    all_cols = _all_event_cols()
    p = tmp_path / "fsa_closure_events_2026-01-01.csv"
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=all_cols)
        w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in all_cols})
    return p


def _inst_event(**kw) -> dict:
    """Minimal institution-closure event (approach2 main campus)."""
    defaults = {
        "opeid8": "00100200", "opeid6": "001002",
        "school_name": "Alpha College", "city": "Boston", "state": "MA",
        "control_label": "public", "iclevel": "1",
        "iclevel_label": "4yr+", "institution_category": "public_4yr",
        "program_level": "4-year or above", "credential_level": "Bachelor's",
        "enrollment_band": "1,000-4,999", "enrollment_headcount_total": "2000",
        "closed_date_iso": "2020-06-01", "closed_year": "2020",
        "approach2_main_campus_only": "true", "is_main_campus": "true",
        "is_suspicious_location": "false", "is_traditional_college": "true",
        "closure_type": "institutional_closure",
        "closure_type_basis": "main campus; public nonprofit",
        "match_method": "matched_opeid8_yr2019",
        "unitid": "111111",
    }
    defaults.update(kw)
    return defaults


def _campus_event(**kw) -> dict:
    """Minimal campus-closure event (branch with IPEDS coverage, parent continuing)."""
    defaults = {
        "opeid8": "00100201", "opeid6": "001002",
        "school_name": "Alpha College West", "city": "Worcester", "state": "MA",
        "control_label": "public", "iclevel": "1",
        "iclevel_label": "4yr+", "institution_category": "public_4yr",
        "program_level": "4-year or above", "credential_level": "Bachelor's",
        "enrollment_band": "Under 1,000", "enrollment_headcount_total": "400",
        "closed_date_iso": "2019-05-01", "closed_year": "2019",
        "approach2_main_campus_only": "false", "is_main_campus": "false",
        "is_suspicious_location": "false", "is_traditional_college": "true",
        "closure_type": "branch_closure",
        "closure_type_basis": "OPEID suffix != 00",
        "match_method": "matched_opeid8_yr2018",
        "unitid": "111112",
    }
    defaults.update(kw)
    return defaults


def _sample_events() -> list[dict]:
    return [
        _inst_event(opeid8="00100200", opeid6="001002",
                    school_name="Alpha College", closed_date_iso="2020-06-01",
                    closed_year="2020"),
        _inst_event(opeid8="00200300", opeid6="002003",
                    school_name="Beta Institute", state="CO",
                    control_label="for-profit", iclevel="1",
                    institution_category="for_profit_4yr",
                    is_traditional_college="false",
                    closure_type="for_profit_closure",
                    closed_date_iso="2018-05-01", closed_year="2018"),
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

    def test_min_year_filters_older_rows(self, tmp_path):
        _write_events_csv(tmp_path, _sample_events())
        p = tmp_path / "fsa_closure_events_2026-01-01.csv"
        cols = ["opeid8", "closed_year"]
        result = pgs._load_rows(p, cols, min_year=2019)
        years = [row[1] for row in result[1:]]
        assert all(int(y) >= 2019 for y in years if y)

    def test_min_year_none_keeps_all(self, tmp_path):
        _write_events_csv(tmp_path, _sample_events())
        p = tmp_path / "fsa_closure_events_2026-01-01.csv"
        cols = ["opeid8"]
        assert len(pgs._load_rows(p, cols, min_year=None)) == 3  # header + 2

    def test_exclude_types_removes_matching_rows(self, tmp_path):
        rows = _sample_events() + [
            _inst_event(opeid8="99999900", opeid6="999999",
                        closure_type="location_administrative",
                        closed_year="2020", closed_date_iso="2020-01-01"),
        ]
        _write_events_csv(tmp_path, rows)
        p = tmp_path / "fsa_closure_events_2026-01-01.csv"
        result = pgs._load_rows(
            p, ["opeid8", "closure_type"],
            exclude_types=frozenset({"location_administrative"})
        )
        types = [row[1] for row in result[1:]]
        assert "location_administrative" not in types
        assert len(result) == 3  # header + 2 remaining rows


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
# Tests for tab column definitions
# ---------------------------------------------------------------------------

class TestTabColumns:
    def test_closure_list_includes_closure_type_basis(self):
        assert "closure_type_basis" in pgs.TAB_COLUMNS["closure_list"]

    def test_published_does_not_include_closure_type_basis(self):
        assert "closure_type_basis" not in pgs.TAB_COLUMNS["published"]

    def test_published_has_classification_basis_instead(self):
        assert "classification_basis" in pgs.TAB_COLUMNS["published"]

    def test_both_tabs_include_opeid8(self):
        for cols in pgs.TAB_COLUMNS.values():
            assert "opeid8" in cols

    def test_published_has_program_level_not_iclevel_label(self):
        pub = pgs.TAB_COLUMNS["published"]
        assert "program_level" in pub
        assert "iclevel_label" not in pub

    def test_closure_list_has_iclevel_label_for_auditing(self):
        assert "iclevel_label" in pgs.TAB_COLUMNS["closure_list"]


# ---------------------------------------------------------------------------
# Tests for published schema
# ---------------------------------------------------------------------------

class TestPublishedSchema:
    def test_exact_column_count(self):
        assert len(pgs._PUBLIC_COLS) == 23

    def test_first_12_columns_in_order(self):
        expected = [
            "opeid8", "school_name", "city", "state",
            "control_label", "program_level", "credential_level",
            "enrollment_band", "enrollment_headcount_total",
            "closed_date_iso", "closed_year", "closure_type",
        ]
        assert pgs._PUBLIC_COLS[:12] == expected

    def test_new_metadata_columns_appended(self):
        new_cols = pgs._PUBLIC_COLS[12:]
        assert "closure_scope" in new_cols
        assert "parent_institution_continued" in new_cols
        assert "closure_event_id" in new_cols
        assert "sheeo_approach1_include" in new_cols
        assert "sheeo_approach2_include" in new_cols
        assert "sheeo_approach3_include" in new_cols
        assert "classification_basis" in new_cols
        assert "include_institution_totals" in new_cols
        assert "include_campus_totals" in new_cols
        assert "opeid6" in new_cols
        assert "unitid" in new_cols

    def test_no_blank_or_unnamed_columns(self):
        for col in pgs._PUBLIC_COLS:
            assert col and not col.startswith("unnamed"), f"Bad column: {col!r}"


# ---------------------------------------------------------------------------
# Tests for _filter_published_rows (pure function)
# ---------------------------------------------------------------------------

class TestFilterPublishedRows:
    # --- institution scope ---

    def test_institutional_closure_becomes_institution_scope(self):
        rows = [_inst_event(closure_type="institutional_closure")]
        result = pgs._filter_published_rows(rows)
        assert len(result) == 1
        assert result[0]["closure_scope"] == "institution"

    def test_system_consolidation_becomes_institution_scope(self):
        rows = [_inst_event(closure_type="system_consolidation")]
        result = pgs._filter_published_rows(rows)
        assert len(result) == 1
        assert result[0]["closure_scope"] == "institution"

    def test_for_profit_closure_becomes_institution_scope(self):
        rows = [_inst_event(closure_type="for_profit_closure",
                            is_traditional_college="false",
                            control_label="for-profit")]
        result = pgs._filter_published_rows(rows)
        assert len(result) == 1
        assert result[0]["closure_scope"] == "institution"

    def test_institution_row_published_closure_type_normalized(self):
        rows = [_inst_event(closure_type="for_profit_closure")]
        result = pgs._filter_published_rows(rows)
        assert result[0]["closure_type"] == "institution_closure"

    def test_institution_flags(self):
        rows = [_inst_event()]
        r = pgs._filter_published_rows(rows)[0]
        assert r["parent_institution_continued"] == "false"
        assert r["include_institution_totals"] == "true"
        assert r["include_campus_totals"] == "false"
        assert r["sheeo_approach2_include"] == "true"

    def test_institution_event_id_format(self):
        rows = [_inst_event(opeid6="001002", closed_date_iso="2020-06-01")]
        r = pgs._filter_published_rows(rows)[0]
        assert r["closure_event_id"] == "institution:001002:2020-06-01"

    # --- campus scope ---

    def test_branch_with_ipeds_match_and_postsecondary_becomes_campus(self):
        parent = _inst_event(opeid8="00500000", opeid6="005000",
                             closed_date_iso="2021-01-01", closed_year="2021")
        # Different OPEID6 → parent not in closed_main_opeid6s
        branch = _campus_event(opeid8="00600001", opeid6="006000",
                               match_method="matched_opeid8_yr2018", iclevel="2")
        result = pgs._filter_published_rows([parent, branch])
        scopes = {r["opeid8"]: r["closure_scope"] for r in result}
        assert scopes.get("00600001") == "campus"

    def test_campus_flags(self):
        branch = _campus_event(opeid8="00700001", opeid6="007000",
                               match_method="matched_opeid8_yr2018", iclevel="1")
        r = pgs._filter_published_rows([branch])[0]
        assert r["closure_scope"] == "campus"
        assert r["parent_institution_continued"] == "true"
        assert r["include_institution_totals"] == "false"
        assert r["include_campus_totals"] == "true"
        assert r["sheeo_approach2_include"] == "false"

    def test_campus_event_id_format(self):
        branch = _campus_event(opeid8="00700001", opeid6="007000",
                               closed_date_iso="2019-05-01",
                               match_method="matched_opeid8_yr2018", iclevel="1")
        r = pgs._filter_published_rows([branch])[0]
        assert r["closure_event_id"] == "campus:00700001:2019-05-01"

    def test_campus_published_closure_type_normalized(self):
        branch = _campus_event(opeid8="00700001", opeid6="007000",
                               match_method="matched_opeid8_yr2018", iclevel="1")
        r = pgs._filter_published_rows([branch])[0]
        assert r["closure_type"] == "campus_closure"

    # --- exclusion rules ---

    def test_location_administrative_excluded(self):
        rows = [_inst_event(closure_type="location_administrative",
                            is_suspicious_location="true")]
        assert pgs._filter_published_rows(rows) == []

    def test_unclassified_excluded(self):
        rows = [_inst_event(closure_type="unclassified")]
        assert pgs._filter_published_rows(rows) == []

    def test_merger_absorption_excluded(self):
        rows = [_inst_event(closure_type="merger_absorption")]
        assert pgs._filter_published_rows(rows) == []

    def test_branch_no_ipeds_match_excluded(self):
        branch = _campus_event(match_method="no_match", iclevel="")
        assert pgs._filter_published_rows([branch]) == []

    def test_branch_no_coverage_excluded(self):
        branch = _campus_event(match_method="no_ipeds_coverage", iclevel="")
        assert pgs._filter_published_rows([branch]) == []

    def test_branch_matched_but_no_iclevel_excluded(self):
        branch = _campus_event(match_method="matched_opeid8_yr2018", iclevel="")
        assert pgs._filter_published_rows([branch]) == []

    def test_full_system_shutdown_branch_excluded(self):
        # Parent's main campus also closed → branch should not be a standalone campus event
        parent = _inst_event(opeid8="00100200", opeid6="001002",
                             closure_type="institutional_closure",
                             closed_date_iso="2020-06-01", closed_year="2020")
        branch = _campus_event(opeid8="00100201", opeid6="001002",
                               closure_type="branch_closure",
                               match_method="matched_opeid8_yr2018", iclevel="1",
                               closed_date_iso="2020-04-01", closed_year="2020")
        result = pgs._filter_published_rows([parent, branch])
        # Institution row present; branch row excluded (same OPEID6 as fully closed institution)
        scopes = [r["closure_scope"] for r in result]
        assert "campus" not in scopes
        assert "institution" in scopes

    def test_min_year_filter_applied(self):
        old_row = _inst_event(opeid8="00111100", opeid6="001111",
                              closed_date_iso="1999-06-01", closed_year="1999")
        new_row = _inst_event(opeid8="00222200", opeid6="002222",
                              closed_date_iso="2005-06-01", closed_year="2005")
        result = pgs._filter_published_rows([old_row, new_row], min_year=2000)
        opeid8s = [r["opeid8"] for r in result]
        assert "00111100" not in opeid8s
        assert "00222200" in opeid8s

    # --- Approach flags ---

    def test_all_published_rows_have_sheeo_approach1_true(self):
        inst = _inst_event(opeid8="00111100", opeid6="001111")
        branch = _campus_event(opeid8="00222201", opeid6="002222",
                               match_method="matched_opeid8_yr2018", iclevel="1")
        result = pgs._filter_published_rows([inst, branch])
        assert all(r["sheeo_approach1_include"] == "true" for r in result)

    def test_all_published_rows_have_sheeo_approach3_true(self):
        inst = _inst_event(opeid8="00111100", opeid6="001111")
        branch = _campus_event(opeid8="00222201", opeid6="002222",
                               match_method="matched_opeid8_yr2018", iclevel="1")
        result = pgs._filter_published_rows([inst, branch])
        assert all(r["sheeo_approach3_include"] == "true" for r in result)

    def test_merger_candidate_excluded_from_approach3(self):
        rows = [_inst_event(closure_type="merger_absorption")]
        result = pgs._filter_published_rows(rows)
        assert result == []

    def test_approach3_multiple_campuses_same_opeid6(self):
        # Two campus rows from same OPEID6 — both have approach3=true; consumer counts distinct
        branch1 = _campus_event(opeid8="00800001", opeid6="008000",
                                match_method="matched_opeid8_yr2018", iclevel="1",
                                closed_date_iso="2018-01-01", closed_year="2018")
        branch2 = _campus_event(opeid8="00800002", opeid6="008000",
                                match_method="matched_opeid8_yr2017", iclevel="1",
                                closed_date_iso="2017-06-01", closed_year="2017")
        result = pgs._filter_published_rows([branch1, branch2])
        assert len(result) == 2
        assert all(r["sheeo_approach3_include"] == "true" for r in result)
        # Approach 3 aggregate = 1 (one distinct OPEID6)
        approach3_opeid6s = {r["opeid6"] for r in result if r["sheeo_approach3_include"] == "true"}
        assert len(approach3_opeid6s) == 1

    def test_approach3_full_institution_counts_once(self):
        rows = [_inst_event(opeid8="00111100", opeid6="001111")]
        result = pgs._filter_published_rows(rows)
        approach3_opeid6s = {r["opeid6"] for r in result if r["sheeo_approach3_include"] == "true"}
        assert len(approach3_opeid6s) == 1

    def test_approach3_excluded_record_does_not_qualify(self):
        admin = _inst_event(closure_type="location_administrative")
        uncertain = _inst_event(closure_type="unclassified", opeid8="00222200", opeid6="002222")
        result = pgs._filter_published_rows([admin, uncertain])
        assert result == []

    # --- closure_event_id uniqueness ---

    def test_closure_event_id_unique(self):
        inst = _inst_event(opeid8="00100200", opeid6="001002",
                           closed_date_iso="2020-06-01", closed_year="2020")
        branch = _campus_event(opeid8="00200301", opeid6="002003",
                               match_method="matched_opeid8_yr2019", iclevel="1",
                               closed_date_iso="2019-01-01", closed_year="2019")
        result = pgs._filter_published_rows([inst, branch])
        ids = [r["closure_event_id"] for r in result]
        assert len(ids) == len(set(ids)), "Duplicate closure_event_ids found"

    def test_closure_event_id_stable_across_calls(self):
        rows = [_inst_event(opeid8="00100200", opeid6="001002",
                            closed_date_iso="2020-06-01", closed_year="2020")]
        r1 = pgs._filter_published_rows(rows)[0]["closure_event_id"]
        r2 = pgs._filter_published_rows(rows)[0]["closure_event_id"]
        assert r1 == r2

    def test_all_published_rows_have_non_empty_event_id(self):
        inst = _inst_event()
        branch = _campus_event(opeid8="00700001", opeid6="007000",
                               match_method="matched_opeid8_yr2018", iclevel="1")
        result = pgs._filter_published_rows([inst, branch])
        assert all(r["closure_event_id"] for r in result)


# ---------------------------------------------------------------------------
# Regression tests: named real records must not enter published without evidence
# ---------------------------------------------------------------------------

class TestRegressionNamedRecords:
    """
    10166659 — Concordia University - Maine West High School
    00170764 — Lewis University - Plainfield Consolidate District 202

    Both are branches (non-00 suffix).  They must not appear in published
    output unless structured IPEDS evidence confirms postsecondary identity.
    Name patterns are NOT used for the decision.
    """

    def _branch_no_match(self, opeid8: str, opeid6: str) -> dict:
        return _campus_event(opeid8=opeid8, opeid6=opeid6,
                             match_method="no_match", iclevel="")

    def _branch_matched_postsec(self, opeid8: str, opeid6: str, iclevel: str) -> dict:
        return _campus_event(opeid8=opeid8, opeid6=opeid6,
                             match_method="matched_opeid8_yr2019",
                             iclevel=iclevel)

    def test_concordia_high_school_no_ipeds_excluded(self):
        rows = [self._branch_no_match("10166659", "101666")]
        result = pgs._filter_published_rows(rows)
        assert not any(r["opeid8"] == "10166659" for r in result)

    def test_lewis_district_no_ipeds_excluded(self):
        rows = [self._branch_no_match("00170764", "001707")]
        result = pgs._filter_published_rows(rows)
        assert not any(r["opeid8"] == "00170764" for r in result)

    def test_concordia_no_coverage_excluded(self):
        rows = [_campus_event(opeid8="10166659", opeid6="101666",
                              match_method="no_ipeds_coverage", iclevel="")]
        result = pgs._filter_published_rows(rows)
        assert not any(r["opeid8"] == "10166659" for r in result)

    def test_branch_matched_no_iclevel_excluded(self):
        rows = [_campus_event(opeid8="10166659", opeid6="101666",
                              match_method="matched_opeid8_yr2019", iclevel="")]
        result = pgs._filter_published_rows(rows)
        assert not any(r["opeid8"] == "10166659" for r in result)

    def test_genuine_branch_with_evidence_is_publishable(self):
        """A branch with IPEDS match + iclevel + independent parent must be publishable."""
        branch = self._branch_matched_postsec("00800001", "008000", "1")
        result = pgs._filter_published_rows([branch])
        assert any(r["opeid8"] == "00800001" for r in result)

    def test_genuine_institution_closure_is_publishable(self):
        """A main campus institutional closure must appear as published institution row."""
        result = pgs._filter_published_rows([_inst_event()])
        assert len(result) == 1
        assert result[0]["closure_scope"] == "institution"


# ---------------------------------------------------------------------------
# Tests for by_year aggregation
# ---------------------------------------------------------------------------

class TestByYearReconciliation:

    def _two_inst_rows(self) -> list[dict]:
        return [
            _inst_event(opeid8="00100200", opeid6="001002",
                        control_label="public",
                        credential_level="Bachelor's",
                        closed_date_iso="2020-06-01", closed_year="2020"),
            _inst_event(opeid8="00200300", opeid6="002003",
                        control_label="for-profit",
                        credential_level="Certificate",
                        closure_type="for_profit_closure",
                        closed_date_iso="2019-05-01", closed_year="2019"),
        ]

    def test_by_year_header_columns(self):
        data = pgs._build_by_year_data([])
        assert data[0] == pgs._BY_YEAR_HEADER

    def test_total_equals_institution_closures(self):
        pub = pgs._filter_published_rows(self._two_inst_rows())
        data = pgs._build_by_year_data(pub)
        for row in data[1:]:
            if row[1] == 0:  # annual rows only
                total = row[3]
                inst  = row[12]
                assert total == inst, f"total ({total}) != institution_closures ({inst}) for year {row[0]}"

    def test_all_published_events_equals_inst_plus_campus(self):
        inst = _inst_event(opeid8="00100200", opeid6="001002",
                           closed_date_iso="2020-06-01", closed_year="2020")
        branch = _campus_event(opeid8="00700001", opeid6="007000",
                               match_method="matched_opeid8_yr2019", iclevel="1",
                               closed_date_iso="2020-03-01", closed_year="2020")
        pub = pgs._filter_published_rows([inst, branch])
        data = pgs._build_by_year_data(pub)
        for row in data[1:]:
            if row[1] == 0:
                inst_col  = row[12]
                camp_col  = row[13]
                all_col   = row[14]
                assert all_col == inst_col + camp_col

    def test_control_totals_reconcile(self):
        pub = pgs._filter_published_rows(self._two_inst_rows())
        data = pgs._build_by_year_data(pub)
        for row in data[1:]:
            if row[1] == 0:
                inst       = row[12]
                fp, np, pub_ctrl = row[4], row[5], row[6]
                unk_ctrl   = row[15]
                assert fp + np + pub_ctrl + unk_ctrl == inst, (
                    f"Control breakdown does not sum to institution_closures in year {row[0]}"
                )

    def test_credential_totals_reconcile(self):
        pub = pgs._filter_published_rows(self._two_inst_rows())
        data = pgs._build_by_year_data(pub)
        for row in data[1:]:
            if row[1] == 0:
                inst = row[12]
                nd, cert, assoc, bach, grad = row[7], row[8], row[9], row[10], row[11]
                unk_cred = row[16]
                assert nd + cert + assoc + bach + grad + unk_cred == inst, (
                    f"Credential breakdown does not sum to institution_closures in year {row[0]}"
                )

    def test_campus_closures_separate_from_institution_totals(self):
        inst   = _inst_event(opeid8="00100200", opeid6="001002",
                             control_label="public",
                             closed_date_iso="2020-06-01", closed_year="2020")
        branch = _campus_event(opeid8="00700001", opeid6="007000",
                               match_method="matched_opeid8_yr2019", iclevel="1",
                               closed_date_iso="2020-03-01", closed_year="2020")
        pub = pgs._filter_published_rows([inst, branch])
        data = pgs._build_by_year_data(pub)
        year_row = next(r for r in data[1:] if r[0] == "2020" and r[1] == 0)
        # total (backward compat) should not include campus
        assert year_row[3] == 1   # 1 institution
        assert year_row[13] == 1  # 1 campus
        assert year_row[14] == 2  # all_published_events

    def test_monthly_rows_present_for_months_with_data(self):
        pub = pgs._filter_published_rows(self._two_inst_rows())
        data = pgs._build_by_year_data(pub)
        monthly = [r for r in data[1:] if r[1] != 0]
        assert len(monthly) >= 2  # at least one month per closure


# ---------------------------------------------------------------------------
# Tests for by_state aggregation
# ---------------------------------------------------------------------------

class TestByStateReconciliation:

    def test_by_state_header_columns(self):
        data = pgs._build_by_state_data([])
        assert data[0] == pgs._BY_STATE_HEADER

    def test_total_equals_institution_closures(self):
        rows = [
            _inst_event(opeid8="00100200", opeid6="001002",
                        state="MA", control_label="public",
                        closed_date_iso="2020-06-01", closed_year="2020"),
        ]
        pub = pgs._filter_published_rows(rows)
        data = pgs._build_by_state_data(pub)
        ma_row = next(r for r in data[1:] if r[0] == "MA")
        assert ma_row[1] == ma_row[5]  # total == institution_closures

    def test_all_published_events_equals_inst_plus_campus(self):
        inst = _inst_event(state="MA", opeid8="00100200", opeid6="001002",
                           closed_date_iso="2020-06-01", closed_year="2020")
        branch = _campus_event(state="MA", opeid8="00700001", opeid6="007000",
                               match_method="matched_opeid8_yr2019", iclevel="1",
                               closed_date_iso="2020-03-01", closed_year="2020")
        pub = pgs._filter_published_rows([inst, branch])
        data = pgs._build_by_state_data(pub)
        ma_row = next(r for r in data[1:] if r[0] == "MA")
        assert ma_row[7] == ma_row[5] + ma_row[6]  # all = inst + campus

    def test_control_totals_reconcile(self):
        rows = [
            _inst_event(state="CA", opeid8="00100200", opeid6="001002",
                        control_label="public",
                        closed_date_iso="2020-06-01", closed_year="2020"),
            _inst_event(state="CA", opeid8="00200300", opeid6="002003",
                        control_label="nonprofit",
                        closed_date_iso="2019-01-01", closed_year="2019"),
        ]
        pub = pgs._filter_published_rows(rows)
        data = pgs._build_by_state_data(pub)
        ca_row = next(r for r in data[1:] if r[0] == "CA")
        inst       = ca_row[5]
        fp, np, pb = ca_row[2], ca_row[3], ca_row[4]
        unk_ctrl   = ca_row[8]
        assert fp + np + pb + unk_ctrl == inst

    def test_institution_closure_not_duplicated_across_states(self):
        # One institution with one state → should appear once only
        inst = _inst_event(state="MA", opeid8="00100200", opeid6="001002",
                           closed_date_iso="2020-06-01", closed_year="2020")
        pub = pgs._filter_published_rows([inst])
        data = pgs._build_by_state_data(pub)
        ma_rows = [r for r in data[1:] if r[0] == "MA"]
        assert len(ma_rows) == 1
        assert ma_rows[0][5] == 1  # institution_closures = 1

    def test_two_char_state_codes_only(self):
        rows = [
            _inst_event(state="XX_BAD", opeid8="00100200", opeid6="001002",
                        closed_date_iso="2020-06-01", closed_year="2020"),
        ]
        pub = pgs._filter_published_rows(rows)
        data = pgs._build_by_state_data(pub)
        assert len(data) == 1  # header only — bad state excluded


# ---------------------------------------------------------------------------
# Tests for push_to_gsheet tab columns (TAB_COLUMNS integration)
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

    def test_location_administrative_excluded_from_closure_list(self, tmp_path):
        rows = _sample_events() + [
            _inst_event(opeid8="99999900", opeid6="999999",
                        closure_type="location_administrative",
                        is_suspicious_location="true",
                        closed_year="2020", closed_date_iso="2020-01-01"),
        ]
        _write_events_csv(tmp_path, rows)

        gc, sh, ws = self._make_mock_sheet()
        with patch.object(pgs, "DERIVED", tmp_path), \
             patch("push_to_gsheet._get_gspread_client", return_value=gc):
            pgs.push("FAKE_SHEET_ID", "closure_list")

        call_data = ws.update.call_args[0][1]
        opeid8s = [r[0] for r in call_data[1:]]
        assert "99999900" not in opeid8s


# ---------------------------------------------------------------------------
# Tests for _load_rows with new exclude_types parameter
# ---------------------------------------------------------------------------

class TestLoadRowsFiltering:
    def test_exclude_types_none_keeps_all(self, tmp_path):
        rows = _sample_events() + [
            _inst_event(opeid8="99999900", opeid6="999999",
                        closure_type="location_administrative",
                        closed_year="2020", closed_date_iso="2020-01-01"),
        ]
        _write_events_csv(tmp_path, rows)
        p = tmp_path / "fsa_closure_events_2026-01-01.csv"
        result = pgs._load_rows(p, ["opeid8"])
        assert len(result) == 4  # header + 3 rows

    def test_exclude_types_removes_matching(self, tmp_path):
        rows = _sample_events() + [
            _inst_event(opeid8="99999900", opeid6="999999",
                        closure_type="location_administrative",
                        closed_year="2020", closed_date_iso="2020-01-01"),
        ]
        _write_events_csv(tmp_path, rows)
        p = tmp_path / "fsa_closure_events_2026-01-01.csv"
        result = pgs._load_rows(p, ["opeid8"],
                                exclude_types=frozenset({"location_administrative"}))
        assert len(result) == 3  # header + 2 remaining rows

    def test_min_year_filters_older_rows(self, tmp_path):
        _write_events_csv(tmp_path, _sample_events())
        p = tmp_path / "fsa_closure_events_2026-01-01.csv"
        cols = ["opeid8", "closed_year"]
        result = pgs._load_rows(p, cols, min_year=2019)
        years = [row[1] for row in result[1:]]
        assert all(int(y) >= 2019 for y in years if y)

    def test_min_year_none_keeps_all(self, tmp_path):
        _write_events_csv(tmp_path, _sample_events())
        p = tmp_path / "fsa_closure_events_2026-01-01.csv"
        cols = ["opeid8"]
        assert len(pgs._load_rows(p, cols, min_year=None)) == 3  # header + 2


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
