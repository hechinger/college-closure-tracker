"""Tests for scripts/build_ipeds_crosswalk.py."""

import csv
import io
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_ipeds_crosswalk import (
    _closed_year,
    _empty_ipeds,
    _enrollment_band,
    _ipeds_from_row,
    _is_valid_opeid8,
    build_opeid6_rows_index,
    build_opeid8_index,
    enrich_closure_records,
    load_year_cache,
    write_crosswalk_csv,
    write_publication_table,
    CROSSWALK_FIELDS,
    ENRICHED_FIELDS,
    PUBLICATION_TABLE_FIELDS,
    REVIEW_FIELDS,
    LEVEL_LABELS,
    CONTROL_LABELS,
    SECTOR_LABELS,
    SIZE_LABELS,
    TRADITIONAL_SECTORS,
    MIN_YEAR,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cache_csv(rows: list[dict]) -> str:
    """Render a list of dicts as a CSV string with the year-cache columns."""
    fields = ["opeid", "unitid", "year", "institution_name", "city", "state",
              "level", "control", "sector", "enrollment_headcount_total"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def _write_cache_file(dir_: Path, year: int, rows: list[dict]) -> None:
    path = dir_ / f"ipeds_financial_health_year_{year}.csv"
    path.write_text(_make_cache_csv(rows), encoding="utf-8")


def _sample_row(opeid: str, unitid: str, year: int, **kwargs) -> dict:
    return {
        "opeid": opeid,
        "unitid": unitid,
        "year": str(year),
        "institution_name": kwargs.get("institution_name", "Test U"),
        "city": kwargs.get("city", "Testville"),
        "state": kwargs.get("state", "TX"),
        "level": kwargs.get("level", "1"),
        "control": kwargs.get("control", "2"),
        "sector": kwargs.get("sector", "2"),
        "enrollment_headcount_total": kwargs.get("enrollment_headcount_total", "1000"),
    }


def _fsa_rec(opeid8: str, closed_iso: str, **kwargs) -> dict:
    return {
        "opeid8":          opeid8,
        "opeid6":          opeid8[:6],
        "location_suffix": opeid8[6:],
        "school_name":     kwargs.get("school_name", "Test College"),
        "location_name":   "",
        "city":            "Testville",
        "state":           "TX",
        "country":         "US",
        "closed_date":     "10/31/2026",
        "closed_date_iso": closed_iso,
        "source_date":     "2026-11-08",
        "source_file":     "test.csv",
    }


# ---------------------------------------------------------------------------
# _is_valid_opeid8
# ---------------------------------------------------------------------------

class TestIsValidOpeid8:
    def test_valid(self):
        assert _is_valid_opeid8("00100200") is True

    def test_too_short(self):
        assert _is_valid_opeid8("001002") is False

    def test_too_long(self):
        assert _is_valid_opeid8("001002000") is False

    def test_missing_sentinel(self):
        assert _is_valid_opeid8("-2") is False

    def test_missing_sentinel_neg3(self):
        assert _is_valid_opeid8("-3") is False

    def test_empty(self):
        assert _is_valid_opeid8("") is False

    def test_has_letters(self):
        assert _is_valid_opeid8("0010020A") is False

    def test_all_zeros(self):
        assert _is_valid_opeid8("00000000") is True


# ---------------------------------------------------------------------------
# _closed_year
# ---------------------------------------------------------------------------

class TestClosedYear:
    def test_full_iso(self):
        assert _closed_year("2023-05-15") == 2023

    def test_year_only(self):
        assert _closed_year("2017") == 2017

    def test_empty(self):
        assert _closed_year("") is None

    def test_non_digit_start(self):
        assert _closed_year("unknown") is None

    def test_partial(self):
        assert _closed_year("202") is None


# ---------------------------------------------------------------------------
# load_year_cache
# ---------------------------------------------------------------------------

class TestLoadYearCache:
    def test_loads_valid_rows(self, tmp_path):
        _write_cache_file(tmp_path, 2022, [
            _sample_row("00100200", "123456", 2022),
            _sample_row("00200300", "234567", 2022),
        ])
        rows = load_year_cache(tmp_path)
        assert len(rows) == 2

    def test_skips_missing_opeid(self, tmp_path):
        _write_cache_file(tmp_path, 2022, [
            _sample_row("-2", "123456", 2022),
            _sample_row("00200300", "234567", 2022),
        ])
        rows = load_year_cache(tmp_path)
        assert len(rows) == 1
        assert rows[0]["opeid8"] == "00200300"

    def test_skips_missing_unitid(self, tmp_path):
        _write_cache_file(tmp_path, 2022, [
            _sample_row("00100200", "-3", 2022),
        ])
        rows = load_year_cache(tmp_path)
        assert len(rows) == 0

    def test_opeid6_derived(self, tmp_path):
        _write_cache_file(tmp_path, 2022, [_sample_row("00100200", "111", 2022)])
        rows = load_year_cache(tmp_path)
        assert rows[0]["opeid6"] == "001002"

    def test_multiple_years(self, tmp_path):
        _write_cache_file(tmp_path, 2020, [_sample_row("00100200", "111", 2020)])
        _write_cache_file(tmp_path, 2021, [_sample_row("00100200", "111", 2021)])
        rows = load_year_cache(tmp_path)
        assert len(rows) == 2
        assert {r["year"] for r in rows} == {"2020", "2021"}

    def test_empty_dir(self, tmp_path):
        assert load_year_cache(tmp_path) == []

    def test_bom_handled(self, tmp_path):
        # utf-8-sig (BOM) should be stripped
        path = tmp_path / "ipeds_financial_health_year_2022.csv"
        path.write_text(
            "﻿opeid,unitid,year,institution_name,city,state,level,control,sector,enrollment_headcount_total\n"
            "00100200,999,2022,Test,City,TX,1,2,2,500\n",
            encoding="utf-8",
        )
        rows = load_year_cache(tmp_path)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# build_opeid8_index
# ---------------------------------------------------------------------------

class TestBuildOpeid8Index:
    def _rows(self):
        return [
            {**_sample_row("00100200", "111", 2022), "opeid8": "00100200", "opeid6": "001002"},
            {**_sample_row("00200300", "222", 2022), "opeid8": "00200300", "opeid6": "002003"},
        ]

    def test_basic_lookup(self):
        rows = [
            {"opeid8": "00100200", "opeid6": "001002", "unitid": "111", "year": "2022",
             "institution_name": "", "city": "", "state": "", "level": "1",
             "control": "2", "sector": "2", "enrollment_headcount_total": ""},
        ]
        idx = build_opeid8_index(rows)
        assert ("00100200", "2022") in idx

    def test_multi_unitid_flagged(self):
        rows = [
            {"opeid8": "00100200", "opeid6": "001002", "unitid": "111", "year": "2022",
             "institution_name": "", "city": "", "state": "", "level": "1",
             "control": "2", "sector": "2", "enrollment_headcount_total": ""},
            {"opeid8": "00100200", "opeid6": "001002", "unitid": "999", "year": "2022",
             "institution_name": "", "city": "", "state": "", "level": "1",
             "control": "2", "sector": "2", "enrollment_headcount_total": ""},
        ]
        idx = build_opeid8_index(rows)
        assert idx[("00100200", "2022")].get("_multi_unitid") is True

    def test_single_unitid_not_flagged(self):
        rows = [
            {"opeid8": "00100200", "opeid6": "001002", "unitid": "111", "year": "2022",
             "institution_name": "", "city": "", "state": "", "level": "1",
             "control": "2", "sector": "2", "enrollment_headcount_total": ""},
        ]
        idx = build_opeid8_index(rows)
        assert not idx[("00100200", "2022")].get("_multi_unitid")


# ---------------------------------------------------------------------------
# build_opeid6_rows_index
# ---------------------------------------------------------------------------

class TestBuildOpeid6RowsIndex:
    def test_groups_by_opeid6_year(self):
        rows = [
            {"opeid8": "00100200", "opeid6": "001002", "unitid": "111", "year": "2022",
             "institution_name": "", "city": "", "state": "", "level": "1",
             "control": "2", "sector": "2", "enrollment_headcount_total": ""},
            {"opeid8": "00100201", "opeid6": "001002", "unitid": "222", "year": "2022",
             "institution_name": "", "city": "", "state": "", "level": "1",
             "control": "2", "sector": "2", "enrollment_headcount_total": ""},
        ]
        idx = build_opeid6_rows_index(rows)
        assert len(idx[("001002", "2022")]) == 2

    def test_different_years_separate_keys(self):
        rows = [
            {"opeid8": "00100200", "opeid6": "001002", "unitid": "111", "year": "2021",
             "institution_name": "", "city": "", "state": "", "level": "1",
             "control": "2", "sector": "2", "enrollment_headcount_total": ""},
            {"opeid8": "00100200", "opeid6": "001002", "unitid": "111", "year": "2022",
             "institution_name": "", "city": "", "state": "", "level": "1",
             "control": "2", "sector": "2", "enrollment_headcount_total": ""},
        ]
        idx = build_opeid6_rows_index(rows)
        assert ("001002", "2021") in idx
        assert ("001002", "2022") in idx


# ---------------------------------------------------------------------------
# enrich_closure_records
# ---------------------------------------------------------------------------

def _make_indexes(cache_rows):
    opeid8_idx      = build_opeid8_index(cache_rows)
    opeid6_rows_idx = build_opeid6_rows_index(cache_rows)
    return opeid8_idx, opeid6_rows_idx


class TestEnrichClosureRecords:

    def test_matched_opeid8(self):
        cache = [{"opeid8": "00100200", "opeid6": "001002", "unitid": "111",
                  "year": "2022", "institution_name": "Test U", "city": "C",
                  "state": "TX", "level": "1", "control": "2", "sector": "2",
                  "enrollment_headcount_total": "500"}]
        closures = [_fsa_rec("00100200", "2023-05-01")]
        enriched, review = enrich_closure_records(closures, *_make_indexes(cache))
        assert len(enriched) == 1
        assert enriched[0]["match_method"].startswith("matched_opeid8")
        assert enriched[0]["unitid"] == "111"
        assert enriched[0]["iclevel"] == "1"
        assert enriched[0]["iclevel_label"] == "4yr+"
        assert enriched[0]["control_label"] == "nonprofit"
        assert len(review) == 0

    def test_opeid8_lookback_up_to_max(self):
        # Closure 2024; cache row in 2019 (5 years back = edge of window)
        cache = [{"opeid8": "00100200", "opeid6": "001002", "unitid": "333",
                  "year": "2019", "institution_name": "X", "city": "C",
                  "state": "TX", "level": "2", "control": "1", "sector": "4",
                  "enrollment_headcount_total": "200"}]
        closures = [_fsa_rec("00100200", "2024-01-01")]
        enriched, _ = enrich_closure_records(closures, *_make_indexes(cache))
        assert enriched[0]["match_method"] == "matched_opeid8_yr2019"

    def test_opeid8_beyond_lookback_not_matched(self):
        # Closure 2024; cache row in 2018 (6 years back, beyond MAX_LOOKBACK=5)
        cache = [{"opeid8": "00100200", "opeid6": "001002", "unitid": "333",
                  "year": "2018", "institution_name": "X", "city": "C",
                  "state": "TX", "level": "2", "control": "1", "sector": "4",
                  "enrollment_headcount_total": "200"}]
        closures = [_fsa_rec("00100200", "2024-01-01")]
        enriched, review = enrich_closure_records(closures, *_make_indexes(cache))
        assert enriched[0]["match_method"] == "no_match"
        assert len(review) == 1

    def test_no_ipeds_coverage_for_old_closure(self):
        cache = [{"opeid8": "00100200", "opeid6": "001002", "unitid": "111",
                  "year": "2014", "institution_name": "X", "city": "C",
                  "state": "TX", "level": "1", "control": "2", "sector": "2",
                  "enrollment_headcount_total": "100"}]
        closures = [_fsa_rec("00100200", "2014-05-01")]
        enriched, review = enrich_closure_records(closures, *_make_indexes(cache))
        assert enriched[0]["match_method"] == "no_ipeds_coverage"
        assert len(review) == 0   # expected gap, not a data quality issue

    def test_no_closure_date(self):
        cache = []
        closures = [_fsa_rec("00100200", "")]
        enriched, review = enrich_closure_records(closures, *_make_indexes(cache))
        assert enriched[0]["match_method"] == "no_closure_date"
        assert len(review) == 0

    def test_opeid8_multi_unitid_goes_to_review(self):
        cache = [
            {"opeid8": "00100200", "opeid6": "001002", "unitid": "111",
             "year": "2022", "institution_name": "X", "city": "C", "state": "TX",
             "level": "1", "control": "2", "sector": "2", "enrollment_headcount_total": ""},
            {"opeid8": "00100200", "opeid6": "001002", "unitid": "999",
             "year": "2022", "institution_name": "Y", "city": "C", "state": "TX",
             "level": "1", "control": "2", "sector": "2", "enrollment_headcount_total": ""},
        ]
        closures = [_fsa_rec("00100200", "2023-01-01")]
        enriched, review = enrich_closure_records(closures, *_make_indexes(cache))
        assert enriched[0]["match_method"] == "review_opeid8_multi_unitid"
        assert enriched[0]["unitid"] == ""   # no IPEDS attached
        assert len(review) == 1
        assert review[0]["match_method"] == "review_opeid8_multi_unitid"

    def test_opeid6_fallback_single_candidate(self):
        # OPEID8 00100201 not in cache; OPEID6 001002 maps to exactly 1 UNITID
        cache = [{"opeid8": "00100200", "opeid6": "001002", "unitid": "111",
                  "year": "2022", "institution_name": "Main Campus", "city": "C",
                  "state": "TX", "level": "1", "control": "2", "sector": "2",
                  "enrollment_headcount_total": "900"}]
        closures = [_fsa_rec("00100201", "2023-06-01")]
        enriched, review = enrich_closure_records(closures, *_make_indexes(cache))
        assert enriched[0]["match_method"].startswith("matched_opeid6_single")
        assert enriched[0]["unitid"] == "111"
        assert len(review) == 0

    def test_opeid6_fallback_multiple_candidates_goes_to_review(self):
        cache = [
            {"opeid8": "00100200", "opeid6": "001002", "unitid": "111",
             "year": "2022", "institution_name": "Campus A", "city": "C",
             "state": "TX", "level": "1", "control": "2", "sector": "2",
             "enrollment_headcount_total": ""},
            {"opeid8": "00100201", "opeid6": "001002", "unitid": "222",
             "year": "2022", "institution_name": "Campus B", "city": "C",
             "state": "TX", "level": "1", "control": "2", "sector": "2",
             "enrollment_headcount_total": ""},
        ]
        # Closure uses a different OPEID8 so Pass 1 misses; OPEID6 is ambiguous
        closures = [_fsa_rec("00100299", "2023-01-01")]
        enriched, review = enrich_closure_records(closures, *_make_indexes(cache))
        assert enriched[0]["match_method"] == "review_opeid6_ambiguous"
        assert enriched[0]["unitid"] == ""
        assert len(review) == 1
        assert "111" in review[0]["opeid6_candidate_unitids"]
        assert "222" in review[0]["opeid6_candidate_unitids"]

    def test_no_match_in_window_goes_to_review(self):
        cache = []
        closures = [_fsa_rec("00100200", "2020-01-01")]
        enriched, review = enrich_closure_records(closures, *_make_indexes(cache))
        assert enriched[0]["match_method"] == "no_match"
        assert len(review) == 1

    def test_opeid8_match_preferred_over_opeid6(self):
        """When OPEID8 matches, we never fall through to OPEID6."""
        cache = [
            {"opeid8": "00100200", "opeid6": "001002", "unitid": "EXACT",
             "year": "2022", "institution_name": "X", "city": "C", "state": "TX",
             "level": "1", "control": "2", "sector": "2", "enrollment_headcount_total": ""},
            # A second institution sharing OPEID6 — should be ignored
            {"opeid8": "00100201", "opeid6": "001002", "unitid": "OPEID6ONLY",
             "year": "2022", "institution_name": "Y", "city": "C", "state": "TX",
             "level": "2", "control": "1", "sector": "4", "enrollment_headcount_total": ""},
        ]
        closures = [_fsa_rec("00100200", "2023-01-01")]
        enriched, _ = enrich_closure_records(closures, *_make_indexes(cache))
        assert enriched[0]["unitid"] == "EXACT"
        assert enriched[0]["match_method"].startswith("matched_opeid8")

    def test_all_enriched_rows_have_required_fields(self):
        cache = [{"opeid8": "00100200", "opeid6": "001002", "unitid": "111",
                  "year": "2022", "institution_name": "U", "city": "C",
                  "state": "TX", "level": "1", "control": "2", "sector": "2",
                  "enrollment_headcount_total": "500"}]
        closures = [_fsa_rec("00100200", "2023-01-01")]
        enriched, _ = enrich_closure_records(closures, *_make_indexes(cache))
        for field in ENRICHED_FIELDS:
            assert field in enriched[0], f"Missing field: {field}"

    def test_all_review_rows_have_required_fields(self):
        cache = []
        closures = [_fsa_rec("00100200", "2020-01-01")]
        _, review = enrich_closure_records(closures, *_make_indexes(cache))
        for field in REVIEW_FIELDS:
            assert field in review[0], f"Missing field: {field}"


# ---------------------------------------------------------------------------
# write_crosswalk_csv
# ---------------------------------------------------------------------------

class TestWriteCrosswalkCsv:
    def test_writes_expected_fields(self, tmp_path):
        rows = [{"opeid8": "00100200", "opeid6": "001002", "unitid": "111",
                 "year": "2022", "institution_name": "U", "city": "C", "state": "TX",
                 "level": "1", "control": "2", "sector": "2",
                 "enrollment_headcount_total": "500"}]
        out = tmp_path / "crosswalk.csv"
        write_crosswalk_csv(rows, out)
        with out.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            assert set(reader.fieldnames) == set(CROSSWALK_FIELDS)
            data = list(reader)
        assert len(data) == 1
        assert data[0]["opeid8"] == "00100200"


# ---------------------------------------------------------------------------
# Constants / labels
# ---------------------------------------------------------------------------

class TestConstants:
    def test_level_labels_coverage(self):
        assert "1" in LEVEL_LABELS
        assert "2" in LEVEL_LABELS
        assert "3" in LEVEL_LABELS

    def test_control_labels_coverage(self):
        assert "1" in CONTROL_LABELS
        assert "2" in CONTROL_LABELS
        assert "3" in CONTROL_LABELS

    def test_min_year(self):
        assert MIN_YEAR == 2014

    def test_sector_labels_coverage(self):
        for code in ("1", "2", "3", "4", "5", "6", "7", "8"):
            assert code in SECTOR_LABELS

    def test_traditional_sectors_are_public_and_nonprofit_2yr_4yr(self):
        # IPEDS SECTOR: 1 public 4yr, 2 nonprofit 4yr, 4 public 2yr, 5 nonprofit 2yr.
        # Sector 3 is for-profit 4yr and must not be "traditional".
        assert TRADITIONAL_SECTORS == frozenset({"1", "2", "4", "5"})

    @pytest.mark.parametrize("code,label", [
        ("1", "public_4yr"), ("2", "private_nonprofit_4yr"), ("3", "for_profit_4yr"),
        ("4", "public_2yr"), ("5", "private_nonprofit_2yr"), ("6", "for_profit_2yr"),
        ("7", "public_lt2yr"), ("8", "private_nonprofit_lt2yr"), ("9", "for_profit_lt2yr"),
    ])
    def test_sector_labels_follow_ipeds_control_by_level(self, code, label):
        assert SECTOR_LABELS[code] == label

    def test_size_labels_coverage(self):
        for code in ("1", "2", "3", "4", "5"):
            assert code in SIZE_LABELS


# ---------------------------------------------------------------------------
# _enrollment_band
# ---------------------------------------------------------------------------

class TestEnrollmentBand:
    """_enrollment_band maps IPEDS size codes (1-5) to official federal labels."""

    def test_code_1(self):
        assert _enrollment_band("1") == "Under 1,000"

    def test_code_2(self):
        assert _enrollment_band("2") == "1,000–4,999"

    def test_code_3(self):
        assert _enrollment_band("3") == "5,000–9,999"

    def test_code_4(self):
        assert _enrollment_band("4") == "10,000–19,999"

    def test_code_5(self):
        assert _enrollment_band("5") == "20,000 and above"

    def test_empty_returns_empty(self):
        assert _enrollment_band("") == ""

    def test_unknown_code_returns_empty(self):
        assert _enrollment_band("9") == ""

    def test_whitespace_stripped(self):
        assert _enrollment_band("  3  ") == "5,000–9,999"

    def test_zero_returns_empty(self):
        assert _enrollment_band("0") == ""

    def test_non_numeric_returns_empty(self):
        assert _enrollment_band("n/a") == ""


# ---------------------------------------------------------------------------
# Enrichment field tests — new fields added in Phase 7 enrichment pass
# ---------------------------------------------------------------------------

class TestEnrichmentNewFields:

    def _base_cache(self, opeid8="00100200", sector="2", enroll="1500", size="2"):
        return [{"opeid8": opeid8, "opeid6": opeid8[:6], "unitid": "111",
                 "year": "2022", "institution_name": "Test U", "city": "C",
                 "state": "TX", "level": "1", "control": "2", "sector": sector,
                 "enrollment_headcount_total": enroll, "size": size}]

    def test_is_main_campus_true_for_00_suffix(self):
        closures = [_fsa_rec("00100200", "2023-01-01")]  # suffix "00"
        enriched, _ = enrich_closure_records(closures, *_make_indexes(self._base_cache()))
        assert enriched[0]["is_main_campus"] == "true"

    def test_is_main_campus_false_for_branch(self):
        # opeid8 "00100201" → suffix "01" → not main campus
        cache = [{"opeid8": "00100200", "opeid6": "001002", "unitid": "111",
                  "year": "2022", "institution_name": "X", "city": "C", "state": "TX",
                  "level": "1", "control": "2", "sector": "2",
                  "enrollment_headcount_total": "900", "size": "1"}]
        closures = [_fsa_rec("00100201", "2023-01-01")]  # suffix "01"
        enriched, _ = enrich_closure_records(closures, *_make_indexes(cache))
        assert enriched[0]["is_main_campus"] == "false"

    def test_approach1_always_true(self):
        closures = [_fsa_rec("00100200", "2023-01-01")]
        enriched, _ = enrich_closure_records(closures, *_make_indexes(self._base_cache()))
        assert enriched[0]["approach1_all_campuses"] == "true"

    def test_approach2_true_for_main_campus(self):
        closures = [_fsa_rec("00100200", "2023-01-01")]
        enriched, _ = enrich_closure_records(closures, *_make_indexes(self._base_cache()))
        assert enriched[0]["approach2_main_campus_only"] == "true"

    def test_approach2_false_for_branch(self):
        cache = self._base_cache()  # opeid8 "00100200" in cache
        closures = [_fsa_rec("00100201", "2023-01-01")]  # branch suffix "01"
        enriched, _ = enrich_closure_records(closures, *_make_indexes(cache))
        assert enriched[0]["approach2_main_campus_only"] == "false"

    def test_approach3_always_true(self):
        closures = [_fsa_rec("00100200", "2023-01-01")]
        enriched, _ = enrich_closure_records(closures, *_make_indexes(self._base_cache()))
        assert enriched[0]["approach3_system_level"] == "true"

    def test_institution_category_from_sector(self):
        closures = [_fsa_rec("00100200", "2023-01-01")]
        enriched, _ = enrich_closure_records(closures, *_make_indexes(self._base_cache(sector="2")))
        assert enriched[0]["institution_category"] == "private_nonprofit_4yr"

    def test_is_traditional_college_true_for_public_nonprofit_2yr_4yr(self):
        for sector in ("1", "2", "4", "5"):
            closures = [_fsa_rec("00100200", "2023-01-01")]
            enriched, _ = enrich_closure_records(
                closures, *_make_indexes(self._base_cache(sector=sector))
            )
            assert enriched[0]["is_traditional_college"] == "true", f"sector {sector}"

    def test_is_traditional_college_false_for_for_profit_and_lt2yr(self):
        for sector in ("3", "6", "7", "8", "9"):
            closures = [_fsa_rec("00100200", "2023-01-01")]
            enriched, _ = enrich_closure_records(
                closures, *_make_indexes(self._base_cache(sector=sector))
            )
            assert enriched[0]["is_traditional_college"] == "false", f"sector {sector}"

    def test_enrollment_band_populated(self):
        closures = [_fsa_rec("00100200", "2023-01-01")]
        enriched, _ = enrich_closure_records(
            closures, *_make_indexes(self._base_cache(size="2"))
        )
        assert enriched[0]["enrollment_band"] == "1,000–4,999"

    def test_empty_ipeds_has_new_fields(self):
        result = _empty_ipeds()
        assert "institution_category" in result
        assert "is_traditional_college" in result
        assert "ipeds_size_code" in result
        assert "enrollment_band" in result

    def test_ipeds_from_row_sector_labels(self):
        row = {"unitid": "1", "institution_name": "X", "city": "C", "state": "TX",
               "level": "1", "control": "1", "sector": "4",
               "enrollment_headcount_total": "2000", "size": "2", "year": "2022"}
        result = _ipeds_from_row(row)
        assert result["institution_category"] == "public_2yr"
        assert result["is_traditional_college"] == "true"
        assert result["enrollment_band"] == "1,000–4,999"


# ---------------------------------------------------------------------------
# write_publication_table
# ---------------------------------------------------------------------------

def _enriched_row(**kwargs) -> dict:
    """Minimal enriched row with all required fields for publication table tests."""
    defaults = {
        "closed_date_iso":        "2020-06-01",
        "closed_year":            "2020",
        "school_name":            "Test College",
        "city":                   "Testville",
        "state":                  "TX",
        "institution_category":   "public_4yr",
        "control_label":          "public",
        "iclevel_label":          "4yr+",
        "enrollment_band":        "1,000–4,999",
        "is_main_campus":         "true",
        "is_suspicious_location": "false",
        "match_method":           "matched_opeid8_yr2019",
        "opeid8":                 "00100200",
        "opeid6":                 "001002",
        "unitid":                 "111",
        "approach1_all_campuses":     "true",
        "approach2_main_campus_only": "true",
        "approach3_system_level":     "true",
    }
    defaults.update(kwargs)
    return defaults


class TestWritePublicationTable:

    def test_filters_to_approach2_and_post_2015(self, tmp_path):
        rows = [
            _enriched_row(closed_year="2016", approach2_main_campus_only="true"),
            _enriched_row(closed_year="2016", approach2_main_campus_only="false"),  # branch
            _enriched_row(closed_year="2014", approach2_main_campus_only="true"),   # too old
        ]
        out = tmp_path / "pub.csv"
        result = write_publication_table(rows, out)
        assert len(result) == 1
        assert result[0]["closed_year"] == "2016"

    def test_excludes_missing_closed_year(self, tmp_path):
        rows = [
            _enriched_row(closed_year=""),
            _enriched_row(closed_year="2020"),
        ]
        out = tmp_path / "pub.csv"
        result = write_publication_table(rows, out)
        assert len(result) == 1

    def test_review_flag_suspicious_location(self, tmp_path):
        rows = [_enriched_row(is_suspicious_location="true")]
        out = tmp_path / "pub.csv"
        result = write_publication_table(rows, out)
        assert "suspicious_location" in result[0]["review_flag"]

    def test_review_flag_no_ipeds_match(self, tmp_path):
        rows = [_enriched_row(match_method="no_match")]
        out = tmp_path / "pub.csv"
        result = write_publication_table(rows, out)
        assert "no_ipeds_match" in result[0]["review_flag"]

    def test_review_flag_both(self, tmp_path):
        rows = [_enriched_row(is_suspicious_location="true", match_method="review_opeid6_ambiguous")]
        out = tmp_path / "pub.csv"
        result = write_publication_table(rows, out)
        assert "suspicious_location" in result[0]["review_flag"]
        assert "no_ipeds_match" in result[0]["review_flag"]

    def test_review_flag_empty_for_clean_row(self, tmp_path):
        rows = [_enriched_row()]
        out = tmp_path / "pub.csv"
        result = write_publication_table(rows, out)
        assert result[0]["review_flag"] == ""

    def test_sorted_by_close_date_descending(self, tmp_path):
        rows = [
            _enriched_row(closed_date_iso="2018-01-01", closed_year="2018"),
            _enriched_row(closed_date_iso="2022-06-15", closed_year="2022"),
            _enriched_row(closed_date_iso="2020-03-10", closed_year="2020"),
        ]
        out = tmp_path / "pub.csv"
        result = write_publication_table(rows, out)
        dates = [r["closed_date_iso"] for r in result]
        assert dates == sorted(dates, reverse=True)

    def test_csv_has_publication_table_fields(self, tmp_path):
        rows = [_enriched_row()]
        out = tmp_path / "pub.csv"
        write_publication_table(rows, out)
        with out.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            assert set(reader.fieldnames) == set(PUBLICATION_TABLE_FIELDS)

    def test_all_returned_rows_have_publication_fields(self, tmp_path):
        rows = [_enriched_row()]
        out = tmp_path / "pub.csv"
        result = write_publication_table(rows, out)
        for field in PUBLICATION_TABLE_FIELDS:
            assert field in result[0], f"Missing field: {field}"


# ---------------------------------------------------------------------------
# Integration: load_year_cache -> build indexes -> enrich
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_end_to_end_match(self, tmp_path):
        _write_cache_file(tmp_path, 2021, [
            _sample_row("00500600", "777", 2021,
                        institution_name="Valley College", state="CA",
                        level="2", control="1", sector="4",
                        enrollment_headcount_total="3000"),
        ])
        cache_rows      = load_year_cache(tmp_path)
        opeid8_idx      = build_opeid8_index(cache_rows)
        opeid6_rows_idx = build_opeid6_rows_index(cache_rows)
        closures = [_fsa_rec("00500600", "2022-06-30", school_name="Valley College")]
        enriched, review = enrich_closure_records(closures, opeid8_idx, opeid6_rows_idx)
        assert enriched[0]["unitid"] == "777"
        assert enriched[0]["iclevel_label"] == "2-4yr"
        assert enriched[0]["control_label"] == "public"
        assert enriched[0]["enrollment_headcount_total"] == "3000"
        assert len(review) == 0

    def test_end_to_end_no_coverage(self, tmp_path):
        _write_cache_file(tmp_path, 2014, [_sample_row("00500600", "777", 2014)])
        cache_rows      = load_year_cache(tmp_path)
        opeid8_idx      = build_opeid8_index(cache_rows)
        opeid6_rows_idx = build_opeid6_rows_index(cache_rows)
        # Closed in 2010 — well before MIN_YEAR
        closures = [_fsa_rec("00500600", "2010-05-01")]
        enriched, review = enrich_closure_records(closures, opeid8_idx, opeid6_rows_idx)
        assert enriched[0]["match_method"] == "no_ipeds_coverage"
        assert len(review) == 0

    def test_crosswalk_csv_roundtrip(self, tmp_path):
        _write_cache_file(tmp_path, 2023, [
            _sample_row("00100200", "123", 2023),
            _sample_row("00200300", "456", 2023),
        ])
        cache_rows = load_year_cache(tmp_path)
        out = tmp_path / "crosswalk.csv"
        write_crosswalk_csv(cache_rows, out)
        with out.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 2
        opeids = {r["opeid8"] for r in rows}
        assert opeids == {"00100200", "00200300"}


# ---------------------------------------------------------------------------
# Credential level (HDEGOFR1 coding) and extended history coverage
# ---------------------------------------------------------------------------

from build_ipeds_crosswalk import _credential_level, load_year_caches


class TestCredentialLevel:
    @pytest.mark.parametrize("code,label", [
        ("11", "Doctoral"), ("12", "Doctoral"), ("13", "Doctoral"), ("14", "Doctoral"),
        ("20", "Master's"), ("30", "Bachelor's"), ("40", "Bachelor's or above"),
        ("-3", "Bachelor's or above"),
    ])
    def test_four_year_uses_hdegofr1(self, code, label):
        assert _credential_level("1", code) == label

    def test_two_year_is_associate(self):
        assert _credential_level("2", "40") == "Associate's"

    def test_less_than_two_year(self):
        assert _credential_level("3", "0") == "Non-degree"
        assert _credential_level("3", "40") == "Certificate"


class TestHistoryCoverage:
    def _cache(self, year):
        return [{"opeid8": "00100200", "opeid6": "001002", "unitid": "111",
                 "year": str(year), "institution_name": "X", "city": "C",
                 "state": "TX", "level": "1", "control": "2", "sector": "2",
                 "enrollment_headcount_total": "100"}]

    def test_min_year_param_extends_coverage(self):
        closures = [_fsa_rec("00100200", "2009-05-01")]
        enriched, _ = enrich_closure_records(closures, *_make_indexes(self._cache(2007)), 2002)
        assert enriched[0]["match_method"] == "matched_opeid8_yr2007"

    def test_closure_at_or_before_min_year_is_gap(self):
        closures = [_fsa_rec("00100200", "2002-05-01")]
        enriched, review = enrich_closure_records(closures, *_make_indexes(self._cache(2002)), 2002)
        assert enriched[0]["match_method"] == "no_ipeds_coverage"
        assert review == []

    def test_load_year_caches_reads_history_files_and_dedupes(self, tmp_path):
        fh, hist = tmp_path / "fh", tmp_path / "hist"
        fh.mkdir(); hist.mkdir()
        _write_cache_file(fh, 2014, [_sample_row("00100200", "111", 2014)])
        (hist / "ipeds_history_year_2010.csv").write_text(
            _make_cache_csv([_sample_row("00100200", "111", 2010)]), encoding="utf-8")
        (hist / "ipeds_history_year_2014.csv").write_text(
            _make_cache_csv([_sample_row("00100200", "111", 2014)]), encoding="utf-8")
        rows = load_year_caches([fh, hist])
        assert sorted(r["year"] for r in rows) == ["2010", "2014"]


class TestContinuationFields:
    def test_opeid6_last_year_and_max_year_on_enriched_rows(self):
        from build_ipeds_crosswalk import build_opeid6_last_year
        base = {"opeid6": "001002", "institution_name": "X", "city": "C", "state": "TX",
                "level": "1", "control": "3", "sector": "3", "enrollment_headcount_total": "1"}
        cache = [
            {**base, "opeid8": "00100200", "unitid": "111", "year": "2016"},
            {**base, "opeid8": "00100201", "unitid": "112", "year": "2017"},
        ]
        last = build_opeid6_last_year(cache)
        assert last == {"001002": 2017}
        enriched, _ = enrich_closure_records(
            [_fsa_rec("00100200", "2018-01-01")], *_make_indexes(cache), 2002,
            opeid6_last_year=last, cache_max_year=2025)
        assert enriched[0]["ipeds_opeid6_last_year"] == "2017"
        assert enriched[0]["ipeds_cache_max_year"] == "2025"


class TestIpedsStatusSignals:
    _BASE = {"opeid6": "001002", "institution_name": "X", "city": "C", "state": "TX",
             "level": "1", "control": "3", "sector": "3", "enrollment_headcount_total": "1"}

    def test_closed_rows_do_not_count_as_continued_reporting(self):
        from build_ipeds_crosswalk import build_opeid6_last_year
        cache = [
            {**self._BASE, "opeid8": "00100200", "unitid": "111", "year": "2016", "is_active": "1", "status": "A"},
            {**self._BASE, "opeid8": "00100201", "unitid": "112", "year": "2017", "is_active": "3", "status": "D"},
        ]
        assert build_opeid6_last_year(cache) == {"001002": 2016}

    def test_combined_status_flag(self):
        from build_ipeds_crosswalk import build_combined_years
        cache = [
            {**self._BASE, "opeid8": "00100200", "unitid": "111", "year": "2017", "is_active": "1", "status": "A"},
            {**self._BASE, "opeid8": "00100200", "unitid": "111", "year": "2018", "is_active": "3", "status": "C"},
        ]
        enriched, _ = enrich_closure_records(
            [_fsa_rec("00100200", "2018-03-01")], *_make_indexes(cache), 2002,
            combined_years=build_combined_years(cache))
        assert enriched[0]["ipeds_combined_after_closure"] == "true"


def test_history_status_derivation():
    from build_ipeds_history_cache import _status
    assert _status("1", "-2", "-2") == "A"
    assert _status("3", "123456", "-2") == "C"
    assert _status("3", "-2", "06/2010") == "D"
