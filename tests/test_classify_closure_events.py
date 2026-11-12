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
    AUTO_REVIEW_STATUS,
    CONSOLIDATION_WINDOW_YEARS,
    EVENT_TYPES,
    EVENTS_FIELDS,
    _build_multi_closure_opeid6s,
    _build_rt01_indexes,
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
        assert result[0]["closure_type"] == "unclassified"
        assert "location_name_requires_review" in result[0]["review_reason"]

    def test_location_administrative_beats_branch(self):
        # A name pattern can trigger review but cannot decide location type.
        records = [_rec(is_suspicious_location="true", is_main_campus="false",
                        location_suffix="01")]
        result = classify_records(records)
        assert result[0]["closure_type"] == "unclassified"
        assert result[0]["review_required"] == "true"

    def test_branch_closure(self):
        records = [_rec(opeid8="00100203", is_main_campus="false", location_suffix="03", iclevel="1")]
        rt01 = {
            "001002": [
                {"opeid8": "00100200", "close_date_iso": "", "reinstate_date_iso": ""},
                {"opeid8": "00100203", "close_date_iso": "2020-06-01", "reinstate_date_iso": ""},
            ]
        }
        result = classify_records(records, rt01_by_opeid6=rt01)
        assert result[0]["closure_type"] == "branch_closure"
        assert result[0]["parent_continued"] == "true"
        assert result[0]["review_required"] == "true"

    def test_rt11_is_record_custody_not_merger(self, tmp_path):
        p = _make_rt11_csv(tmp_path, [
            {"opeid8": "00100200", "record_holder_desc": "Valley Community College"},
        ])
        rt11_idx = _build_rt11_index(p)
        records = [_rec()]
        result = classify_records(records, rt11_idx=rt11_idx)
        assert result[0]["closure_type"] == "institutional_closure"
        assert result[0]["record_holder_desc"] == "Valley Community College"
        assert "record_custodian_available" in result[0]["review_reason"]

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
        assert main_row["closure_type"] == "institutional_closure"
        assert "possible_system_consolidation" in main_row["review_reason"]
        # Clustering is only a review signal; it cannot establish branch scope.
        assert branch_row["closure_type"] == "unclassified"
        assert "possible_system_consolidation" in branch_row["review_reason"]

    def test_for_profit_multi_campus_not_system_consolidation(self):
        # Ownership stays separate from closure type; clustering remains a review signal.
        records = [
            _rec(opeid6="001002", opeid8="00100200", closed_year="2020",
                 control_label="for-profit", is_traditional_college="false"),  # main
            _rec(opeid6="001002", opeid8="00100201", is_main_campus="false",
                 location_suffix="01", closed_year="2021",
                 control_label="for-profit", is_traditional_college="false"),  # branch
        ]
        result = classify_records(records)
        main_row = next(r for r in result if r["opeid8"] == "00100200")
        assert main_row["closure_type"] == "institutional_closure"
        assert main_row["control_label"] == "for-profit"
        assert "control = for-profit" in main_row["closure_type_basis"]

    def test_for_profit_school_uses_institutional_closure(self):
        records = [_rec(control_label="for-profit", is_traditional_college="false")]
        result = classify_records(records)
        assert result[0]["closure_type"] == "institutional_closure"
        assert result[0]["control_label"] == "for-profit"

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
        assert result[0]["closure_type"] == "unclassified"
        assert "institution_identity_unresolved" in result[0]["review_reason"]

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
        for field in (
            "closure_type", "closure_type_basis", "closure_event_id",
            "location_operations_ceased", "parent_continued",
            "review_status", "publication_decision", "evidence_fingerprint",
        ):
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
    # RT06 is an exact identifier relationship, not merger proof. It becomes an
    # identifier-replacement review signal only when date-proximate and when the
    # exact successor OPEID8 is active in RT01.

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
        # Neither an undated successor status nor RT11 custody proves merger.
        rt06_p = _make_rt06_csv(tmp_path, [_rt06_row("00100200", "09999900")])
        rt06_idx = _build_rt06_index(rt06_p, frozenset())
        rt11_p = _make_rt11_csv(tmp_path, [
            {"opeid8": "00100200", "record_holder_desc": "Valley Community College"},
        ])
        rt11_idx = _build_rt11_index(rt11_p)
        records = [_rec()]
        result = classify_records(records, rt06_idx=rt06_idx, rt11_idx=rt11_idx)
        assert result[0]["closure_type"] == "institutional_closure"
        assert result[0]["record_holder_desc"] == "Valley Community College"

    def test_rt06_absent_rt11_still_fires(self, tmp_path):
        # RT11 is record custody only, even when no RT06 file is available.
        rt11_p = _make_rt11_csv(tmp_path, [
            {"opeid8": "00100200", "record_holder_desc": "Valley Community College"},
        ])
        rt11_idx = _build_rt11_index(rt11_p)
        records = [_rec()]
        result = classify_records(records, rt06_idx=None, rt11_idx=rt11_idx)
        assert result[0]["closure_type"] == "institutional_closure"
        assert result[0]["record_holder_desc"] == "Valley Community College"

    def test_rt06_passed_does_not_affect_branch_classification(self, tmp_path):
        p = _make_rt06_csv(tmp_path, [_rt06_row("00100201", "09999901")])
        idx = _build_rt06_index(p, frozenset())
        records = [_rec(opeid8="00100201", is_main_campus="false", location_suffix="01")]
        result = classify_records(records, rt06_idx=idx)
        assert result[0]["closure_type"] == "unclassified"
        assert "parent_continuation_unknown" in result[0]["review_reason"]

    def test_date_proximate_active_exact_successor_is_review_signal(self, tmp_path):
        p = _make_rt06_csv(tmp_path, [_rt06_row("00100200", "09999900", eff="2020-05-15")])
        idx = _build_rt06_index(p, frozenset())
        successor = {
            "09999900": {
                "opeid8": "09999900", "opeid6": "099999",
                "close_date_iso": "", "reinstate_date_iso": "",
            }
        }
        result = classify_records([_rec()], rt06_idx=idx, rt01_by_opeid8=successor)
        assert result[0]["closure_type"] == "identifier_replacement"
        assert result[0]["replacement_opeid8"] == "09999900"
        assert result[0]["publication_decision"] == ""

    def test_stale_rt06_relationship_is_not_replacement(self, tmp_path):
        p = _make_rt06_csv(tmp_path, [_rt06_row("00100200", "09999900", eff="1991-09-04")])
        idx = _build_rt06_index(p, frozenset())
        successor = {
            "09999900": {
                "opeid8": "09999900", "opeid6": "099999",
                "close_date_iso": "", "reinstate_date_iso": "",
            }
        }
        result = classify_records([_rec()], rt06_idx=idx, rt01_by_opeid8=successor)
        assert result[0]["closure_type"] == "institutional_closure"
        assert result[0]["replacement_opeid8"] == ""


class TestAutoBaseline:
    """Base-table rows meeting the evidence standard auto-publish; new ones need review."""

    _RT01_BRANCH = {
        "001002": [
            {"opeid8": "00100200", "close_date_iso": "", "reinstate_date_iso": ""},
            {"opeid8": "00100203", "close_date_iso": "2020-06-01", "reinstate_date_iso": ""},
        ]
    }
    _RT01_MAIN_ALONE = {
        "001002": [
            {"opeid8": "00100200", "close_date_iso": "2020-06-01", "reinstate_date_iso": ""},
        ]
    }

    def _branch(self, **kw):
        return _rec(opeid8="00100203", is_main_campus="false", location_suffix="03",
                    iclevel="1", **kw)

    def test_baseline_branch_is_auto_resolved_campus(self):
        r = classify_records([self._branch()], rt01_by_opeid6=self._RT01_BRANCH,
                             auto_baseline=frozenset({"fsa:00100203:2020-06-01"}))[0]
        assert r["review_status"] == AUTO_REVIEW_STATUS
        assert r["review_required"] == "false"
        assert r["publication_decision"] == "include_campus"
        assert r["location_operations_ceased"] == "true"
        assert "operational_closure_unverified" not in r["review_reason"]

    def test_new_closure_meeting_standard_still_needs_review(self):
        r = classify_records([self._branch()], rt01_by_opeid6=self._RT01_BRANCH,
                             auto_baseline=frozenset({"fsa:09999900:2019-01-01"}))[0]
        assert r["review_status"] == "pending"
        assert r["review_required"] == "true"
        assert r["publication_decision"] == ""
        assert "new_since_baseline" in r["review_reason"]

    def test_no_baseline_means_nothing_auto(self):
        r = classify_records([self._branch()], rt01_by_opeid6=self._RT01_BRANCH)[0]
        assert r["review_required"] == "true"

    def test_baseline_main_with_no_active_sibling_is_institution(self):
        r = classify_records([_rec()], rt01_by_opeid6=self._RT01_MAIN_ALONE,
                             auto_baseline=frozenset({"fsa:00100200:2020-06-01"}))[0]
        assert r["publication_decision"] == "include_institution"
        assert r["parent_continued"] == "false"

    def test_baseline_main_with_active_sibling_needs_review(self):
        rt01 = {"001002": [
            {"opeid8": "00100200", "close_date_iso": "2020-06-01", "reinstate_date_iso": ""},
            {"opeid8": "00100201", "close_date_iso": "", "reinstate_date_iso": ""},
        ]}
        r = classify_records([_rec()], rt01_by_opeid6=rt01,
                             auto_baseline=frozenset({"fsa:00100200:2020-06-01"}))[0]
        assert r["review_required"] == "true"

    def test_ambiguous_ipeds_match_never_auto(self):
        r = classify_records([_rec(match_method="review_opeid6_multi")],
                             rt01_by_opeid6=self._RT01_MAIN_ALONE,
                             auto_baseline=frozenset({"fsa:00100200:2020-06-01"}))[0]
        assert r["review_required"] == "true"

    def test_editorial_override_beats_auto(self):
        ov = {"fsa:00100203:2020-06-01": {
            "closure_type": "location_administrative", "closure_type_basis": "Editor",
            "publication_decision": "exclude", "evidence_fingerprint": "",
        }}
        r = classify_records([self._branch()], rt01_by_opeid6=self._RT01_BRANCH,
                             overrides=ov,
                             auto_baseline=frozenset({"fsa:00100203:2020-06-01"}))[0]
        assert r["review_status"] == "resolved"
        assert r["publication_decision"] == "exclude"

    def test_freeze_refuses_to_overwrite_existing_baseline(self, tmp_path):
        from classify_closure_events import main
        baseline = tmp_path / "baseline.csv"
        baseline.write_text("closure_event_id,frozen_on\n", encoding="utf-8")
        assert main(["--freeze-auto-baseline", "--auto-baseline-csv", str(baseline),
                     "--out-dir", str(tmp_path)]) == 1


class TestRealEvidenceBoundary:
    """Regressions using the exact current production rows and source indexes."""

    def test_stale_merger_relations_and_high_school_locations_stay_unresolved(self):
        project = Path(__file__).resolve().parent.parent
        enriched = project / "data" / "derived" / "fsa_closure_ipeds_enriched_2026-11-10.csv"
        rt01_path = project / "data" / "derived" / "fsa_wsf_rt01_2026-11-05.csv"
        rt06_path = project / "data" / "derived" / "fsa_wsf_rt06_2026-11-05.csv"
        rt11_path = project / "data" / "derived" / "fsa_wsf_rt11_2026-11-05.csv"
        if not all(p.exists() for p in (enriched, rt01_path, rt06_path, rt11_path)):
            pytest.skip("real profiled source files are not present")

        ids = {"04139800", "03088800", "00219300", "00214900", "10166659", "00170764"}
        with enriched.open(encoding="utf-8", newline="") as fh:
            records = [r for r in csv.DictReader(fh) if r.get("opeid8") in ids]
        closed_opeid6s = frozenset(r.get("opeid6", "") for r in records)
        rt01_by_opeid8, rt01_by_opeid6 = _build_rt01_indexes(rt01_path)
        result = classify_records(
            records,
            rt06_idx=_build_rt06_index(rt06_path, closed_opeid6s),
            rt11_idx=_build_rt11_index(rt11_path),
            rt01_by_opeid8=rt01_by_opeid8,
            rt01_by_opeid6=rt01_by_opeid6,
        )
        by_id = {r["opeid8"]: r for r in result}

        for opeid8 in {"04139800", "03088800", "00219300", "00214900"}:
            assert by_id[opeid8]["closure_type"] != "merger_absorption"
            assert by_id[opeid8]["replacement_opeid8"] == ""
            assert by_id[opeid8]["review_required"] == "true"

        for opeid8 in {"10166659", "00170764"}:
            assert by_id[opeid8]["closure_type"] == "unclassified"
            assert "postsecondary_location_identity_unresolved" in by_id[opeid8]["review_reason"]


# ---------------------------------------------------------------------------
# TestLoadOverrides
# ---------------------------------------------------------------------------

def _write_overrides_csv(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "closure_type_overrides.csv"
    fields = [
        "closure_event_id", "opeid8", "review_status", "publication_decision",
        "closure_type", "closure_type_basis", "location_operations_ceased",
        "parent_continued", "verified_closed_date_iso", "date_basis",
        "date_evidence_url", "evidence_fingerprint", "editor_note",
    ]
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
        assert "legacy:00219300" in ov
        assert ov["legacy:00219300"]["closure_type"] == "merger_absorption"
        assert "URI" in ov["legacy:00219300"]["closure_type_basis"]

    def test_loads_multiple_rows(self, tmp_path):
        p = _write_overrides_csv(tmp_path, [
            {"opeid8": "00219300", "closure_type": "merger_absorption",
             "closure_type_basis": "Editorial A"},
            {"opeid8": "03088800", "closure_type": "merger_absorption",
             "closure_type_basis": "Editorial B"},
        ])
        ov = _load_overrides(p)
        assert len(ov) == 2
        assert "legacy:00219300" in ov
        assert "legacy:03088800" in ov

    def test_pending_review_row_is_not_active_override(self, tmp_path):
        p = _write_overrides_csv(tmp_path, [{
            "closure_event_id": "fsa:00219300:2018-05-09",
            "opeid8": "00219300",
            "review_status": "pending",
            "closure_type": "",
            "closure_type_basis": "",
        }])
        assert _load_overrides(p) == {}

    def test_event_specific_resolved_row_uses_event_key(self, tmp_path):
        p = _write_overrides_csv(tmp_path, [{
            "closure_event_id": "fsa:00219300:2018-05-09",
            "opeid8": "00219300",
            "review_status": "resolved",
            "publication_decision": "include_institution",
            "closure_type": "institutional_closure",
            "closure_type_basis": "Editorial: institution ceased operations",
        }])
        ov = _load_overrides(p)
        assert list(ov) == ["fsa:00219300:2018-05-09"]

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

    def test_legacy_for_profit_closure_is_normalized(self, tmp_path):
        p = _write_overrides_csv(tmp_path, [{
            "closure_event_id": "fsa:03005400:2026-10-02",
            "opeid8": "03005400",
            "review_status": "resolved",
            "publication_decision": "include_institution",
            "closure_type": "for_profit_closure",
            "closure_type_basis": "Editorial: institution ceased operations",
        }])
        ov = _load_overrides(p)
        assert ov["fsa:03005400:2026-10-02"]["closure_type"] == "institutional_closure"

    def test_empty_file_returns_empty_dict(self, tmp_path):
        p = _write_overrides_csv(tmp_path, [])
        ov = _load_overrides(p)
        assert ov == {}


# ---------------------------------------------------------------------------
# TestClassifyRecordsOverrides
# ---------------------------------------------------------------------------

class TestClassifyRecordsOverrides:
    def test_override_supersedes_institutional_closure(self, tmp_path):
        # Without override: institutional closure; with override: merger absorption.
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
        assert len(ov) >= 4, "Expected the four reviewed merger/affiliation cases"
        assert "fsa:00219300:2018-05-09" in ov, "Mount Ida decision missing"
        assert "fsa:03088800:2020-06-30" in ov, "Watkins College decision missing"
        for opeid8, entry in ov.items():
            assert entry["closure_type"] in EVENT_TYPES, (
                f"Production override for {opeid8} has unknown type {entry['closure_type']!r}"
            )


class TestInstitutionCeasedEvidence:
    """Stale RT01 'open' siblings are overridden by IPEDS silence or FH confirmation."""

    _RT01_STALE_SIBLING = {"001002": [
        {"opeid8": "00100200", "close_date_iso": "2018-06-01", "reinstate_date_iso": ""},
        {"opeid8": "00100201", "close_date_iso": "", "reinstate_date_iso": ""},
    ]}
    _RT01_RECENT = {"001002": [
        {"opeid8": "00100200", "close_date_iso": "2025-06-01", "reinstate_date_iso": ""},
        {"opeid8": "00100201", "close_date_iso": "", "reinstate_date_iso": ""},
    ]}
    _BASE = frozenset({"fsa:00100200:2018-06-01"})
    _BASE_RECENT = frozenset({"fsa:00100200:2025-06-01"})

    def _main(self, **kw):
        defaults = dict(closed_date_iso="2018-06-01", closed_year="2018",
                        control_label="for-profit", ipeds_cache_max_year="2025")
        defaults.update(kw)
        return _rec(**defaults)

    def _recent(self):
        return self._main(closed_date_iso="2025-06-01", closed_year="2025", ipeds_opeid6_last_year="2025")

    def test_ipeds_silence_makes_institution_closure(self):
        r = classify_records([self._main(ipeds_opeid6_last_year="2017")],
                             rt01_by_opeid6=self._RT01_STALE_SIBLING, auto_baseline=self._BASE)[0]
        assert r["parent_continued"] == "false"
        assert "stale" in r["parent_continued_basis"]
        assert r["publication_decision"] == "include_institution"

    def test_ipeds_reporting_continues_stays_in_review(self):
        r = classify_records([self._main(ipeds_opeid6_last_year="2020")],
                             rt01_by_opeid6=self._RT01_STALE_SIBLING, auto_baseline=self._BASE)[0]
        assert r["parent_continued"] == "true"
        assert r["review_required"] == "true"
        assert "ipeds_reporting_continued_after_closure" in r["review_reason"]

    def test_too_recent_for_ipeds_stays_in_review(self):
        r = classify_records([self._recent()], rt01_by_opeid6=self._RT01_RECENT,
                             auto_baseline=self._BASE_RECENT)[0]
        assert r["review_required"] == "true"

    def test_fh_confirmed_closure_resolves_recent_case(self):
        fh = {"111": [{"announcement_date": "2024-11-01", "source_url": "https://example.org/x"}]}
        r = classify_records([self._recent()], rt01_by_opeid6=self._RT01_RECENT, fh_closures=fh,
                             auto_baseline=self._BASE_RECENT)[0]
        assert r["parent_continued"] == "false"
        assert "https://example.org/x" in r["parent_continued_basis"]
        assert "fh_confirmed_institution_closure" in r["review_reason"]
        assert r["publication_decision"] == "include_institution"

    def test_fh_evidence_is_base_table_only(self):
        # A closure first seen after the freeze ignores FH data entirely.
        fh = {"111": [{"announcement_date": "2017-11-01", "source_url": "u"}]}
        r = classify_records([self._main(ipeds_opeid6_last_year="2020")],
                             rt01_by_opeid6=self._RT01_STALE_SIBLING, fh_closures=fh,
                             auto_baseline=frozenset())[0]
        assert r["parent_continued"] == "true"
        assert "fh_confirmed_institution_closure" not in r["review_reason"]
        assert r["review_required"] == "true"

    def test_ipeds_evidence_still_annotates_new_closures(self):
        r = classify_records([self._main(ipeds_opeid6_last_year="2017")],
                             rt01_by_opeid6=self._RT01_STALE_SIBLING, auto_baseline=frozenset())[0]
        assert r["parent_continued"] == "false"
        assert r["review_required"] == "true"
        assert "new_since_baseline" in r["review_reason"]

    def test_fh_announcement_far_from_close_date_ignored(self):
        fh = {"111": [{"announcement_date": "2010-01-01", "source_url": "u"}]}
        r = classify_records([self._main(ipeds_opeid6_last_year="2020")],
                             rt01_by_opeid6=self._RT01_STALE_SIBLING, fh_closures=fh,
                             auto_baseline=self._BASE)[0]
        assert r["parent_continued"] == "true"

    def test_load_fh_closures_keeps_confirmed_institution_closures_only(self, tmp_path):
        from classify_closure_events import _load_fh_closures
        p = tmp_path / "fh.csv"
        p.write_text(
            "unitid,cut_type,status,announcement_date,source_url\n"
            "111,institution_closure,confirmed,2024-01-01,a\n"
            "222,institution_closure,,2024-01-01,b\n"
            "333,campus_closure,confirmed,2024-01-01,c\n", encoding="utf-8")
        assert set(_load_fh_closures(p)) == {"111"}


class TestIpedsCombinedStatus:
    def test_combined_status_blocks_auto_publication(self):
        rt01 = {"001002": [
            {"opeid8": "00100200", "close_date_iso": "2018-06-01", "reinstate_date_iso": ""},
        ]}
        r = classify_records(
            [_rec(closed_date_iso="2018-06-01", closed_year="2018", ipeds_combined_after_closure="true")],
            rt01_by_opeid6=rt01, auto_baseline=frozenset({"fsa:00100200:2018-06-01"}))[0]
        assert r["parent_continued"] == "false"
        assert r["review_required"] == "true"
        assert "ipeds_status_combined_with_other_institution" in r["review_reason"]


class TestChainShutdownLocations:
    """Branch locations of a chain that shut down entirely are not campus closures."""

    _RT01 = {"001002": [
        {"opeid8": "00100200", "close_date_iso": "2018-06-01", "reinstate_date_iso": ""},
        {"opeid8": "00100203", "close_date_iso": "2018-05-01", "reinstate_date_iso": ""},
        {"opeid8": "00100204", "close_date_iso": "", "reinstate_date_iso": ""},  # stale "open" row
    ]}
    _BASE = frozenset({"fsa:00100203:2018-05-01", "fsa:00100200:2018-06-01"})

    def _branch(self, last="2017"):
        return _rec(opeid8="00100203", is_main_campus="false", location_suffix="03", iclevel="1",
                    closed_date_iso="2018-05-01", closed_year="2018",
                    ipeds_opeid6_last_year=last, ipeds_cache_max_year="2025")

    def _main(self):
        return _rec(closed_date_iso="2018-06-01", closed_year="2018",
                    ipeds_opeid6_last_year="2017", ipeds_cache_max_year="2025")

    def test_location_closed_with_institution(self):
        rows = classify_records([self._branch(), self._main()], rt01_by_opeid6=self._RT01,
                                auto_baseline=self._BASE)
        branch = next(r for r in rows if r["opeid8"] == "00100203")
        assert branch["closure_type"] == "location_closed_with_institution"
        assert branch["publication_decision"] == "exclude"
        assert branch["review_required"] == "false"
        main = next(r for r in rows if r["opeid8"] == "00100200")
        assert main["publication_decision"] == "include_institution"

    def test_parent_gone_without_main_close_goes_to_review(self):
        r = classify_records([self._branch()], rt01_by_opeid6=self._RT01, auto_baseline=self._BASE)[0]
        assert r["closure_type"] == "branch_closure"
        assert "ipeds_parent_inactive_after_closure" in r["review_reason"]
        assert r["review_required"] == "true"

    def test_parent_still_active_stays_campus_closure(self):
        rows = classify_records([self._branch(last="2024"), self._main()], rt01_by_opeid6=self._RT01,
                                auto_baseline=self._BASE)
        branch = next(r for r in rows if r["opeid8"] == "00100203")
        assert branch["publication_decision"] == "include_campus"


class TestQueueReductionRules:
    """Out-of-scope rows auto-excluded in the base table; suggested for new closures."""

    _BASE = frozenset({"fsa:00100200:2018-06-01", "fsa:00100203:2018-06-01"})
    _RT01_MAIN_ALONE = {"001002": [
        {"opeid8": "00100200", "close_date_iso": "2018-06-01", "reinstate_date_iso": ""},
    ]}

    def _main(self, **kw):
        d = dict(closed_date_iso="2018-06-01", closed_year="2018", is_traditional_college="false",
                 control_label="nonprofit", ipeds_opeid6_last_year="2017", ipeds_cache_max_year="2025")
        d.update(kw)
        return _rec(**d)

    def _loc(self, **kw):
        d = dict(opeid8="00100203", is_main_campus="false", location_suffix="03", iclevel="1",
                 closed_date_iso="2018-06-01", closed_year="2018", ipeds_cache_max_year="2025")
        d.update(kw)
        return _rec(**d)

    def test_nonprofit_lt2yr_main_is_institution_closure(self):
        r = classify_records([self._main(sector="8")], rt01_by_opeid6=self._RT01_MAIN_ALONE,
                             auto_baseline=self._BASE)[0]
        assert r["closure_type"] == "institutional_closure"
        assert r["publication_decision"] == "include_institution"

    def test_public_lt2yr_main_excluded_as_career_tech(self):
        r = classify_records([self._main(sector="7", control_label="public")],
                             rt01_by_opeid6=self._RT01_MAIN_ALONE, auto_baseline=self._BASE)[0]
        assert r["closure_type"] == "public_career_tech_program"
        assert (r["review_required"], r["publication_decision"]) == ("false", "exclude")

    def test_new_closure_gets_suggestion_not_auto_exclusion(self):
        r = classify_records([self._main(sector="7", control_label="public")],
                             rt01_by_opeid6=self._RT01_MAIN_ALONE, auto_baseline=frozenset())[0]
        assert r["review_required"] == "true"
        assert "suggested_exclude_public_career_tech_program" in r["review_reason"]

    def test_foreign_location_excluded(self):
        r = classify_records([self._main(sector="2", country="GERMANY", is_traditional_college="true")],
                             rt01_by_opeid6=self._RT01_MAIN_ALONE, auto_baseline=self._BASE)[0]
        assert r["closure_type"] == "foreign_location"
        assert r["publication_decision"] == "exclude"

    def test_location_without_ipeds_record_excluded(self):
        r = classify_records([self._loc(match_method="no_match")], auto_baseline=self._BASE)[0]
        assert r["closure_type"] == "location_not_in_ipeds"
        assert r["review_required"] == "false"

    def test_unknown_parent_resolved_by_ipeds_activity(self):
        # No RT01 sibling data at all, but the parent reports to IPEDS after the close.
        r = classify_records([self._loc(ipeds_opeid6_last_year="2022")], auto_baseline=self._BASE)[0]
        assert r["parent_continued"] == "true"
        assert r["closure_type"] == "branch_closure"
        assert r["publication_decision"] == "include_campus"


class TestCampusNeedsOwnIpedsRecord:
    _RT01 = {"001002": [
        {"opeid8": "00100200", "close_date_iso": "", "reinstate_date_iso": ""},
        {"opeid8": "00100203", "close_date_iso": "2020-06-01", "reinstate_date_iso": ""},
    ]}
    _BASE = frozenset({"fsa:00100203:2020-06-01"})

    def _loc(self, method):
        return _rec(opeid8="00100203", is_main_campus="false", location_suffix="03", iclevel="1",
                    match_method=method, location_name="Viterbo University - Portage High School")

    def test_parent_only_match_is_not_a_campus_closure(self):
        r = classify_records([self._loc("matched_opeid6_single_yr2019")],
                             rt01_by_opeid6=self._RT01, auto_baseline=self._BASE)[0]
        assert r["closure_type"] == "location_not_in_ipeds"
        assert r["publication_decision"] == "exclude"

    def test_own_record_is_a_campus_closure(self):
        r = classify_records([self._loc("matched_opeid8_yr2019")],
                             rt01_by_opeid6=self._RT01, auto_baseline=self._BASE)[0]
        assert r["publication_decision"] == "include_campus"


class TestForeignScopeRule:
    def test_new_foreign_closure_is_excluded_without_review(self):
        r = classify_records([_rec(country="ENGLAND", closed_date_iso="2026-10-01", closed_year="2026")],
                             auto_baseline=frozenset())[0]
        assert r["closure_type"] == "foreign_location"
        assert (r["review_required"], r["publication_decision"]) == ("false", "exclude")


class TestTerritoryScope:
    def test_freely_associated_state_is_foreign(self):
        r = classify_records([_rec(state="FM")], auto_baseline=frozenset())[0]
        assert r["closure_type"] == "foreign_location"

    def test_us_territory_stays_in_scope(self):
        r = classify_records([_rec(state="GU")], auto_baseline=frozenset())[0]
        assert r["closure_type"] != "foreign_location"
