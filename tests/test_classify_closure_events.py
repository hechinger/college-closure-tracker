"""Tests for scripts/classify_closure_events.py."""

from __future__ import annotations

import csv
import io
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from classify_closure_events import (
    CONSOLIDATION_WINDOW_YEARS,
    EVENT_TYPES,
    EVENTS_FIELDS,
    _build_multi_closure_opeid6s,
    _build_rt06_index,
    _build_rt11_index,
    _load_overrides,
    _looks_like_institution,
    classify_records,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rec(**kwargs) -> dict:
    """Minimal enriched record with sensible defaults."""
    defaults = {
        "opeid8":                 "00100200",
        "opeid6":                 "001002",
        "location_suffix":        "00",
        "is_main_campus":         "true",
        "is_suspicious_location": "false",
        "is_traditional_college": "true",
        "control_label":          "public",
        "control":                "1",
        "school_name":            "Test College",
        "location_name":          "Test College",
        "closed_date_iso":        "2020-06-01",
        "closed_year":            "2020",
        "institution_category":   "public_4yr",
        "enrollment_band":        "1000-4999",
        "match_method":           "matched_opeid8_yr2019",
        "unitid":                 "111",
        "approach1_all_campuses":     "true",
        "approach2_main_campus_only": "true",
        "approach3_system_level":     "true",
    }
    defaults.update(kwargs)
    return defaults


def _make_rt11_csv(tmp_path: Path, rows: list[dict]) -> Path:
    fields = ["opeid8", "opeid6", "record_holder_desc"]
    p = tmp_path / "fsa_wsf_rt11_test.csv"
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return p


# ---------------------------------------------------------------------------
# _looks_like_institution
# ---------------------------------------------------------------------------

class TestLooksLikeInstitution:
    def test_plain_college_name_true(self):
        assert _looks_like_institution("Springfield Community College") is True

    def test_university_name_true(self):
        assert _looks_like_institution("Valley Technical University") is True

    def test_state_agency_false(self):
        assert _looks_like_institution("State Approving Agency for Veterans Education") is False

    def test_board_false(self):
        assert _looks_like_institution("State Board of Education") is False

    def test_department_false(self):
        assert _looks_like_institution("Department of Education") is False

    def test_bankruptcy_false(self):
        assert _looks_like_institution("Chapter 7 Trustee") is False

    def test_empty_false(self):
        assert _looks_like_institution("") is False

    def test_single_word_false(self):
        assert _looks_like_institution("SCHOOL") is False

    def test_very_short_false(self):
        assert _looks_like_institution("ABC") is False

    def test_accrediting_body_false(self):
        assert _looks_like_institution("Accrediting Council for Independent Colleges") is False

    def test_federal_false(self):
        assert _looks_like_institution("Federal Student Aid Office") is False

    def test_guaranty_agency_false(self):
        assert _looks_like_institution("Texas Guaranteed Student Loan Corporation") is False


# ---------------------------------------------------------------------------
# _build_rt11_index
# ---------------------------------------------------------------------------

class TestBuildRt11Index:
    def test_basic_lookup(self, tmp_path):
        p = _make_rt11_csv(tmp_path, [
            {"opeid8": "00100200", "record_holder_desc": "Springfield College"},
        ])
        idx = _build_rt11_index(p)
        assert idx["00100200"] == "Springfield College"

    def test_first_non_empty_wins(self, tmp_path):
        p = _make_rt11_csv(tmp_path, [
            {"opeid8": "00100200", "record_holder_desc": "First College"},
            {"opeid8": "00100200", "record_holder_desc": "Second College"},
        ])
        idx = _build_rt11_index(p)
        assert idx["00100200"] == "First College"

    def test_empty_desc_skipped(self, tmp_path):
        p = _make_rt11_csv(tmp_path, [
            {"opeid8": "00100200", "record_holder_desc": ""},
            {"opeid8": "00100200", "record_holder_desc": "Later College"},
        ])
        idx = _build_rt11_index(p)
        assert idx["00100200"] == "Later College"

    def test_missing_opeid8_skipped(self, tmp_path):
        p = _make_rt11_csv(tmp_path, [
            {"opeid8": "", "record_holder_desc": "Some College"},
        ])
        idx = _build_rt11_index(p)
        assert "" not in idx


# ---------------------------------------------------------------------------
# _build_multi_closure_opeid6s
# ---------------------------------------------------------------------------

class TestBuildMultiClosureOpeid6s:
    def test_single_campus_not_included(self):
        records = [_rec(opeid6="001002", opeid8="00100200", closed_year="2020")]
        result = _build_multi_closure_opeid6s(records)
        assert "001002" not in result

    def test_two_campuses_same_opeid6_included(self):
        records = [
            _rec(opeid6="001002", opeid8="00100200", closed_year="2020"),
            _rec(opeid6="001002", opeid8="00100201", closed_year="2021"),
        ]
        result = _build_multi_closure_opeid6s(records)
        assert "001002" in result

    def test_window_exceeded_not_included(self):
        records = [
            _rec(opeid6="001002", opeid8="00100200", closed_year="2015"),
            _rec(opeid6="001002", opeid8="00100201",
                 closed_year=str(2015 + CONSOLIDATION_WINDOW_YEARS + 1)),
        ]
        result = _build_multi_closure_opeid6s(records)
        assert "001002" not in result

    def test_window_exactly_at_limit_included(self):
        records = [
            _rec(opeid6="001002", opeid8="00100200", closed_year="2015"),
            _rec(opeid6="001002", opeid8="00100201",
                 closed_year=str(2015 + CONSOLIDATION_WINDOW_YEARS)),
        ]
        result = _build_multi_closure_opeid6s(records)
        assert "001002" in result

    def test_different_opeid6s_treated_independently(self):
        records = [
            _rec(opeid6="001002", opeid8="00100200", closed_year="2020"),
            _rec(opeid6="002003", opeid8="00200300", closed_year="2020"),
        ]
        result = _build_multi_closure_opeid6s(records)
        assert "001002" not in result
        assert "002003" not in result


# ---------------------------------------------------------------------------
# classify_records — per-rule tests
# ---------------------------------------------------------------------------

class TestClassifyRecords:

    def test_location_administrative_wins(self):
        records = [_rec(is_suspicious_location="true")]
        result = classify_records(records)
        assert result[0]["closure_type"] == "location_administrative"

    def test_location_administrative_beats_branch(self):
        # Suspicious non-main campus → location_administrative, not branch_closure
        records = [_rec(is_suspicious_location="true", is_main_campus="false",
                        location_suffix="01")]
        result = classify_records(records)
        assert result[0]["closure_type"] == "location_administrative"

    def test_branch_closure(self):
        records = [_rec(is_main_campus="false", location_suffix="03")]
        result = classify_records(records)
        assert result[0]["closure_type"] == "branch_closure"

    def test_merger_absorption_via_rt11(self, tmp_path):
        p = _make_rt11_csv(tmp_path, [
            {"opeid8": "00100200", "record_holder_desc": "Valley Community College"},
        ])
        rt11_idx = _build_rt11_index(p)
        records = [_rec()]
        result = classify_records(records, rt11_idx=rt11_idx)
        assert result[0]["closure_type"] == "merger_absorption"
        assert "Valley Community College" in result[0]["closure_type_basis"]

    def test_merger_absorption_govt_holder_skipped(self, tmp_path):
        p = _make_rt11_csv(tmp_path, [
            {"opeid8": "00100200", "record_holder_desc": "State Approving Agency"},
        ])
        rt11_idx = _build_rt11_index(p)
        records = [_rec()]
        result = classify_records(records, rt11_idx=rt11_idx)
        # Govt body → not merger_absorption; falls through to institutional_closure
        assert result[0]["closure_type"] == "institutional_closure"

    def test_merger_absorption_requires_both_idxs_absent(self):
        # Without RT06 or RT11, merger_absorption never fires
        records = [_rec()]
        result = classify_records(records, rt06_idx=None, rt11_idx=None)
        assert result[0]["closure_type"] == "institutional_closure"

    def test_system_consolidation_nonprofit(self):
        records = [
            _rec(opeid6="001002", opeid8="00100200", closed_year="2020",
                 control_label="nonprofit", is_traditional_college="true"),  # main
            _rec(opeid6="001002", opeid8="00100201", is_main_campus="false",
                 location_suffix="01", closed_year="2021",
                 control_label="nonprofit"),                                  # branch
        ]
        result = classify_records(records)
        main_row   = next(r for r in result if r["opeid8"] == "00100200")
        branch_row = next(r for r in result if r["opeid8"] == "00100201")
        assert main_row["closure_type"] == "system_consolidation"
        # Branch is still branch_closure (rule 2 fires before rule 4 for it)
        assert branch_row["closure_type"] == "branch_closure"

    def test_for_profit_multi_campus_not_system_consolidation(self):
        # For-profit chain closing main + branch → for_profit_closure, not system_consolidation
        records = [
            _rec(opeid6="001002", opeid8="00100200", closed_year="2020",
                 control_label="for-profit", is_traditional_college="false"),  # main
            _rec(opeid6="001002", opeid8="00100201", is_main_campus="false",
                 location_suffix="01", closed_year="2021",
                 control_label="for-profit", is_traditional_college="false"),  # branch
        ]
        result = classify_records(records)
        main_row = next(r for r in result if r["opeid8"] == "00100200")
        assert main_row["closure_type"] == "for_profit_closure"

    def test_for_profit_closure(self):
        records = [_rec(control_label="for-profit", is_traditional_college="false")]
        result = classify_records(records)
        assert result[0]["closure_type"] == "for_profit_closure"

    def test_institutional_closure_traditional(self):
        records = [_rec(control_label="nonprofit", is_traditional_college="true")]
        result = classify_records(records)
        assert result[0]["closure_type"] == "institutional_closure"

    def test_institutional_closure_public(self):
        records = [_rec(control_label="public", is_traditional_college="true")]
        result = classify_records(records)
        assert result[0]["closure_type"] == "institutional_closure"

    def test_main_campus_unclassified_sector_still_institutional(self):
        records = [_rec(control_label="", is_traditional_college="false")]
        result = classify_records(records)
        assert result[0]["closure_type"] == "institutional_closure"

    def test_all_rows_have_closure_type(self):
        records = [
            _rec(is_suspicious_location="true"),
            _rec(is_main_campus="false", location_suffix="01"),
            _rec(control_label="for-profit", is_traditional_college="false"),
            _rec(control_label="public", is_traditional_college="true"),
        ]
        result = classify_records(records)
        for row in result:
            assert row["closure_type"] in EVENT_TYPES

    def test_all_rows_have_basis_string(self):
        records = [_rec()]
        result = classify_records(records)
        assert isinstance(result[0]["closure_type_basis"], str)
        assert len(result[0]["closure_type_basis"]) > 0

    def test_all_events_fields_present(self):
        records = [_rec()]
        result = classify_records(records)
        for field in ("closure_type", "closure_type_basis"):
            assert field in result[0], f"Missing: {field}"

    def test_input_records_not_mutated(self):
        original = _rec()
        records = [original]
        classify_records(records)
        assert "closure_type" not in original


# ---------------------------------------------------------------------------
# _build_rt06_index + RT06 merger detection
# ---------------------------------------------------------------------------

def _make_rt06_csv(tmp_path: Path, rows: list[dict]) -> Path:
    fields = [
        "prev_opeid8", "prev_opeid6", "new_opeid8", "new_opeid6",
        "coa_actn_cd", "effective_date_iso", "opeid6_changed",
    ]
    p = tmp_path / "fsa_wsf_rt06_test.csv"
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return p


def _rt06_row(prev8="00100200", new8="09999900", coa="1", eff="2020-06-01"):
    prev6 = prev8[:6]
    new6  = new8[:6]
    return {
        "prev_opeid8":        prev8,
        "prev_opeid6":        prev6,
        "new_opeid8":         new8,
        "new_opeid6":         new6,
        "coa_actn_cd":        coa,
        "effective_date_iso": eff,
        "opeid6_changed":     "true" if prev6 != new6 else "false",
    }


class TestBuildRt06Index:
    def test_opeid6_changed_record_indexed(self, tmp_path):
        p = _make_rt06_csv(tmp_path, [_rt06_row("00100200", "09999900")])
        idx = _build_rt06_index(p, frozenset())
        assert "00100200" in idx

    def test_suffix_only_record_excluded(self, tmp_path):
        # Same OPEID6, different suffix — opeid6_changed="false"
        p = _make_rt06_csv(tmp_path, [_rt06_row("00100200", "00100201")])
        # Manually mark as suffix-only
        rows = [_rt06_row("00100200", "00100201")]
        rows[0]["opeid6_changed"] = "false"
        p = _make_rt06_csv(tmp_path, rows)
        idx = _build_rt06_index(p, frozenset())
        assert "00100200" not in idx

    def test_coa_actn_cd_4_excluded(self, tmp_path):
        # coa_actn_cd=4 is always a renumber — excluded even if opeid6_changed="true"
        row = _rt06_row("00100200", "09999900", coa="4")
        p = _make_rt06_csv(tmp_path, [row])
        idx = _build_rt06_index(p, frozenset())
        assert "00100200" not in idx

    def test_is_merger_candidate_true_when_new_opeid6_not_closed(self, tmp_path):
        p = _make_rt06_csv(tmp_path, [_rt06_row("00100200", "09999900")])
        idx = _build_rt06_index(p, frozenset())  # empty closed set
        assert idx["00100200"]["is_merger_candidate"] is True

    def test_is_merger_candidate_false_when_new_opeid6_also_closed(self, tmp_path):
        p = _make_rt06_csv(tmp_path, [_rt06_row("00100200", "09999900")])
        # new OPEID6 is "099999" — mark it as closed
        idx = _build_rt06_index(p, frozenset(["099999"]))
        assert idx["00100200"]["is_merger_candidate"] is False

    def test_new_opeid8_stored(self, tmp_path):
        p = _make_rt06_csv(tmp_path, [_rt06_row("00100200", "09999900")])
        idx = _build_rt06_index(p, frozenset())
        assert idx["00100200"]["new_opeid8"] == "09999900"

    def test_first_record_wins(self, tmp_path):
        rows = [
            _rt06_row("00100200", "09999900", eff="2018-01-01"),
            _rt06_row("00100200", "08888800", eff="2020-06-01"),
        ]
        p = _make_rt06_csv(tmp_path, rows)
        idx = _build_rt06_index(p, frozenset())
        assert idx["00100200"]["new_opeid8"] == "09999900"


class TestClassifyRecordsRt06:
    # RT06 is loaded and indexed but NOT used for merger_absorption classification.
    # RT06 captures historical corporate-lineage OPEID changes (some 40+ years old)
    # that predate the closure event and produce false positives for chain collapses.
    # The rt06_idx parameter is accepted by classify_records for forward compatibility
    # but does not affect any classification rule.

    def test_rt06_does_not_trigger_merger_absorption(self, tmp_path):
        # Even with a valid RT06 OPEID6 change, merger_absorption should NOT fire
        # (RT11 is the only merger signal)
        p = _make_rt06_csv(tmp_path, [_rt06_row("00100200", "09999900")])
        idx = _build_rt06_index(p, frozenset())
        records = [_rec()]
        result = classify_records(records, rt06_idx=idx)
        # Falls through to institutional_closure since no RT11 present
        assert result[0]["closure_type"] == "institutional_closure"

    def test_rt06_does_not_override_rt11(self, tmp_path):
        # With both RT06 and RT11, RT11 should still produce merger_absorption
        rt06_p = _make_rt06_csv(tmp_path, [_rt06_row("00100200", "09999900")])
        rt06_idx = _build_rt06_index(rt06_p, frozenset())
        rt11_p = _make_rt11_csv(tmp_path, [
            {"opeid8": "00100200", "record_holder_desc": "Valley Community College"},
        ])
        rt11_idx = _build_rt11_index(rt11_p)
        records = [_rec()]
        result = classify_records(records, rt06_idx=rt06_idx, rt11_idx=rt11_idx)
        assert result[0]["closure_type"] == "merger_absorption"
        assert "RT11" in result[0]["closure_type_basis"]

    def test_rt06_absent_rt11_still_fires(self, tmp_path):
        # RT06 absent but RT11 present — RT11 heuristic should still work
        rt11_p = _make_rt11_csv(tmp_path, [
            {"opeid8": "00100200", "record_holder_desc": "Valley Community College"},
        ])
        rt11_idx = _build_rt11_index(rt11_p)
        records = [_rec()]
        result = classify_records(records, rt06_idx=None, rt11_idx=rt11_idx)
        assert result[0]["closure_type"] == "merger_absorption"
        assert "RT11" in result[0]["closure_type_basis"]

    def test_rt06_passed_does_not_affect_branch_classification(self, tmp_path):
        p = _make_rt06_csv(tmp_path, [_rt06_row("00100201", "09999901")])
        idx = _build_rt06_index(p, frozenset())
        records = [_rec(opeid8="00100201", is_main_campus="false", location_suffix="01")]
        result = classify_records(records, rt06_idx=idx)
        assert result[0]["closure_type"] == "branch_closure"


# ---------------------------------------------------------------------------
# TestLoadOverrides
# ---------------------------------------------------------------------------

def _write_overrides_csv(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "closure_type_overrides.csv"
    fields = ["opeid8", "closure_type", "closure_type_basis", "editor_note"]
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({f: row.get(f, "") for f in fields})
    return p


class TestLoadOverrides:
    def test_loads_valid_rows(self, tmp_path):
        p = _write_overrides_csv(tmp_path, [
            {"opeid8": "00219300", "closure_type": "merger_absorption",
             "closure_type_basis": "Editorial: merged into URI", "editor_note": "Mount Ida"},
        ])
        ov = _load_overrides(p)
        assert "00219300" in ov
        assert ov["00219300"]["closure_type"] == "merger_absorption"
        assert "URI" in ov["00219300"]["closure_type_basis"]

    def test_loads_multiple_rows(self, tmp_path):
        p = _write_overrides_csv(tmp_path, [
            {"opeid8": "00219300", "closure_type": "merger_absorption",
             "closure_type_basis": "Editorial A"},
            {"opeid8": "03088800", "closure_type": "merger_absorption",
             "closure_type_basis": "Editorial B"},
        ])
        ov = _load_overrides(p)
        assert len(ov) == 2
        assert "00219300" in ov
        assert "03088800" in ov

    def test_skips_row_missing_opeid8(self, tmp_path, capsys):
        p = _write_overrides_csv(tmp_path, [
            {"opeid8": "", "closure_type": "merger_absorption",
             "closure_type_basis": "no opeid"},
        ])
        ov = _load_overrides(p)
        assert len(ov) == 0
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    def test_skips_row_missing_closure_type(self, tmp_path, capsys):
        p = _write_overrides_csv(tmp_path, [
            {"opeid8": "00219300", "closure_type": "",
             "closure_type_basis": "no type"},
        ])
        ov = _load_overrides(p)
        assert len(ov) == 0
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    def test_skips_row_unknown_closure_type(self, tmp_path, capsys):
        p = _write_overrides_csv(tmp_path, [
            {"opeid8": "00219300", "closure_type": "banana",
             "closure_type_basis": "bad type"},
        ])
        ov = _load_overrides(p)
        assert len(ov) == 0
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    def test_all_valid_event_types_accepted(self, tmp_path):
        rows = [
            {"opeid8": f"0000000{i}", "closure_type": et,
             "closure_type_basis": f"basis {et}"}
            for i, et in enumerate(EVENT_TYPES)
        ]
        p = _write_overrides_csv(tmp_path, rows)
        ov = _load_overrides(p)
        assert len(ov) == len(EVENT_TYPES)

    def test_empty_file_returns_empty_dict(self, tmp_path):
        p = _write_overrides_csv(tmp_path, [])
        ov = _load_overrides(p)
        assert ov == {}


# ---------------------------------------------------------------------------
# TestClassifyRecordsOverrides
# ---------------------------------------------------------------------------

class TestClassifyRecordsOverrides:
    def test_override_supersedes_institutional_closure(self, tmp_path):
        # Without override: for-profit → for_profit_closure; with override → merger_absorption
        records = [_rec(opeid8="00219300", control="3", control_label="for-profit")]
        p = _write_overrides_csv(tmp_path, [
            {"opeid8": "00219300", "closure_type": "merger_absorption",
             "closure_type_basis": "Editorial: merged into URI"},
        ])
        ov = _load_overrides(p)
        result = classify_records(records, overrides=ov)
        assert result[0]["closure_type"] == "merger_absorption"
        assert "Editorial" in result[0]["closure_type_basis"]

    def test_override_supersedes_rt11_merger_rule(self, tmp_path):
        # RT11 would say merger; override says institutional_closure
        rt11_p = _make_rt11_csv(tmp_path, [
            {"opeid8": "00100200", "record_holder_desc": "Valley Community College"},
        ])
        rt11_idx = _build_rt11_index(rt11_p)
        p = _write_overrides_csv(tmp_path, [
            {"opeid8": "00100200", "closure_type": "institutional_closure",
             "closure_type_basis": "Editorial: RT11 record is stale"},
        ])
        ov = _load_overrides(p)
        result = classify_records([_rec()], rt11_idx=rt11_idx, overrides=ov)
        assert result[0]["closure_type"] == "institutional_closure"
        assert "Editorial" in result[0]["closure_type_basis"]

    def test_override_does_not_affect_non_matching_opeid(self, tmp_path):
        p = _write_overrides_csv(tmp_path, [
            {"opeid8": "99999999", "closure_type": "merger_absorption",
             "closure_type_basis": "Editorial: unrelated institution"},
        ])
        ov = _load_overrides(p)
        result = classify_records([_rec(opeid8="00100200")], overrides=ov)
        assert result[0]["closure_type"] == "institutional_closure"

    def test_override_none_does_not_error(self):
        result = classify_records([_rec()], overrides=None)
        assert result[0]["closure_type"] == "institutional_closure"

    def test_override_empty_dict_does_not_error(self):
        result = classify_records([_rec()], overrides={})
        assert result[0]["closure_type"] == "institutional_closure"

    def test_override_applies_to_multiple_records(self, tmp_path):
        p = _write_overrides_csv(tmp_path, [
            {"opeid8": "00219300", "closure_type": "merger_absorption",
             "closure_type_basis": "Editorial A"},
            {"opeid8": "03088800", "closure_type": "merger_absorption",
             "closure_type_basis": "Editorial B"},
        ])
        ov = _load_overrides(p)
        records = [
            _rec(opeid8="00219300", opeid6="002193"),
            _rec(opeid8="03088800", opeid6="030888"),
            _rec(opeid8="00100200", opeid6="001002"),
        ]
        result = classify_records(records, overrides=ov)
        types = {r["opeid8"]: r["closure_type"] for r in result}
        assert types["00219300"] == "merger_absorption"
        assert types["03088800"] == "merger_absorption"
        assert types["00100200"] == "institutional_closure"

    def test_production_overrides_csv_loads_cleanly(self):
        prod_path = (
            Path(__file__).resolve().parent.parent
            / "data" / "manual" / "closure_type_overrides.csv"
        )
        if not prod_path.exists():
            pytest.skip("production overrides CSV not present")
        ov = _load_overrides(prod_path)
        if len(ov) == 0:
            pytest.skip("overrides CSV is empty — populate Sheet overrides tab to enable this guard")
        assert len(ov) >= 4, "Expected at least 4 confirmed mergers"
        assert "00219300" in ov, "Mount Ida override missing"
        assert "03088800" in ov, "Watkins College override missing"
        for opeid8, entry in ov.items():
            assert entry["closure_type"] in EVENT_TYPES, (
                f"Production override for {opeid8} has unknown type {entry['closure_type']!r}"
            )
