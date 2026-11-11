"""Narrow regression tests for profile_sources.py.

Layer 1 — unit tests using synthetic data (always run, no raw files needed).
Layer 2 — integration smoke tests against the real federal source files
           (automatically skipped when the files are absent, so CI without
           the raw data never breaks).

Run from the project root:
    python -m pytest tests/test_profile_sources.py -v
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

# Add scripts/ to sys.path.
SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from profile_sources import (
    HISTORICAL_POSITIONS,
    RECTYPE01_POSITIONS,
    RECTYPE11_POSITIONS,
    _detect_date_format,
    _fwf,
    _xlsx_declared_dim_from_path,
    profile_closed_xlsx,
    profile_wsf_access,
    stream_xlsx_rows,
)

PROJECT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Real-file paths (integration tests skip if absent).
# ---------------------------------------------------------------------------

WSF_ACCESS_ZIP = (
    PROJECT
    / "data/raw/weekly_school_file/2026-11-05/access/schfile_access_format_20261105.dat.zip"
)
CLOSED_XLSX_ZIP = (
    PROJECT
    / "data/raw/weekly_closed_school_search/2026-11-08"
    / "WEEKLY_CLOSED_REPORT_20261108070401179490.zip"
)

needs_wsf = pytest.mark.skipif(
    not WSF_ACCESS_ZIP.exists(),
    reason="WSF access ZIP not present",
)
needs_closed = pytest.mark.skipif(
    not CLOSED_XLSX_ZIP.exists(),
    reason="Closed-school XLSX ZIP not present",
)


# ---------------------------------------------------------------------------
# XLSX helpers — build a minimal XLSX with a deliberately wrong dimension.
# ---------------------------------------------------------------------------

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _make_xlsx(rows: list[list[str]], declared_ref: str = "A1:A1") -> Path:
    """Write a minimal XLSX to tmp_path with the given declared dimension."""
    # Shared strings
    flat = [v for row in rows for v in row]
    unique = list(dict.fromkeys(flat))
    si = {s: i for i, s in enumerate(unique)}

    sheet_rows = []
    for r_idx, row in enumerate(rows, 1):
        cells = []
        for c_idx, val in enumerate(row, 1):
            col = chr(ord("A") + c_idx - 1)
            cells.append(f'<c r="{col}{r_idx}" t="s"><v>{si[val]}</v></c>')
        sheet_rows.append(f'<row r="{r_idx}">{"".join(cells)}</row>')

    sheet_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_NS}">'
        f'<dimension ref="{declared_ref}"/>'
        f'<sheetData>{"".join(sheet_rows)}</sheetData>'
        f"</worksheet>"
    )

    si_els = "".join(f"<si><t>{s}</t></si>" for s in unique)
    sst_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{_NS}" count="{len(unique)}" uniqueCount="{len(unique)}">'
        f"{si_els}</sst>"
    )

    ct_xml = (
        '<?xml version="1.0"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Override PartName="/xl/worksheets/sheet1.xml"'
        ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zf.writestr("xl/sharedStrings.xml", sst_xml)
        zf.writestr("[Content_Types].xml", ct_xml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Unit tests: stream_xlsx_rows bypasses declared dimension
# ---------------------------------------------------------------------------

class TestStreamXlsx:
    def test_reads_all_rows_despite_small_declared_dimension(self, tmp_path):
        rows = [
            ["Name", "OPEID", "Close Date"],
            ["School A", "00123400", "11/01/2024"],
            ["School B", "00567800", "03/15/2023"],
            ["School C", "00999900", ""],
        ]
        # Declare only A1:A1 — a correct parser must ignore this.
        xlsx_bytes = _make_xlsx(rows, declared_ref="A1:A1")
        xlsx_path = tmp_path / "test.xlsx.zip"
        xlsx_path.write_bytes(xlsx_bytes)

        result = list(stream_xlsx_rows(xlsx_path))
        assert len(result) == 4, f"Expected 4 rows, got {len(result)}"
        assert result[0][0] == "Name"
        assert result[1][1] == "00123400"
        assert result[3][1] == "00999900"

    def test_declared_dimension_is_captured(self, tmp_path):
        xlsx_bytes = _make_xlsx([["h1", "h2"]], declared_ref="A1:I40")
        xlsx_path = tmp_path / "test.xlsx.zip"
        xlsx_path.write_bytes(xlsx_bytes)
        dim = _xlsx_declared_dim_from_path(xlsx_path)
        assert dim == "A1:I40"

    def test_empty_sheet_yields_no_rows(self, tmp_path):
        buf = io.BytesIO()
        sheet_xml = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{_NS}"><sheetData/></worksheet>'
        )
        ct_xml = (
            '<?xml version="1.0"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/xl/worksheets/sheet1.xml"'
            ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>"
        )
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
            zf.writestr("[Content_Types].xml", ct_xml)
        path = tmp_path / "empty.xlsx.zip"
        path.write_bytes(buf.getvalue())
        assert list(stream_xlsx_rows(path)) == []


# ---------------------------------------------------------------------------
# Unit tests: profile_closed_xlsx
# ---------------------------------------------------------------------------

class TestProfileClosedXlsx:
    def _make_closed_zip(self, data_rows: list[list[str]], tmp_path: Path) -> Path:
        header = ["School Name", "OPE ID", "Close Date", "Country"]
        rows = [header] + data_rows
        # Declare only 2 rows to simulate the real dimension bug.
        xlsx_bytes = _make_xlsx(rows, declared_ref="A1:D2")
        zip_path = tmp_path / "WKCLOSED.zip"
        inner_buf = io.BytesIO()
        with zipfile.ZipFile(inner_buf, "w") as zf:
            # The closed-school file ships as a ZIP containing the XLSX.
            # profile_closed_xlsx takes the outer ZIP path, calls stream_xlsx_rows on it
            # directly (the XLSX IS the outer zip in our structure).
            # For test purposes we pass the XLSX bytes directly as the ZIP.
            pass
        # Actually profile_closed_xlsx calls stream_xlsx_rows(zip_path) treating
        # the zip_path AS the XLSX/ZIP. Write xlsx_bytes as the zip_path.
        zip_path.write_bytes(xlsx_bytes)
        return zip_path

    def test_data_row_count_correct(self, tmp_path):
        data_rows = [
            ["Alpha College", "00111100", "11/01/2024", ""],
            ["Beta U", "00222200", "03/15/2023", ""],
            ["Gamma Intl", "00333300", "05/20/2022", "Canada"],
        ]
        path = self._make_closed_zip(data_rows, tmp_path)
        profile = profile_closed_xlsx(path, "2026-11-08")
        assert profile["data_row_count"] == 3

    def test_date_format_mm_dd_yyyy_detected(self, tmp_path):
        data_rows = [
            ["School X", "00111100", "11/01/2024", ""],
            ["School Y", "00222200", "03/15/2023", ""],
        ]
        path = self._make_closed_zip(data_rows, tmp_path)
        profile = profile_closed_xlsx(path, "2026-11-08")
        assert "MM/DD/YYYY" in profile["date_formats_seen"]

    def test_domestic_vs_foreign_count(self, tmp_path):
        data_rows = [
            ["Domestic A", "00111100", "11/01/2024", ""],
            ["Domestic B", "00222200", "03/15/2023", "US"],
            ["Foreign C", "00333300", "05/20/2022", "Canada"],
        ]
        path = self._make_closed_zip(data_rows, tmp_path)
        profile = profile_closed_xlsx(path, "2026-11-08")
        assert profile["domestic_count"] == 2
        assert profile["foreign_count"] == 1

    def test_header_row_captured(self, tmp_path):
        data_rows = [["Test School", "00111100", "11/01/2024", ""]]
        path = self._make_closed_zip(data_rows, tmp_path)
        profile = profile_closed_xlsx(path, "2026-11-08")
        # Columns are lowercased in the col-index map; header_row is raw.
        assert any("ope" in h.lower() or "close" in h.lower() for h in profile["header_row"])


# ---------------------------------------------------------------------------
# Boundary regression: exact Nov 8 2026 workbook section structure
# ---------------------------------------------------------------------------

def _make_production_shaped_zip(tmp_path: Path) -> Path:
    """Build an XLSX (wrapped in an outer ZIP) that reproduces the real FSA workbook
    section structure: summary labels, column header, closure records, then the
    CLOSED SCHOOLS DATA DISTRIBUTION section with annual summary rows."""
    rows = [
        # Pre-header rows that mirror the real file layout.
        [""],                                      # 0  blank
        ["REPORT DATE", "2026-11-08"],             # 1  report date — must NOT become the total
        [""],                                      # 2  blank
        ["DOMESTIC", "23804.0"],                   # 3
        ["FOREIGN", "583.0"],                      # 4
        ["TOTAL", "24387.0"],                      # 5
        [""],                                      # 6  blank
        # Column header row (must contain both CLOSED DATE and OPEID).
        ["CLOSED DATE", "OPEID", "SCHOOL NAME", "LOCATION",
         "ADDRESS", "CITY", "STATE", "ZIP CODE", "COUNTRY (FOREIGN)"],  # 7
        # Closure records — only these should be counted.
        ["10/31/2026", "01017607", "Westmoreland College", "Main", "140 N St", "Uniontown", "PA", "15401", ""],
        ["10/20/2026", "04163200", "Tenaj Salon Institute", "", "11915 CR 103", "The Villages", "FL", "32162", ""],
        ["12/18/1983", "00118502", "College of the Redwoods - DN", "", "883 W Washington", "Crescent City", "CA", "95531", ""],
        # End sentinel — data rows after this must be excluded.
        ["CLOSED SCHOOLS DATA DISTRIBUTION"],      # 11
        ["YEAR", "Domestic", "Foreign", "Total"],  # 12  distribution header
        ["2026.0", "175.0", "0.0", "175.0"],       # 13  annual summary row
        ["2025.0", "210.0", "3.0", "213.0"],       # 14  annual summary row
    ]
    xlsx_bytes = _make_xlsx(rows, declared_ref="A1:I40")
    outer_buf = io.BytesIO()
    with zipfile.ZipFile(outer_buf, "w") as outer_zf:
        outer_zf.writestr("WKCLOSED_BOUNDARY_TEST.xlsx", xlsx_bytes)
    path = tmp_path / "WKCLOSED_BOUNDARY_TEST.zip"
    path.write_bytes(outer_buf.getvalue())
    return path


class TestProfileClosedXlsxBoundary:
    """Regression tests for the Nov 8 2026 workbook boundary."""

    def test_total_comes_from_labeled_total_row_not_report_date(self, tmp_path):
        profile = profile_closed_xlsx(_make_production_shaped_zip(tmp_path), "2026-11-08")
        assert profile["header_reported_total"] == 24387, (
            f"Expected 24387 from TOTAL row, got {profile['header_reported_total']}. "
            "Parser must not extract 2026 from the REPORT DATE cell."
        )

    def test_only_closure_records_counted_not_annual_summaries(self, tmp_path):
        profile = profile_closed_xlsx(_make_production_shaped_zip(tmp_path), "2026-11-08")
        assert profile["data_row_count"] == 3, (
            f"Expected 3 closure records, got {profile['data_row_count']}. "
            "CLOSED SCHOOLS DATA DISTRIBUTION rows must be excluded."
        )

    def test_annual_summary_rows_do_not_produce_other_date_formats(self, tmp_path):
        profile = profile_closed_xlsx(_make_production_shaped_zip(tmp_path), "2026-11-08")
        assert profile["date_formats_seen"].get("other", 0) == 0, (
            f"Annual summary rows ('2026.0') leaked into date_formats_seen: "
            f"{profile['date_formats_seen']}"
        )

    def test_all_closure_dates_classify_as_mm_dd_yyyy(self, tmp_path):
        profile = profile_closed_xlsx(_make_production_shaped_zip(tmp_path), "2026-11-08")
        assert profile["date_formats_seen"] == {"MM/DD/YYYY": 3}, (
            f"Expected exactly 3 MM/DD/YYYY dates, got {profile['date_formats_seen']}"
        )

    def test_gap_uses_bounded_closure_count(self, tmp_path):
        profile = profile_closed_xlsx(_make_production_shaped_zip(tmp_path), "2026-11-08")
        # 24387 - 3 closure records = 24384
        assert profile["header_vs_data_gap"] == 24384, (
            f"Expected gap 24384, got {profile['header_vs_data_gap']}. "
            "Gap must be computed against the bounded record count."
        )

    def test_excluded_tail_rows_reported(self, tmp_path):
        profile = profile_closed_xlsx(_make_production_shaped_zip(tmp_path), "2026-11-08")
        # Sentinel (1) + YEAR header (1) + 2 annual rows = 4
        assert profile["excluded_tail_rows"] == 4


# ---------------------------------------------------------------------------
# Unit tests: fixed-width helpers
# ---------------------------------------------------------------------------

class TestFwf:
    def test_extracts_exact_field(self):
        line = " " * 11 + "SOME SCHOOL NAME         " + " " * 500
        # Field school_name: positions 12–81 → 0-indexed 11:81
        result = _fwf(line, 12, 81)
        assert result == "SOME SCHOOL NAME"

    def test_strips_whitespace(self):
        line = "  12345678" + " " * 540
        # opeid8: positions 3–10 → 0-indexed 2:10
        result = _fwf(line, 3, 10)
        assert result == "12345678"

    def test_short_line_returns_empty(self):
        # Line shorter than the field end → slice returns empty after strip
        line = "AB"
        result = _fwf(line, 10, 20)
        assert result == ""


class TestDetectDateFormat:
    def test_yyyymmdd(self):
        assert _detect_date_format(["20241101", "20230315"]) == "YYYYMMDD"

    def test_mm_dd_yyyy(self):
        assert _detect_date_format(["11/01/2024", "3/15/2023"]) == "MM/DD/YYYY"

    def test_iso(self):
        assert _detect_date_format(["2024-11-01", "2023-03-15"]) == "YYYY-MM-DD"

    def test_empty_values_return_unknown(self):
        assert _detect_date_format(["", "   ", ""]) == "unknown or empty"

    def test_mixed_first_nonblank_wins(self):
        # Function returns the format of the first non-blank value.
        result = _detect_date_format(["", "20241101", "11/01/2024"])
        assert result == "YYYYMMDD"


# ---------------------------------------------------------------------------
# Unit tests: profile_wsf_access with synthetic fixed-width data
# ---------------------------------------------------------------------------

def _make_wsf_zip(records: list[bytes]) -> bytes:
    """Pack records into a ZIP member named 'rectype_01.dat'."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        member = b"\n".join(records)
        zf.writestr("rectype_01.dat", member)
    return buf.getvalue()


def _make_record(
    opeid: str = "00000000",
    school_name: str = "Test School",
    state: str = "MA",
    close_date: str = "        ",
    record_len: int = 550,
) -> bytes:
    """Build a synthetic 550-byte latin-1 fixed-width record."""
    # Fields use 1-based inclusive positions from HISTORICAL_POSITIONS.
    buf = bytearray(b" " * record_len)
    # opeid8: 3–10 (0-indexed: 2:10)
    for i, ch in enumerate(opeid[:8]):
        buf[2 + i] = ord(ch)
    # school_name: 12–81 (0-indexed: 11:81)
    for i, ch in enumerate(school_name[:70]):
        buf[11 + i] = ord(ch)
    # state: 247–248 (0-indexed: 246:248)
    for i, ch in enumerate(state[:2]):
        buf[246 + i] = ord(ch)
    # close_date: 382–389 (0-indexed: 381:389)
    for i, ch in enumerate(close_date[:8]):
        buf[381 + i] = ord(ch)
    return bytes(buf)


class TestProfileWsfAccess:
    def test_record_count(self, tmp_path):
        records = [_make_record(opeid="1234567" + str(i)) for i in range(5)]
        zip_bytes = _make_wsf_zip(records)
        zip_path = tmp_path / "wsf.zip"
        zip_path.write_bytes(zip_bytes)
        profile = profile_wsf_access(zip_path, "2026-11-05", sample_limit=100)
        assert profile["total_records_all_types"] == 5

    def test_opeid_extracted_correctly(self, tmp_path):
        rec = _make_record(opeid="12345600")
        zip_path = tmp_path / "wsf.zip"
        zip_path.write_bytes(_make_wsf_zip([rec]))
        profile = profile_wsf_access(zip_path, "2026-11-05", sample_limit=100)
        samples = profile["field_samples_first_5"]["opeid8"]
        assert "12345600" in samples

    def test_close_date_yyyymmdd_format_detected(self, tmp_path):
        records = [_make_record(close_date="20241101"), _make_record(close_date="20230315")]
        zip_path = tmp_path / "wsf.zip"
        zip_path.write_bytes(_make_wsf_zip(records))
        profile = profile_wsf_access(zip_path, "2026-11-05", sample_limit=100)
        assert profile["close_date_format"] == "YYYYMMDD"

    def test_empty_close_dates_reported_as_unknown(self, tmp_path):
        records = [_make_record(close_date="        ") for _ in range(3)]
        zip_path = tmp_path / "wsf.zip"
        zip_path.write_bytes(_make_wsf_zip(records))
        profile = profile_wsf_access(zip_path, "2026-11-05", sample_limit=100)
        assert "unknown" in profile["close_date_format"]

    def test_historical_positions_present_in_profile(self, tmp_path):
        zip_path = tmp_path / "wsf.zip"
        zip_path.write_bytes(_make_wsf_zip([_make_record()]))
        profile = profile_wsf_access(zip_path, "2026-11-05", sample_limit=100)
        for field in HISTORICAL_POSITIONS:
            assert field in profile["historical_positions"]


# ---------------------------------------------------------------------------
# Integration tests — real files (skipped if absent)
# ---------------------------------------------------------------------------

class TestRealClosedXlsx:
    @needs_closed
    def test_actual_row_count_exceeds_declared_dimension(self):
        profile = profile_closed_xlsx(CLOSED_XLSX_ZIP, "2026-11-08")
        dim = profile["declared_dimension"]
        data_count = profile["data_row_count"]
        # Declared dimension must be A1:I40 (the known bug).
        assert dim == "A1:I40", f"Declared dimension changed: {dim}"
        # Actual rows must be far more than the 39 data rows the dimension implies.
        assert data_count > 1000, f"Expected 1000+ data rows, got {data_count}"

    @needs_closed
    def test_header_reported_total_present(self):
        profile = profile_closed_xlsx(CLOSED_XLSX_ZIP, "2026-11-08")
        total = profile["header_reported_total"]
        assert total is not None
        assert total > 20_000, f"Header total looks wrong: {total}"

    @needs_closed
    def test_date_format_is_mm_dd_yyyy(self):
        profile = profile_closed_xlsx(CLOSED_XLSX_ZIP, "2026-11-08")
        assert "MM/DD/YYYY" in profile["date_formats_seen"], (
            f"Expected MM/DD/YYYY date format; got {profile['date_formats_seen']}"
        )

    @needs_closed
    def test_no_iso_dates(self):
        # The April 2026 file used YYYY-MM-DD; the November file must not.
        profile = profile_closed_xlsx(CLOSED_XLSX_ZIP, "2026-11-08")
        assert profile["date_formats_seen"].get("YYYY-MM-DD", 0) == 0, (
            "ISO date format appeared unexpectedly — check whether the source "
            "file was replaced with a newer version."
        )

    @needs_closed
    def test_header_to_data_gap_documented(self):
        profile = profile_closed_xlsx(CLOSED_XLSX_ZIP, "2026-11-08")
        gap = profile["header_vs_data_gap"]
        # The known gap is ~1282 records; allow ±500 tolerance for file updates.
        assert gap is not None
        assert 500 < gap < 3000, f"Gap {gap} is outside expected range 500–3000"


class TestRealWsfAccess:
    @needs_wsf
    def test_rectype_01_member_exists(self):
        profile = profile_wsf_access(WSF_ACCESS_ZIP, "2026-11-05")
        assert len(profile["rectype01_members"]) >= 1, (
            "No rectype_01 member found in WSF access ZIP"
        )

    @needs_wsf
    def test_dominant_record_length_is_550(self):
        profile = profile_wsf_access(WSF_ACCESS_ZIP, "2026-11-05")
        assert profile["dominant_record_length"] == 550, (
            f"Record length changed from expected 550: {profile['dominant_record_length']}"
        )

    @needs_wsf
    def test_close_date_positions_still_valid(self):
        profile = profile_wsf_access(WSF_ACCESS_ZIP, "2026-11-05")
        fmt = profile["close_date_format"]
        assert fmt == "YYYYMMDD", (
            f"Close date format at positions 382–389 is '{fmt}', not 'YYYYMMDD'. "
            "Historical parser positions may need updating."
        )

    @needs_wsf
    def test_opeid_fill_rate_high(self):
        profile = profile_wsf_access(WSF_ACCESS_ZIP, "2026-11-05")
        fill = profile["field_fill_rates_in_sample"].get("opeid8", 0)
        assert fill is not None and fill > 0.95, (
            f"OPEID fill rate {fill:.1%} is below 95% — check field positions"
        )

    @needs_wsf
    def test_school_name_fill_rate_high(self):
        profile = profile_wsf_access(WSF_ACCESS_ZIP, "2026-11-05")
        fill = profile["field_fill_rates_in_sample"].get("school_name", 0)
        assert fill is not None and fill > 0.95, (
            f"School name fill rate {fill:.1%} is below 95%"
        )
