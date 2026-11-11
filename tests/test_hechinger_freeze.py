"""Tests for freeze_hechinger_baseline and build_hechinger_reconciliation."""
import csv
import io
import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import freeze_hechinger_baseline as frz
import build_hechinger_reconciliation as recon

# ---------------------------------------------------------------------------
# XLSX builder helper
# ---------------------------------------------------------------------------

def _make_simple_xlsx(rows: list[list[str]]) -> bytes:
    """Build a minimal XLSX ZIP with a single sheet (no shared strings)."""
    NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rows_xml = ""
    for r_idx, row in enumerate(rows, 1):
        cells_xml = ""
        for c_idx, val in enumerate(row):
            col_letter = chr(ord("A") + c_idx)
            cells_xml += (
                f'<c r="{col_letter}{r_idx}" t="inlineStr">'
                f'<is><t>{val}</t></is></c>'
            )
        rows_xml += f'<row r="{r_idx}">{cells_xml}</row>'

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{rows_xml}</sheetData>"
        "</worksheet>"
    ).encode("utf-8")

    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        '<sheet name="college_closures" sheetId="1" r:id="rId1"/>'
        "</sheets></workbook>"
    ).encode("utf-8")

    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    ).encode("utf-8")

    ct_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml"'
        ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml"'
        ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    ).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", ct_xml)
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# freeze_hechinger_baseline tests
# ---------------------------------------------------------------------------

class TestValidateHeader:
    def test_accepts_full_expected_columns(self):
        hdr = ["School Name", "State", "Program Length", "School Type", "Year of Closure"]
        assert frz.validate_header(hdr) == []

    def test_accepts_minimal_required_columns(self):
        hdr = ["School Name", "State", "Year of Closure", "Extra Column"]
        assert frz.validate_header(hdr) == []

    def test_rejects_missing_school_name(self):
        hdr = ["State", "Year of Closure"]
        errors = frz.validate_header(hdr)
        assert errors
        assert any("School Name" in e for e in errors)

    def test_rejects_missing_state(self):
        hdr = ["School Name", "Year of Closure"]
        errors = frz.validate_header(hdr)
        assert errors
        assert any("State" in e for e in errors)

    def test_rejects_missing_year(self):
        hdr = ["School Name", "State", "Program Length"]
        errors = frz.validate_header(hdr)
        assert errors
        assert any("Year of Closure" in e for e in errors)

    def test_strips_whitespace_from_header_before_checking(self):
        hdr = [" School Name ", " State ", " Year of Closure "]
        assert frz.validate_header(hdr) == []


class TestNormaliseRows:
    def test_pads_short_rows(self):
        hdr = ["A", "B", "C"]
        data = [["x", "y"]]
        result = frz.normalise_rows(hdr, data)
        assert result[0] == {"A": "x", "B": "y", "C": ""}

    def test_strips_whitespace_from_values(self):
        hdr = ["School Name", "State"]
        data = [["  Foo College  ", " CA "]]
        result = frz.normalise_rows(hdr, data)
        assert result[0]["School Name"] == "Foo College"
        assert result[0]["State"] == "CA"

    def test_handles_extra_columns_in_row(self):
        hdr = ["A", "B"]
        data = [["x", "y", "z_extra"]]
        result = frz.normalise_rows(hdr, data)
        assert result[0] == {"A": "x", "B": "y"}

    def test_year_float_normalised(self):
        hdr = ["School Name", "State", "Year of Closure"]
        data = [["Foo", "CA", "2023.0"]]
        result = frz.normalise_rows(hdr, data)
        assert result[0]["Year of Closure"] == "2023"

    def test_opeid6_float_zero_padded(self):
        hdr = ["School Name", "State", "Year of Closure", "opeid6"]
        data = [["Foo", "CA", "2023.0", "41221.0"]]
        result = frz.normalise_rows(hdr, data)
        assert result[0]["opeid6"] == "041221"

    def test_opeid6_already_six_digits_unchanged(self):
        hdr = ["School Name", "State", "Year of Closure", "opeid6"]
        data = [["Foo", "CA", "2023", "041221"]]
        result = frz.normalise_rows(hdr, data)
        assert result[0]["opeid6"] == "041221"


class TestNormaliseFloatInt:
    def test_float_to_int(self):
        assert frz._normalise_float_int("2008.0") == "2008"

    def test_plain_int_passthrough(self):
        assert frz._normalise_float_int("2023") == "2023"

    def test_empty_passthrough(self):
        assert frz._normalise_float_int("") == ""

    def test_non_numeric_passthrough(self):
        assert frz._normalise_float_int("abc") == "abc"


class TestNormaliseOpeid6:
    def test_float_to_padded(self):
        assert frz._normalise_opeid6("41221.0") == "041221"

    def test_short_int_padded(self):
        assert frz._normalise_opeid6("1012") == "001012"

    def test_six_digit_unchanged(self):
        assert frz._normalise_opeid6("041221") == "041221"

    def test_empty_passthrough(self):
        assert frz._normalise_opeid6("") == ""


class TestFreezeFromXlsx:
    def test_freeze_writes_csv_and_manifest(self, tmp_path):
        rows = [
            ["School Name", "State", "Program Length", "School Type", "Year of Closure"],
            ["Foo College", "CA", "Bachelor's degree", "Private,nonprofit", "2023"],
            ["Bar University", "TX", "Doctor's Degree", "Private,for profit", "2022"],
        ]
        xlsx_bytes = _make_simple_xlsx(rows)
        src = tmp_path / "export.xlsx"
        src.write_bytes(xlsx_bytes)
        manifest = frz.freeze(src, "2026-11-10", out_dir=tmp_path / "out")
        assert manifest["row_count"] == 2
        assert (tmp_path / "out" / "hechinger_baseline_2026-11-10.csv").exists()
        assert (tmp_path / "out" / "manifest.json").exists()

    def test_manifest_contains_expected_keys(self, tmp_path):
        rows = [
            ["School Name", "State", "Year of Closure"],
            ["Foo College", "CA", "2023"],
        ]
        xlsx_bytes = _make_simple_xlsx(rows)
        src = tmp_path / "export.xlsx"
        src.write_bytes(xlsx_bytes)
        manifest = frz.freeze(src, "2026-11-10", out_dir=tmp_path / "out")
        for key in ("snapshot_date", "source_sha256", "csv_sha256", "row_count",
                    "columns", "year_distribution"):
            assert key in manifest, f"Missing key: {key}"

    def test_year_distribution_is_counted(self, tmp_path):
        rows = [
            ["School Name", "State", "Year of Closure"],
            ["A", "CA", "2023"],
            ["B", "TX", "2023"],
            ["C", "NY", "2022"],
        ]
        xlsx_bytes = _make_simple_xlsx(rows)
        src = tmp_path / "export.xlsx"
        src.write_bytes(xlsx_bytes)
        manifest = frz.freeze(src, "2026-11-10", out_dir=tmp_path / "out")
        assert manifest["year_distribution"]["2023"] == 2
        assert manifest["year_distribution"]["2022"] == 1

    def test_blank_rows_are_excluded(self, tmp_path):
        rows = [
            ["School Name", "State", "Year of Closure"],
            ["Foo College", "CA", "2023"],
            ["", "", ""],
        ]
        xlsx_bytes = _make_simple_xlsx(rows)
        src = tmp_path / "export.xlsx"
        src.write_bytes(xlsx_bytes)
        manifest = frz.freeze(src, "2026-11-10", out_dir=tmp_path / "out")
        assert manifest["row_count"] == 1

    def test_missing_required_column_raises_valueerror(self, tmp_path):
        rows = [["School Name", "Program Length"], ["Foo", "Bachelor"]]
        xlsx_bytes = _make_simple_xlsx(rows)
        src = tmp_path / "export.xlsx"
        src.write_bytes(xlsx_bytes)
        with pytest.raises(ValueError, match="validation failed"):
            frz.freeze(src, "2026-11-10", out_dir=tmp_path / "out")


class TestFreezeFromCsv:
    def test_freeze_reads_csv_source(self, tmp_path):
        csv_content = "School Name,State,Year of Closure\nFoo College,CA,2023\n"
        src = tmp_path / "export.csv"
        src.write_text(csv_content, encoding="utf-8")
        manifest = frz.freeze(src, "2026-11-10", out_dir=tmp_path / "out")
        assert manifest["row_count"] == 1

    def test_freeze_rejects_unsupported_extension(self, tmp_path):
        src = tmp_path / "export.tsv"
        src.write_text("x\ty\n")
        with pytest.raises(ValueError, match="Unsupported file type"):
            frz.freeze(src, "2026-11-10", out_dir=tmp_path / "out")


# ---------------------------------------------------------------------------
# build_hechinger_reconciliation tests
# ---------------------------------------------------------------------------

class TestNormaliseName:
    def test_lowercases_and_strips(self):
        assert recon.normalise_name("  Foo COLLEGE  ") == "foo college"

    def test_replaces_non_alnum(self):
        name = recon.normalise_name("St. Mary's-of-the-Lake")
        assert "'" not in name
        assert "-" not in name
        assert "." not in name


class TestNameSimilarity:
    def test_exact_match_returns_one(self):
        assert recon.name_similarity("Foo College", "Foo College") == pytest.approx(1.0)

    def test_zero_overlap_returns_zero(self):
        assert recon.name_similarity("Alpha Institute", "Beta School") == pytest.approx(0.0)

    def test_partial_overlap(self):
        # "Tri-State" tokens: {tri, state} vs "Tri-County Technical": {tri, county, technical}
        # overlap = {tri} → Jaccard = 1/4 = 0.25, strictly between 0 and 1
        sim = recon.name_similarity("Tri-State College", "Tri-County Technical College")
        assert 0.0 < sim < 1.0

    def test_stopwords_do_not_dominate(self):
        # "College of the Arts" vs "College of the Sciences" should be low
        sim = recon.name_similarity("College of the Arts", "College of the Sciences")
        assert sim < 0.5

    def test_empty_names_return_zero(self):
        assert recon.name_similarity("", "Foo College") == pytest.approx(0.0)
        assert recon.name_similarity("Foo College", "") == pytest.approx(0.0)


class TestStateNameToAbbr:
    def test_full_name_to_abbr(self):
        assert recon._state_name_to_abbr("California") == "CA"
        assert recon._state_name_to_abbr("New York") == "NY"
        assert recon._state_name_to_abbr("West Virginia") == "WV"

    def test_already_abbr_passthrough(self):
        assert recon._state_name_to_abbr("TX") == "TX"
        assert recon._state_name_to_abbr("pa") == "PA"

    def test_unknown_state_truncates(self):
        result = recon._state_name_to_abbr("Unknownland")
        assert len(result) == 2


class TestMatchHechingerToFsa:
    def _fsa(self, name, state, year, opeid="00000000"):
        return {"opeid": opeid, "school_name": name, "state": state,
                "closed_date": f"06/01/{year}", "closed_year": str(year)}

    def _hechinger(self, name, state, year):
        return {"School Name": name, "State": state, "Year of Closure": str(year),
                "Program Length": "", "School Type": ""}

    def test_exact_match_returns_matched_exact(self):
        h = [self._hechinger("Foo College", "California", 2023)]
        f = [self._fsa("Foo College", "CA", 2023, "01234500")]
        results = recon.match_hechinger_to_fsa(h, f)
        assert results[0]["match_status"] == "matched_exact"
        assert results[0]["fsa_opeid"] == "01234500"

    def test_fuzzy_match_returned(self):
        h = [self._hechinger("Foo College of Arts", "California", 2023)]
        f = [self._fsa("Foo Arts College", "CA", 2023, "01234500")]
        results = recon.match_hechinger_to_fsa(h, f)
        assert results[0]["match_status"] in {"matched_exact", "matched_fuzzy"}

    def test_different_state_no_match(self):
        h = [self._hechinger("Foo College", "California", 2023)]
        f = [self._fsa("Foo College", "TX", 2023)]
        results = recon.match_hechinger_to_fsa(h, f)
        assert results[0]["match_status"] == "no_fsa_match"

    def test_year_outside_window_no_match(self):
        h = [self._hechinger("Foo College", "California", 2023)]
        f = [self._fsa("Foo College", "CA", 2010)]
        results = recon.match_hechinger_to_fsa(h, f, year_window=2)
        assert results[0]["match_status"] == "no_fsa_match"

    def test_no_fsa_records_all_unmatched(self):
        h = [self._hechinger("Foo College", "California", 2023)]
        results = recon.match_hechinger_to_fsa(h, [])
        assert results[0]["match_status"] == "no_fsa_match"

    def test_match_score_in_result(self):
        h = [self._hechinger("Foo College", "California", 2023)]
        f = [self._fsa("Foo College", "CA", 2023)]
        results = recon.match_hechinger_to_fsa(h, f)
        assert float(results[0]["match_score"]) > 0.0

    def test_opeid6_match_takes_priority(self):
        # Hechinger has opeid6=041221, name is different; should still match via OPEID6
        h = [{"School Name": "Renamed College", "State": "California",
              "Year of Closure": "2023", "opeid6": "041221",
              "Program Length": "", "School Type": ""}]
        f = [self._fsa("Foo College", "CA", 2023, opeid="04122100")]
        results = recon.match_hechinger_to_fsa(h, f)
        assert results[0]["match_status"] == "matched_opeid6"
        assert results[0]["fsa_opeid"] == "04122100"

    def test_opeid6_in_result_field(self):
        h = [self._hechinger("Foo College", "California", 2023)]
        h[0]["opeid6"] = "041221"
        f = [self._fsa("Foo College", "CA", 2023, opeid="04122100")]
        results = recon.match_hechinger_to_fsa(h, f)
        assert results[0]["hechinger_opeid6"] == "041221"


class TestBuildReconciliationIntegration:
    """Integration test with synthetic FSA XLSX."""

    def _make_fsa_zip(self, tmp_path, records):
        """Build a minimal FSA closed-school ZIP with given records."""
        NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        header = ["CLOSED DATE", "OPEID", "SCHOOL NAME", "LOCATION",
                  "ADDRESS", "CITY", "STATE", "ZIP CODE", "COUNTRY (FOREIGN)"]
        rows_xml = ""
        all_rows = [header] + records
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
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as inner:
            inner.writestr("xl/worksheets/sheet1.xml", sheet_xml)
            inner.writestr("[Content_Types].xml", b"<Types/>")
        xlsx_bytes = buf.getvalue()
        outer_buf = io.BytesIO()
        with zipfile.ZipFile(outer_buf, "w") as outer:
            outer.writestr("WKCL.test.xlsx", xlsx_bytes)
        zip_path = tmp_path / "WEEKLY_CLOSED_REPORT_test.zip"
        zip_path.write_bytes(outer_buf.getvalue())
        return zip_path

    def _make_hechinger_csv(self, tmp_path, rows):
        csv_path = tmp_path / "hechinger_baseline_2026-11-10.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["School Name", "State",
                                                     "Program Length", "School Type",
                                                     "Year of Closure"])
            writer.writeheader()
            writer.writerows(rows)
        return csv_path

    def test_reconciliation_produces_csv_and_summary(self, tmp_path):
        fsa_zip = self._make_fsa_zip(tmp_path, [
            ["06/01/2023", "01234500", "Foo College", "Foo College",
             "1 Main St", "Springfield", "CA", "90000", ""],
        ])
        hech_csv = self._make_hechinger_csv(tmp_path, [
            {"School Name": "Foo College", "State": "California",
             "Program Length": "Bachelor's", "School Type": "Private",
             "Year of Closure": "2023"},
        ])
        summary = recon.build_reconciliation(hech_csv, fsa_zip, tmp_path / "out", "2026-11-10")
        assert summary["hechinger_row_count"] == 1
        assert Path(summary["reconciliation_csv"]).exists()
        assert Path(summary["fsa_only_csv"]).exists()

    def test_no_fsa_match_recorded(self, tmp_path):
        fsa_zip = self._make_fsa_zip(tmp_path, [
            ["06/01/2015", "01234500", "Alpha Institute", "Alpha",
             "1 Oak Ave", "Portland", "OR", "97000", ""],
        ])
        hech_csv = self._make_hechinger_csv(tmp_path, [
            {"School Name": "Beta University", "State": "California",
             "Program Length": "Bachelor's", "School Type": "Private",
             "Year of Closure": "2015"},
        ])
        summary = recon.build_reconciliation(hech_csv, fsa_zip, tmp_path / "out", "2026-11-10")
        counts = summary["match_status_counts"]
        assert counts.get("no_fsa_match", 0) == 1
