"""Tests for parse_federal_sources.py.

Unit tests use minimal synthetic data.
Integration tests read real rows from the downloaded source files
(skipped if the files are absent).
"""
from __future__ import annotations

import csv
import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import parse_federal_sources as pfs

PROJECT = Path(__file__).resolve().parents[1]
CLOSED_ZIP = (
    PROJECT
    / "data/raw/weekly_closed_school_search/2026-11-08"
    / "WEEKLY_CLOSED_REPORT_20261108070401179490.zip"
)
WSF_ZIP = (
    PROJECT
    / "data/raw/weekly_school_file/2026-11-05/access"
    / "schfile_access_format_20261105.dat.zip"
)
REAL_FILES = pytest.mark.skipif(
    not (CLOSED_ZIP.exists() and WSF_ZIP.exists()),
    reason="Real source files not present",
)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

class TestIsoFromMmddyyyy:
    def test_standard_date(self):
        assert pfs._iso_from_mmddyyyy("10/31/2026") == "2026-10-31"

    def test_single_digit_month_day(self):
        assert pfs._iso_from_mmddyyyy("1/5/2015") == "2015-01-05"

    def test_empty_returns_empty(self):
        assert pfs._iso_from_mmddyyyy("") == ""

    def test_invalid_format_returns_empty(self):
        assert pfs._iso_from_mmddyyyy("2026-10-31") == ""

    def test_invalid_date_returns_empty(self):
        assert pfs._iso_from_mmddyyyy("13/40/2026") == ""

    def test_leading_trailing_whitespace(self):
        assert pfs._iso_from_mmddyyyy("  06/01/2023  ") == "2023-06-01"


class TestIsoFromYyyymmdd:
    def test_standard_date(self):
        assert pfs._iso_from_yyyymmdd("20261031") == "2026-10-31"

    def test_empty_returns_empty(self):
        assert pfs._iso_from_yyyymmdd("") == ""

    def test_non_digit_returns_empty(self):
        assert pfs._iso_from_yyyymmdd("2026-10-31") == ""

    def test_wrong_length_returns_empty(self):
        assert pfs._iso_from_yyyymmdd("202610") == ""

    def test_invalid_date_returns_empty(self):
        assert pfs._iso_from_yyyymmdd("20261340") == ""

    def test_all_zeros_returns_empty(self):
        assert pfs._iso_from_yyyymmdd("00000000") == ""


# ---------------------------------------------------------------------------
# OPEID helpers
# ---------------------------------------------------------------------------

class TestSplitOpeid8:
    def test_8_digit_input(self):
        opeid8, opeid6, suffix = pfs._split_opeid8("04122100")
        assert opeid8 == "04122100"
        assert opeid6 == "041221"
        assert suffix == "00"

    def test_pads_short_input(self):
        opeid8, opeid6, suffix = pfs._split_opeid8("100200")
        assert opeid8 == "00100200"
        assert opeid6 == "001002"
        assert suffix == "00"

    def test_strips_non_digits(self):
        opeid8, opeid6, suffix = pfs._split_opeid8("04-1221-00")
        assert opeid8 == "04122100"

    def test_non_zero_suffix(self):
        _, _, suffix = pfs._split_opeid8("04122103")
        assert suffix == "03"

    def test_empty_pads_to_zeros(self):
        opeid8, opeid6, suffix = pfs._split_opeid8("")
        assert opeid8 == "00000000"
        assert opeid6 == "000000"
        assert suffix == "00"


# ---------------------------------------------------------------------------
# FWF helper
# ---------------------------------------------------------------------------

class TestFwf:
    def test_extracts_field_from_bytes(self):
        line = b"ABCDEFGHIJ"
        assert pfs._fwf(line, 1, 3) == "ABC"

    def test_strips_whitespace(self):
        line = b"  HELLO  XX"
        assert pfs._fwf(line, 1, 9) == "HELLO"

    def test_short_line_returns_empty(self):
        line = b"AB"
        assert pfs._fwf(line, 1, 10) == ""


# ---------------------------------------------------------------------------
# _is_suspicious_location
# ---------------------------------------------------------------------------

class TestIsSuspiciousLocation:
    def test_plain_college_name_not_suspicious(self):
        assert pfs._is_suspicious_location("Springfield Community College") is False

    def test_department_of_flagged(self):
        assert pfs._is_suspicious_location("Department of Nursing") is True

    def test_dept_abbreviation_flagged(self):
        assert pfs._is_suspicious_location("Dept. of Education Site") is True

    def test_school_of_flagged(self):
        assert pfs._is_suspicious_location("School of Business") is True

    def test_college_of_flagged(self):
        assert pfs._is_suspicious_location("College of Engineering") is True

    def test_teacher_education_flagged(self):
        assert pfs._is_suspicious_location("Teacher Education Center") is True

    def test_off_campus_flagged(self):
        assert pfs._is_suspicious_location("Off-Campus Learning Center") is True

    def test_dual_enrollment_flagged(self):
        assert pfs._is_suspicious_location("Dual Enrollment Site") is True

    def test_satellite_center_flagged(self):
        assert pfs._is_suspicious_location("Satellite Center East") is True

    def test_extension_site_flagged(self):
        assert pfs._is_suspicious_location("University Extension Site") is True

    def test_case_insensitive(self):
        assert pfs._is_suspicious_location("DEPARTMENT OF HEALTH") is True

    def test_empty_string_not_suspicious(self):
        assert pfs._is_suspicious_location("") is False

    def test_distance_learning_flagged(self):
        assert pfs._is_suspicious_location("Distance Learning Program") is True

    def test_continuing_education_flagged(self):
        assert pfs._is_suspicious_location("Continuing Education Division") is True

    def test_school_of_same_as_school_name_not_flagged(self):
        # "School of Visual Arts" is the institution's real name, not a sub-unit
        assert pfs._is_suspicious_location("School of Visual Arts", "School of Visual Arts") is False

    def test_school_of_different_from_school_name_flagged(self):
        # location_name is a sub-department of a differently-named parent
        assert pfs._is_suspicious_location("School of Business", "Metro University") is True

    def test_college_of_same_as_school_name_not_flagged(self):
        assert pfs._is_suspicious_location("College of the Ozarks", "College of the Ozarks") is False

    def test_case_insensitive_same_name_comparison(self):
        # Matching should be case-insensitive
        assert pfs._is_suspicious_location("School of Visual Arts", "SCHOOL OF VISUAL ARTS") is False


# ---------------------------------------------------------------------------
# Closed-school XLSX parser — synthetic
# ---------------------------------------------------------------------------

def _make_fsa_xlsx_zip(rows_after_summary: list[list[str]]) -> bytes:
    """Build a minimal FSA closed-school outer-ZIP with correct structure."""
    import xml.etree.ElementTree as ET
    NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

    # Pre-header summary section + column header + data + sentinel
    all_rows = [
        ["REPORT DATE", "2026-11-08"],
        ["DOMESTIC", "5"],
        ["FOREIGN", "1"],
        ["TOTAL", "6"],
        [],
        ["CLOSED DATE", "OPEID", "SCHOOL NAME", "LOCATION",
         "ADDRESS", "CITY", "STATE", "ZIP CODE", "COUNTRY (FOREIGN)"],
    ] + rows_after_summary + [
        ["CLOSED SCHOOLS DATA DISTRIBUTION", "", ""],
        ["2008", "5", ""],
    ]

    rows_xml = ""
    for r_idx, row in enumerate(all_rows, 1):
        cells_xml = ""
        for c_idx, val in enumerate(row):
            col_letter = chr(ord("A") + c_idx)
            cells_xml += (
                f'<c r="{col_letter}{r_idx}" t="inlineStr">'
                f"<is><t>{val}</t></is></c>"
            )
        rows_xml += f'<row r="{r_idx}">{cells_xml}</row>'

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{rows_xml}</sheetData>"
        "</worksheet>"
    ).encode("utf-8")

    xlsx_buf = io.BytesIO()
    with zipfile.ZipFile(xlsx_buf, "w") as inner:
        inner.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        inner.writestr("[Content_Types].xml", b"<Types/>")
    xlsx_bytes = xlsx_buf.getvalue()

    outer_buf = io.BytesIO()
    with zipfile.ZipFile(outer_buf, "w") as outer:
        outer.writestr("WKCL.test.xlsx", xlsx_bytes)
    return outer_buf.getvalue()


class TestParseClosedSchoolXlsx:
    def _make_zip(self, tmp_path, data_rows):
        z = _make_fsa_xlsx_zip(data_rows)
        p = tmp_path / "WEEKLY_CLOSED_REPORT_test.zip"
        p.write_bytes(z)
        return p

    def test_returns_correct_row_count(self, tmp_path):
        rows = [
            ["06/01/2023", "04122100", "Foo College", "Foo College",
             "1 Main St", "Springfield", "CA", "90000", ""],
            ["07/15/2022", "00500300", "Bar Institute", "Bar Institute",
             "2 Oak Ave", "Portland", "OR", "97000", ""],
        ]
        z = self._make_zip(tmp_path, rows)
        result = pfs.parse_closed_school_xlsx(z, "2026-11-08")
        assert len(result) == 2

    def test_opeid8_zero_padded(self, tmp_path):
        rows = [["06/01/2023", "1234500", "Foo", "Foo", "", "City", "CA", "", ""]]
        z = self._make_zip(tmp_path, rows)
        result = pfs.parse_closed_school_xlsx(z, "2026-11-08")
        assert result[0]["opeid8"] == "01234500"

    def test_opeid6_and_suffix_extracted(self, tmp_path):
        rows = [["06/01/2023", "04122103", "Foo", "Foo", "", "City", "CA", "", ""]]
        z = self._make_zip(tmp_path, rows)
        result = pfs.parse_closed_school_xlsx(z, "2026-11-08")
        assert result[0]["opeid6"] == "041221"
        assert result[0]["location_suffix"] == "03"

    def test_closed_date_iso_converted(self, tmp_path):
        rows = [["10/31/2026", "04122100", "Foo", "Foo", "", "City", "CA", "", ""]]
        z = self._make_zip(tmp_path, rows)
        result = pfs.parse_closed_school_xlsx(z, "2026-11-08")
        assert result[0]["closed_date_iso"] == "2026-10-31"

    def test_sentinel_row_excluded(self, tmp_path):
        rows = [["06/01/2023", "04122100", "Foo", "Foo", "", "City", "CA", "", ""]]
        z = self._make_zip(tmp_path, rows)
        result = pfs.parse_closed_school_xlsx(z, "2026-11-08")
        assert not any(r["school_name"] == "CLOSED SCHOOLS DATA DISTRIBUTION"
                       for r in result)

    def test_provenance_fields_set(self, tmp_path):
        rows = [["06/01/2023", "04122100", "Foo", "Foo", "", "City", "CA", "", ""]]
        z = self._make_zip(tmp_path, rows)
        result = pfs.parse_closed_school_xlsx(z, "2026-11-08")
        assert result[0]["source_date"] == "2026-11-08"
        assert "test" in result[0]["source_file"]

    def test_is_suspicious_location_field_present(self, tmp_path):
        rows = [["06/01/2023", "04122100", "Foo College", "Foo College",
                 "1 Main St", "Springfield", "CA", "90000", ""]]
        z = self._make_zip(tmp_path, rows)
        result = pfs.parse_closed_school_xlsx(z, "2026-11-08")
        assert "is_suspicious_location" in result[0]
        assert result[0]["is_suspicious_location"] == "false"

    def test_suspicious_location_name_flagged(self, tmp_path):
        rows = [["06/01/2023", "04122100", "Foo College", "Department of Nursing",
                 "1 Main St", "Springfield", "CA", "90000", ""]]
        z = self._make_zip(tmp_path, rows)
        result = pfs.parse_closed_school_xlsx(z, "2026-11-08")
        assert result[0]["is_suspicious_location"] == "true"

    def test_is_suspicious_location_in_closed_school_fields(self):
        assert "is_suspicious_location" in pfs.CLOSED_SCHOOL_FIELDS

    def test_no_header_raises_valueerror(self, tmp_path):
        xlsx_buf = io.BytesIO()
        with zipfile.ZipFile(xlsx_buf, "w") as inner:
            sheet = (
                b'<?xml version="1.0"?>'
                b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                b"<sheetData>"
                b'<row r="1"><c r="A1" t="inlineStr"><is><t>no header here</t></is></c></row>'
                b"</sheetData></worksheet>"
            )
            inner.writestr("xl/worksheets/sheet1.xml", sheet)
            inner.writestr("[Content_Types].xml", b"<Types/>")
        outer_buf = io.BytesIO()
        with zipfile.ZipFile(outer_buf, "w") as outer:
            outer.writestr("test.xlsx", xlsx_buf.getvalue())
        p = tmp_path / "bad.zip"
        p.write_bytes(outer_buf.getvalue())
        with pytest.raises(ValueError, match="header row not found"):
            pfs.parse_closed_school_xlsx(p, "2026-11-08")


# ---------------------------------------------------------------------------
# WSF fixed-width parsers — synthetic
# ---------------------------------------------------------------------------

def _make_wsf_zip(tmp_path: Path, member_name: str, lines: list[bytes]) -> Path:
    """Build a minimal WSF access-format ZIP with one member."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        content = b"\n".join(lines) + b"\n"
        zf.writestr(member_name, content)
    p = tmp_path / "schfile_access_format_test.dat.zip"
    p.write_bytes(buf.getvalue())
    return p


def _make_rt01_line(opeid8: str = "04122100", school_name: str = "Foo College",
                    close_date: str = "") -> bytes:
    """Build a minimal 550-byte rectype_01 line with key fields set."""
    # Field positions (1-indexed, inclusive):
    # opeid8:       3-10   (8 chars)
    # school_name: 12-81  (70 chars)
    # close_date:  382-389 (8 chars)
    line = bytearray(b" " * 550)
    line[0:2] = b"01"
    line[2:10] = opeid8.encode("latin-1").ljust(8)[:8]
    line[11:81] = school_name.encode("latin-1").ljust(70)[:70]
    if close_date:
        line[381:389] = close_date.encode("latin-1").ljust(8)[:8]
    return bytes(line)


def _make_rt11_line(opeid8: str = "04122100",
                    closure_dt: str = "20230601",
                    record_holder: str = "School") -> bytes:
    """Build a minimal 550-byte rectype_11 line with key fields set."""
    # Field positions (1-indexed, inclusive):
    # opeid8:               3-10
    # closure_dt_current:  12-19
    # record_holder_desc:  51-100
    line = bytearray(b" " * 550)
    line[0:2] = b"11"
    line[2:10] = opeid8.encode("latin-1").ljust(8)[:8]
    line[11:19] = closure_dt.encode("latin-1").ljust(8)[:8]
    line[50:100] = record_holder.encode("latin-1").ljust(50)[:50]
    return bytes(line)


class TestParseWsfRectype01:
    def test_returns_correct_row_count(self, tmp_path):
        lines = [_make_rt01_line(f"0412210{i}") for i in range(3)]
        z = _make_wsf_zip(tmp_path, "schfile_extract_rectype_01_test.dat", lines)
        result = pfs.parse_wsf_rectype01(z, "2026-11-05")
        assert len(result) == 3

    def test_opeid8_extracted(self, tmp_path):
        lines = [_make_rt01_line("04122100")]
        z = _make_wsf_zip(tmp_path, "schfile_extract_rectype_01_test.dat", lines)
        result = pfs.parse_wsf_rectype01(z, "2026-11-05")
        assert result[0]["opeid8"] == "04122100"

    def test_opeid6_and_suffix(self, tmp_path):
        lines = [_make_rt01_line("04122103")]
        z = _make_wsf_zip(tmp_path, "schfile_extract_rectype_01_test.dat", lines)
        result = pfs.parse_wsf_rectype01(z, "2026-11-05")
        assert result[0]["opeid6"] == "041221"
        assert result[0]["location_suffix"] == "03"

    def test_close_date_iso_converted(self, tmp_path):
        lines = [_make_rt01_line(close_date="20231031")]
        z = _make_wsf_zip(tmp_path, "schfile_extract_rectype_01_test.dat", lines)
        result = pfs.parse_wsf_rectype01(z, "2026-11-05")
        assert result[0]["close_date"] == "20231031"
        assert result[0]["close_date_iso"] == "2023-10-31"

    def test_blank_close_date_gives_empty_iso(self, tmp_path):
        lines = [_make_rt01_line(close_date="")]
        z = _make_wsf_zip(tmp_path, "schfile_extract_rectype_01_test.dat", lines)
        result = pfs.parse_wsf_rectype01(z, "2026-11-05")
        assert result[0]["close_date_iso"] == ""

    def test_provenance_fields(self, tmp_path):
        lines = [_make_rt01_line()]
        z = _make_wsf_zip(tmp_path, "schfile_extract_rectype_01_test.dat", lines)
        result = pfs.parse_wsf_rectype01(z, "2026-11-05")
        assert result[0]["source_date"] == "2026-11-05"

    def test_no_rectype01_member_returns_empty(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("schfile_extract_rectype_02_test.dat", b"irrelevant\n")
        p = tmp_path / "other.zip"
        p.write_bytes(buf.getvalue())
        result = pfs.parse_wsf_rectype01(p, "2026-11-05")
        assert result == []


class TestParseWsfRectype11:
    def test_returns_correct_row_count(self, tmp_path):
        lines = [_make_rt11_line(f"0412210{i}") for i in range(5)]
        z = _make_wsf_zip(tmp_path, "schfile_extract_rectype_11_test.dat", lines)
        result = pfs.parse_wsf_rectype11(z, "2026-11-05")
        assert len(result) == 5

    def test_closure_dt_current_iso(self, tmp_path):
        lines = [_make_rt11_line(closure_dt="20230601")]
        z = _make_wsf_zip(tmp_path, "schfile_extract_rectype_11_test.dat", lines)
        result = pfs.parse_wsf_rectype11(z, "2026-11-05")
        assert result[0]["closure_dt_current_iso"] == "2023-06-01"

    def test_record_holder_desc_extracted(self, tmp_path):
        lines = [_make_rt11_line(record_holder="State Licensing Agency")]
        z = _make_wsf_zip(tmp_path, "schfile_extract_rectype_11_test.dat", lines)
        result = pfs.parse_wsf_rectype11(z, "2026-11-05")
        assert result[0]["record_holder_desc"] == "State Licensing Agency"

    def test_all_fields_present(self, tmp_path):
        lines = [_make_rt11_line()]
        z = _make_wsf_zip(tmp_path, "schfile_extract_rectype_11_test.dat", lines)
        result = pfs.parse_wsf_rectype11(z, "2026-11-05")
        for field in pfs.WSF_RT11_FIELDS:
            assert field in result[0], f"Missing field: {field}"


# ---------------------------------------------------------------------------
# Integration tests against real files
# ---------------------------------------------------------------------------

class TestRealClosedXlsx:
    @REAL_FILES
    def test_produces_23105_records(self):
        result = pfs.parse_closed_school_xlsx(CLOSED_ZIP, "2026-11-08")
        assert len(result) == 23105

    @REAL_FILES
    def test_all_dates_iso_format(self):
        result = pfs.parse_closed_school_xlsx(CLOSED_ZIP, "2026-11-08")
        bad = [r for r in result if r["closed_date"] and not r["closed_date_iso"]]
        assert bad == [], f"Non-convertible dates: {bad[:3]}"

    @REAL_FILES
    def test_all_opeid8_eight_digits(self):
        result = pfs.parse_closed_school_xlsx(CLOSED_ZIP, "2026-11-08")
        bad = [r for r in result if len(r["opeid8"]) != 8 or not r["opeid8"].isdigit()]
        assert bad == [], f"Bad OPEID8s: {bad[:3]}"

    @REAL_FILES
    def test_opeid6_is_first_six_of_opeid8(self):
        result = pfs.parse_closed_school_xlsx(CLOSED_ZIP, "2026-11-08")
        mismatched = [r for r in result[:1000] if r["opeid6"] != r["opeid8"][:6]]
        assert mismatched == []

    @REAL_FILES
    def test_no_sentinel_row_in_output(self):
        result = pfs.parse_closed_school_xlsx(CLOSED_ZIP, "2026-11-08")
        assert not any("distribution" in r["school_name"].lower() for r in result)


class TestRealWsfRectype01:
    @REAL_FILES
    def test_record_count_matches_profile(self):
        result = pfs.parse_wsf_rectype01(WSF_ZIP, "2026-11-05")
        assert len(result) == 58877

    @REAL_FILES
    def test_known_first_opeid(self):
        result = pfs.parse_wsf_rectype01(WSF_ZIP, "2026-11-05")
        assert result[0]["opeid8"] == "00100100"

    @REAL_FILES
    def test_close_date_iso_valid_when_set(self):
        result = pfs.parse_wsf_rectype01(WSF_ZIP, "2026-11-05")
        bad = [r for r in result
               if r["close_date"] and r["close_date"] != "00000000"
               and not r["close_date_iso"]]
        assert bad == [], f"Non-convertible close dates: {bad[:3]}"

    @REAL_FILES
    def test_opeid6_always_six_digits(self):
        result = pfs.parse_wsf_rectype01(WSF_ZIP, "2026-11-05")
        bad = [r for r in result[:5000] if len(r["opeid6"]) != 6]
        assert bad == []


class TestRealWsfRectype11:
    @REAL_FILES
    def test_record_count_matches_profile(self):
        result = pfs.parse_wsf_rectype11(WSF_ZIP, "2026-11-05")
        assert len(result) == 23212

    @REAL_FILES
    def test_closure_dt_current_iso_valid(self):
        result = pfs.parse_wsf_rectype11(WSF_ZIP, "2026-11-05")
        # ~99.5% fill rate confirmed in Phase 4; check those that are set convert OK
        bad = [r for r in result
               if r["closure_dt_current"] and r["closure_dt_current"] != "00000000"
               and not r["closure_dt_current_iso"]]
        assert bad == [], f"Non-convertible closure dates: {bad[:3]}"


# ---------------------------------------------------------------------------
# RT06 (Change of Affiliation) — synthetic
# ---------------------------------------------------------------------------

def _make_rt06_line(
    prev_opeid8: str = "04122100",
    new_opeid8: str = "04122101",
    coa_actn_cd: str = "4",
    effective_date: str = "20231031",
    secondary_cd: str = "",
    indicator: str = "N",
) -> bytes:
    """Build a minimal 550-byte rectype_06 line.

    Positions (1-indexed, inclusive):
      1-2:   record type "06"
      3-10:  prev_opeid8
      11:    indicator
      12-19: new_opeid8
      20:    coa_actn_cd
      21-28: effective_date
      29:    secondary_cd
    """
    line = bytearray(b" " * 550)
    line[0:2] = b"06"
    line[2:10] = prev_opeid8.encode("latin-1").ljust(8)[:8]
    line[10:11] = indicator.encode("latin-1").ljust(1)[:1]
    line[11:19] = new_opeid8.encode("latin-1").ljust(8)[:8]
    line[19:20] = coa_actn_cd.encode("latin-1").ljust(1)[:1]
    line[20:28] = effective_date.encode("latin-1").ljust(8)[:8]
    if secondary_cd:
        line[28:29] = secondary_cd.encode("latin-1").ljust(1)[:1]
    return bytes(line)


class TestParseWsfRectype06:
    def _make_zip(self, tmp_path, lines):
        return _make_wsf_zip(tmp_path, "schfile_extract_rectype_06_test.dat", lines)

    def test_returns_correct_row_count(self, tmp_path):
        lines = [_make_rt06_line(f"0412210{i}", f"0412210{i+1}") for i in range(4)]
        z = self._make_zip(tmp_path, lines)
        result = pfs.parse_wsf_rectype06(z, "2026-11-05")
        assert len(result) == 4

    def test_prev_opeid8_extracted(self, tmp_path):
        z = self._make_zip(tmp_path, [_make_rt06_line("04122100", "04122101")])
        result = pfs.parse_wsf_rectype06(z, "2026-11-05")
        assert result[0]["prev_opeid8"] == "04122100"

    def test_new_opeid8_extracted(self, tmp_path):
        z = self._make_zip(tmp_path, [_make_rt06_line("04122100", "09999900")])
        result = pfs.parse_wsf_rectype06(z, "2026-11-05")
        assert result[0]["new_opeid8"] == "09999900"

    def test_opeid6_and_suffix_split(self, tmp_path):
        z = self._make_zip(tmp_path, [_make_rt06_line("04122103", "04122104")])
        result = pfs.parse_wsf_rectype06(z, "2026-11-05")
        assert result[0]["prev_opeid6"] == "041221"
        assert result[0]["prev_location_suffix"] == "03"
        assert result[0]["new_opeid6"] == "041221"
        assert result[0]["new_location_suffix"] == "04"

    def test_coa_actn_cd_extracted(self, tmp_path):
        z = self._make_zip(tmp_path, [_make_rt06_line(coa_actn_cd="7")])
        result = pfs.parse_wsf_rectype06(z, "2026-11-05")
        assert result[0]["coa_actn_cd"] == "7"

    def test_effective_date_iso_converted(self, tmp_path):
        z = self._make_zip(tmp_path, [_make_rt06_line(effective_date="20231031")])
        result = pfs.parse_wsf_rectype06(z, "2026-11-05")
        assert result[0]["effective_date"] == "20231031"
        assert result[0]["effective_date_iso"] == "2023-10-31"

    def test_sentinel_date_gives_empty_iso(self, tmp_path):
        z = self._make_zip(tmp_path, [_make_rt06_line(effective_date="17000101")])
        result = pfs.parse_wsf_rectype06(z, "2026-11-05")
        assert result[0]["effective_date"] == "17000101"
        assert result[0]["effective_date_iso"] == ""

    def test_secondary_cd_extracted(self, tmp_path):
        z = self._make_zip(tmp_path, [_make_rt06_line(secondary_cd="D")])
        result = pfs.parse_wsf_rectype06(z, "2026-11-05")
        assert result[0]["secondary_cd"] == "D"

    def test_opeid6_changed_true_when_opeid6_differs(self, tmp_path):
        z = self._make_zip(tmp_path, [_make_rt06_line("04122100", "09999900")])
        result = pfs.parse_wsf_rectype06(z, "2026-11-05")
        assert result[0]["opeid6_changed"] == "true"

    def test_opeid6_changed_false_when_suffix_only(self, tmp_path):
        z = self._make_zip(tmp_path, [_make_rt06_line("04122100", "04122101")])
        result = pfs.parse_wsf_rectype06(z, "2026-11-05")
        assert result[0]["opeid6_changed"] == "false"

    def test_all_fields_present(self, tmp_path):
        z = self._make_zip(tmp_path, [_make_rt06_line()])
        result = pfs.parse_wsf_rectype06(z, "2026-11-05")
        for field in pfs.WSF_RT06_FIELDS:
            assert field in result[0], f"Missing field: {field}"

    def test_no_rectype06_member_returns_empty(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("schfile_extract_rectype_01_test.dat", b"irrelevant\n")
        p = tmp_path / "other.zip"
        p.write_bytes(buf.getvalue())
        result = pfs.parse_wsf_rectype06(p, "2026-11-05")
        assert result == []

    def test_provenance_fields(self, tmp_path):
        z = self._make_zip(tmp_path, [_make_rt06_line()])
        result = pfs.parse_wsf_rectype06(z, "2026-11-05")
        assert result[0]["source_date"] == "2026-11-05"
        assert "rectype_06" in result[0]["member"]


class TestRealWsfRectype06:
    @REAL_FILES
    def test_record_count(self):
        result = pfs.parse_wsf_rectype06(WSF_ZIP, "2026-11-05")
        assert len(result) == 3106

    @REAL_FILES
    def test_all_records_550_bytes_parsed(self):
        # Field layout confirmed at 550 bytes wide; prev_opeid8 must always be 8 digits
        result = pfs.parse_wsf_rectype06(WSF_ZIP, "2026-11-05")
        bad = [r for r in result if len(r["prev_opeid8"]) != 8]
        assert bad == [], f"Unexpected OPEID lengths: {bad[:3]}"

    @REAL_FILES
    def test_opeid6_changed_flag_distribution(self):
        result = pfs.parse_wsf_rectype06(WSF_ZIP, "2026-11-05")
        changed = sum(1 for r in result if r["opeid6_changed"] == "true")
        same = sum(1 for r in result if r["opeid6_changed"] == "false")
        assert changed == 2624
        assert same == 482

    @REAL_FILES
    def test_sentinel_date_not_converted(self):
        result = pfs.parse_wsf_rectype06(WSF_ZIP, "2026-11-05")
        sentinel = [r for r in result if r["effective_date"] == "17000101"]
        assert all(r["effective_date_iso"] == "" for r in sentinel)
        assert len(sentinel) == 246

    @REAL_FILES
    def test_coa_actn_cd_4_mostly_suffix_only(self):
        # Action code '4' is a suffix/location-number change.  Empirically 481/482 records
        # are suffix-only; one record (110727→010727) is a leading-digit renumber that
        # triggers opeid6_changed=true while still being a renumber, not a merger.
        result = pfs.parse_wsf_rectype06(WSF_ZIP, "2026-11-05")
        cd4 = [r for r in result if r["coa_actn_cd"] == "4"]
        cd4_opeid6_changed = [r for r in cd4 if r["opeid6_changed"] == "true"]
        # At most 1 exception allowed (the 110727→010727 renumber)
        assert len(cd4_opeid6_changed) <= 1, (
            f"Unexpected coa_actn_cd=4 with OPEID6 change: {cd4_opeid6_changed[:5]}"
        )
        # Overwhelming majority must be suffix-only (>= 99% of cd4 records)
        pct_suffix_only = (len(cd4) - len(cd4_opeid6_changed)) / len(cd4) if cd4 else 0
        assert pct_suffix_only >= 0.99
