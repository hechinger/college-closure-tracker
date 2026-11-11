#!/usr/bin/env python3
"""Profile the two FSA federal source files and write a source-profile report.

Outputs:
  docs/SOURCE_PROFILE_<wsf-date>.md   — human-readable findings
  docs/source_profile_<wsf-date>.json — machine-readable profile

Usage (from project root):
  python scripts/profile_sources.py

Override paths:
  python scripts/profile_sources.py \\
      --wsf-access  data/raw/weekly_school_file/2026-11-05/access/schfile_access_format_20261105.dat.zip \\
      --closed-xlsx data/raw/weekly_closed_school_search/2026-11-08/WEEKLY_CLOSED_REPORT_20261108070401179490.zip \\
      --wsf-date 2026-11-05 \\
      --closed-date 2026-11-08
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DOCS = PROJECT / "docs"

WSF_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


# ---------------------------------------------------------------------------
# XLSX streaming (bypasses bad dimension declaration)
# ---------------------------------------------------------------------------

def _get_xlsx_bytes(source: Path) -> bytes:
    """Return raw XLSX bytes from *source*.

    Handles two shapes:
    - A file that IS an XLSX (a ZIP whose top-level members include xl/…)
    - An outer ZIP that wraps an XLSX member (the closed-school file format)
    """
    with zipfile.ZipFile(source) as outer:
        members = outer.namelist()
        if any(m.startswith("xl/") for m in members):
            # source is itself an XLSX — read the bytes directly
            return source.read_bytes()
        xlsx_members = [m for m in members if m.lower().endswith(".xlsx")]
        if xlsx_members:
            return outer.read(xlsx_members[0])
        raise ValueError(
            f"No XLSX content found in {source.name}. Members: {members}"
        )


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    with zf.open("xl/sharedStrings.xml") as f:
        tree = ET.parse(f)
    strings = []
    for si in tree.iter(f"{{{WSF_NS}}}si"):
        strings.append("".join(t.text or "" for t in si.iter(f"{{{WSF_NS}}}t")))
    return strings


def _xlsx_declared_dimension(zf: zipfile.ZipFile, sheet_member: str) -> str | None:
    with zf.open(sheet_member) as f:
        for event, elem in ET.iterparse(f, events=["end"]):
            if elem.tag == f"{{{WSF_NS}}}dimension":
                return elem.get("ref")
            # Stop after the first few elements (dimension is near the top).
            if elem.tag == f"{{{WSF_NS}}}sheetData":
                break
    return None


def _cell_value(c: ET.Element, shared: list[str]) -> str:
    """Extract the display value from a single XLSX <c> element."""
    t = c.get("t", "")
    v_el = c.find(f"{{{WSF_NS}}}v")
    raw = (v_el.text or "") if v_el is not None else ""
    if t == "s":
        return shared[int(raw)].strip() if raw else ""
    if t == "inlineStr":
        is_el = c.find(f"{{{WSF_NS}}}is")
        t_el = is_el.find(f"{{{WSF_NS}}}t") if is_el is not None else None
        return (t_el.text or "").strip() if t_el is not None else ""
    # Numeric / date / formula result — return raw string as-is.
    return raw


def stream_xlsx_rows(zip_path: Path, *, sheet_member: str = "xl/worksheets/sheet1.xml"):
    """Yield each row as a list of strings, bypassing the declared dimension.

    *zip_path* may be an XLSX file directly or an outer ZIP containing an XLSX.

    Uses full DOM parse (ET.parse) instead of iterparse to avoid Python 3.14
    behaviour where elem.clear() after each row end-event corrupts subsequent
    row children in the same iterparse loop.
    """
    xlsx_bytes = _get_xlsx_bytes(zip_path)
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zf:
        shared = _shared_strings(zf)
        with zf.open(sheet_member) as f:
            root = ET.parse(f).getroot()

    sheet_data = root.find(f"{{{WSF_NS}}}sheetData")
    if sheet_data is None:
        return

    for row_elem in sheet_data:
        if row_elem.tag != f"{{{WSF_NS}}}row":
            continue
        row_vals: list[str] = []
        for c in row_elem:
            if c.tag == f"{{{WSF_NS}}}c":
                row_vals.append(_cell_value(c, shared))
        yield row_vals


def _xlsx_declared_dim_from_path(zip_path: Path) -> str | None:
    xlsx_bytes = _get_xlsx_bytes(zip_path)
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zf:
        return _xlsx_declared_dimension(zf, "xl/worksheets/sheet1.xml")


# ---------------------------------------------------------------------------
# Profile: closed-school XLSX
# ---------------------------------------------------------------------------

_DATE_HEADER_SIGNALS = {"closed date", "close date", "closedate", "closure date"}
_OPEID_HEADER_SIGNALS = {"opeid", "ope id", "ope_id"}
_DISTRIBUTION_SENTINEL = "closed schools data distribution"


def _parse_int_cell(cell: str) -> int | None:
    """Parse '24387', '24,387', or '24387.0' as a nonneg whole number, else None."""
    v = cell.strip().replace(",", "")
    if not v:
        return None
    try:
        f = float(v)
        if f >= 0 and f == int(f):
            return int(f)
    except ValueError:
        pass
    return None


def profile_closed_xlsx(zip_path: Path, report_date: str) -> dict:
    declared_dim = _xlsx_declared_dim_from_path(zip_path)

    rows = list(stream_xlsx_rows(zip_path))
    total_rows = len(rows)

    if not rows:
        return {"error": "no rows read", "declared_dimension": declared_dim}

    # 1. Find the column-header row.
    #    Requires BOTH a date-column signal AND an OPEID-column signal.
    #    Initialize as None — fail clearly if not found.
    header_row_idx: int | None = None
    for i, r in enumerate(rows[:100]):
        normalized = {v.strip().lower() for v in r if v.strip()}
        if normalized & _DATE_HEADER_SIGNALS and normalized & _OPEID_HEADER_SIGNALS:
            header_row_idx = i
            break

    if header_row_idx is None:
        return {
            "error": (
                "Column header row (CLOSED DATE / OPEID) not found in first 100 rows. "
                "Cannot parse this workbook."
            ),
            "declared_dimension": declared_dim,
            "total_rows_including_header": total_rows,
        }

    header_row = rows[header_row_idx]

    # 2. Parse labeled summary counts from pre-header rows only.
    #    Match col-A exactly to the label string; never infer from unlabeled numbers.
    def _labeled_count(label: str) -> int | None:
        label_lower = label.strip().lower()
        for r in rows[:header_row_idx]:
            if r and r[0].strip().lower() == label_lower:
                for cell in r[1:]:
                    v = _parse_int_cell(cell)
                    if v is not None:
                        return v
        return None

    header_total = _labeled_count("total")
    domestic_header = _labeled_count("domestic")
    foreign_header = _labeled_count("foreign")

    # 3. Find the CLOSED SCHOOLS DATA DISTRIBUTION end sentinel.
    sentinel_idx: int | None = None
    for i in range(header_row_idx + 1, total_rows):
        r = rows[i]
        if r and r[0].strip().lower() == _DISTRIBUTION_SENTINEL:
            sentinel_idx = i
            break

    # 4. Bound the closure record block explicitly.
    if sentinel_idx is not None:
        data_rows = rows[header_row_idx + 1:sentinel_idx]
        excluded_tail_rows = total_rows - sentinel_idx
    else:
        data_rows = rows[header_row_idx + 1:]
        excluded_tail_rows = 0
    data_count = len(data_rows)

    # 5. Build column-index map from the confirmed header row.
    col = {h.strip().lower(): i for i, h in enumerate(header_row)}

    # Closure date column.
    date_col_idx: int | None = None
    for candidate in ("closed date", "close date", "closedate", "closure date",
                      "date closed", "dateclosed"):
        if candidate in col:
            date_col_idx = col[candidate]
            break

    # OPEID column.
    opeid_col_idx: int | None = None
    for candidate in ("opeid", "ope id", "ope_id", "school id", "schoolid"):
        if candidate in col:
            opeid_col_idx = col[candidate]
            break

    # School name column.
    name_col_idx: int | None = None
    for candidate in ("school name", "schoolname", "institution name", "name"):
        if candidate in col:
            name_col_idx = col[candidate]
            break

    # Country column.
    country_col_idx: int | None = None
    for candidate in ("country (foreign)", "country", "country name", "countryname"):
        if candidate in col:
            country_col_idx = col[candidate]
            break

    # --- Date format detection ---
    date_formats_seen: dict[str, int] = collections.Counter()
    date_values: list[str] = []
    for r in data_rows:
        if date_col_idx is not None and date_col_idx < len(r):
            v = r[date_col_idx].strip()
            if v:
                date_values.append(v)
                if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
                    date_formats_seen["YYYY-MM-DD"] += 1
                elif re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", v):
                    date_formats_seen["MM/DD/YYYY"] += 1
                elif re.match(r"^\d{8}$", v):
                    date_formats_seen["YYYYMMDD"] += 1
                else:
                    date_formats_seen["other"] += 1

    # --- OPEID patterns ---
    opeid_lengths: dict[int, int] = collections.Counter()
    opeid_missing = 0
    for r in data_rows:
        if opeid_col_idx is not None and opeid_col_idx < len(r):
            v = r[opeid_col_idx].strip()
            if v:
                digits = re.sub(r"\D", "", v)
                opeid_lengths[len(digits)] += 1
            else:
                opeid_missing += 1
        else:
            opeid_missing += 1

    # --- Domestic vs foreign ---
    domestic = 0
    foreign = 0
    country_unknown = 0
    for r in data_rows:
        if country_col_idx is not None and country_col_idx < len(r):
            v = r[country_col_idx].strip()
            if not v or v.upper() in ("US", "USA", "UNITED STATES", "U.S."):
                domestic += 1
            else:
                foreign += 1
        else:
            country_unknown += 1

    # --- Date range ---
    parsed_dates: list[dt.date] = []
    for v in date_values[:5000]:  # sample to bound cost
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
            try:
                parsed_dates.append(dt.datetime.strptime(v, fmt).date())
                break
            except ValueError:
                pass
    date_min = str(min(parsed_dates)) if parsed_dates else None
    date_max = str(max(parsed_dates)) if parsed_dates else None

    # --- Duplicates (by OPEID + school name, first 10 000 rows for speed) ---
    seen: set[tuple] = set()
    duplicate_count = 0
    for r in data_rows[:10_000]:
        key = (
            r[opeid_col_idx].strip() if opeid_col_idx is not None and opeid_col_idx < len(r) else "",
            r[name_col_idx].strip().lower() if name_col_idx is not None and name_col_idx < len(r) else "",
        )
        if key in seen:
            duplicate_count += 1
        seen.add(key)

    gap = (header_total - data_count) if header_total is not None else None

    return {
        "source": "weekly_closed_school_search",
        "report_date": report_date,
        "zip_path": str(zip_path),
        "declared_dimension": declared_dim,
        "total_rows_including_header": total_rows,
        "header_row": header_row,
        "header_row_index": header_row_idx,
        "data_row_count": data_count,
        "excluded_tail_rows": excluded_tail_rows,
        "header_reported_total": header_total,
        "header_domestic": domestic_header,
        "header_foreign": foreign_header,
        "header_vs_data_gap": gap,
        "date_column_index": date_col_idx,
        "date_formats_seen": dict(date_formats_seen),
        "date_range": {"min": date_min, "max": date_max},
        "opeid_digit_lengths": {str(k): v for k, v in sorted(opeid_lengths.items())},
        "opeid_missing_count": opeid_missing,
        "domestic_count": domestic,
        "foreign_count": foreign,
        "country_field_absent": country_unknown,
        "duplicate_rows_in_sample": duplicate_count,
        "first_data_row": data_rows[0] if data_rows else [],
    }


# ---------------------------------------------------------------------------
# Profile: Weekly School File fixed-width (Access format)
# ---------------------------------------------------------------------------

# Rectype_01 (School Detail) field positions — confirmed against official FSA layout doc
# "PEPS Weekly School File – School Detail Record Layout (September 1, 2006)".
RECTYPE01_POSITIONS = {
    "opeid8":           (3,   10),
    "school_name":      (12,  81),
    "location_name":    (82,  151),
    "state":            (247, 248),
    "country":          (252, 276),
    "close_date":       (382, 389),   # CLOSE DT — YYYYMMDD
    "location_reason":  (406, 407),   # 2-char reason code for loss of Title IV eligibility — See Table
    "reinstate_date":   (412, 419),   # REINSTATE DT — YYYYMMDD
}

# Rectype_11 (School Closure Information) field positions — dedicated closure record.
# "PEPS Weekly School File – School Closure Information Record Layout (September 1, 2006)".
RECTYPE11_POSITIONS = {
    "opeid8":               (3,   10),
    "closure_dt_current":   (12,  19),   # YYYYMMDD
    "closure_dt_previous":  (20,  27),   # YYYYMMDD
    "history_cd":           (28,  28),
    "unauthorized_loc_ind": (29,  29),   # Y/N
    "tuition_recovery_fund":(30,  30),   # Y/N
    "perkins_ind":          (31,  31),   # Y/N
    "known_amount":         (32,  40),   # TRF recovered, 9-char numeric
    "state_bond_ind":       (41,  41),   # Y/N
    "school_bond_amount":   (42,  50),   # 9-char numeric
    "record_holder_desc":   (51,  100),  # 50-char name of org holding records
    "verified_by":          (101, 170),  # 70-char org that verified closure
    "created_on_dt":        (171, 178),  # YYYYMMDD
    "modified_dt":          (179, 186),  # YYYYMMDD
}

# Rectype_06 (Change of Affiliation) field positions — confirmed empirically against
# schfile_access_format_20261105.dat.zip.  All records are 550 bytes wide.
# coa_actn_cd values: '4' = location-suffix-only change (NOT a merger);
# all other values with OPEID6 change = institutional affiliation change.
RECTYPE06_POSITIONS = {
    "prev_opeid8":    (3,  10),   # OPE ID before the change
    "indicator":      (11, 11),   # 'N' (normal) / 'Y' (3 records); meaning unclear
    "new_opeid8":     (12, 19),   # OPE ID after the change
    "coa_actn_cd":    (20, 20),   # Change-of-affiliation action code (1 char)
    "effective_date": (21, 28),   # Date the change took effect (YYYYMMDD; 17000101 = no date)
    "secondary_cd":   (29, 29),   # Secondary action qualifier (B/C/D or blank)
}

# Keep alias for callers that use the old name.
HISTORICAL_POSITIONS = RECTYPE01_POSITIONS


def _fwf(line: str, start: int, end: int) -> str:
    return line[start - 1:end].strip()


def _detect_date_format(sample: list[str]) -> str:
    for v in sample:
        if not v:
            continue
        if re.match(r"^\d{8}$", v):
            return "YYYYMMDD"
        if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            return "YYYY-MM-DD"
        if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", v):
            return "MM/DD/YYYY"
    return "unknown or empty"


def profile_wsf_access(zip_path: Path, report_date: str, *, sample_limit: int = 2000) -> dict:
    members_found: list[str] = []
    record_type_counts: dict[str, int] = collections.Counter()
    record_lengths: dict[int, int] = collections.Counter()
    field_samples: dict[str, list[str]] = {k: [] for k in RECTYPE01_POSITIONS}
    r11_samples: dict[str, list[str]] = {k: [] for k in RECTYPE11_POSITIONS}
    total_records = 0
    encoding_errors = 0

    with zipfile.ZipFile(zip_path) as zf:
        members_found = zf.namelist()
        rectype01_members = [m for m in members_found if "rectype_01" in m.lower()]
        rectype11_members = [m for m in members_found if "rectype_11" in m.lower()]

        for member_name in zf.namelist():
            rtype = "unknown"
            m = re.search(r"rectype_(\d+)", member_name.lower())
            if m:
                rtype = f"rectype_{m.group(1)}"
            with zf.open(member_name) as fh:
                for raw_line in fh:
                    record_type_counts[rtype] += 1
                    record_lengths[len(raw_line.rstrip(b"\r\n"))] += 1
                    total_records += 1

        # Sample rectype_01 for School Detail field probing.
        for member_name in rectype01_members:
            with zf.open(member_name) as fh:
                for i, raw_line in enumerate(fh):
                    if i >= sample_limit:
                        break
                    try:
                        line = raw_line.decode("latin-1").rstrip("\r\n")
                    except Exception:
                        encoding_errors += 1
                        continue
                    for field, (start, end) in RECTYPE01_POSITIONS.items():
                        if len(line) >= end:
                            field_samples[field].append(_fwf(line, start, end))

        # Sample rectype_11 for School Closure Information field probing.
        for member_name in rectype11_members:
            with zf.open(member_name) as fh:
                for i, raw_line in enumerate(fh):
                    if i >= sample_limit:
                        break
                    try:
                        line = raw_line.decode("latin-1").rstrip("\r\n")
                    except Exception:
                        encoding_errors += 1
                        continue
                    for field, (start, end) in RECTYPE11_POSITIONS.items():
                        if len(line) >= end:
                            r11_samples[field].append(_fwf(line, start, end))

    # Dominant record length for rectype_01.
    rectype01_length = None
    if record_lengths:
        rectype01_length = max(record_lengths, key=record_lengths.get)

    # Non-empty rate for each sampled rectype_01 field.
    field_fill_rates = {}
    for field, samples in field_samples.items():
        non_empty = sum(1 for s in samples if s)
        field_fill_rates[field] = round(non_empty / len(samples), 4) if samples else None

    # Non-empty rate for each sampled rectype_11 field.
    r11_fill_rates = {}
    for field, samples in r11_samples.items():
        non_empty = sum(1 for s in samples if s)
        r11_fill_rates[field] = round(non_empty / len(samples), 4) if samples else None

    # Date format at rectype_01 positions.
    close_fmt = _detect_date_format(field_samples.get("close_date", []))
    reinstate_fmt = _detect_date_format(field_samples.get("reinstate_date", []))

    # Date format for rectype_11 closure date fields.
    r11_close_current_fmt = _detect_date_format(r11_samples.get("closure_dt_current", []))
    r11_close_previous_fmt = _detect_date_format(r11_samples.get("closure_dt_previous", []))

    # Sample OPEID patterns.
    opeid_lengths: dict[int, int] = collections.Counter()
    for v in field_samples.get("opeid8", []):
        digits = re.sub(r"\D", "", v)
        opeid_lengths[len(digits)] += 1

    # Date range from sampled rectype_01 close_dates.
    parsed_dates: list[dt.date] = []
    for v in field_samples.get("close_date", []):
        if not v:
            continue
        for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                parsed_dates.append(dt.datetime.strptime(v, fmt).date())
                break
            except ValueError:
                pass
    date_min = str(min(parsed_dates)) if parsed_dates else None
    date_max = str(max(parsed_dates)) if parsed_dates else None

    # Date range from rectype_11 current closure dates.
    r11_dates: list[dt.date] = []
    for v in r11_samples.get("closure_dt_current", []):
        if not v:
            continue
        for fmt in ("%Y%m%d", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                r11_dates.append(dt.datetime.strptime(v, fmt).date())
                break
            except ValueError:
                pass
    r11_date_min = str(min(r11_dates)) if r11_dates else None
    r11_date_max = str(max(r11_dates)) if r11_dates else None

    return {
        "source": "weekly_school_file_access",
        "report_date": report_date,
        "zip_path": str(zip_path),
        "zip_members": members_found,
        "rectype01_members": rectype01_members,
        "rectype11_members": rectype11_members,
        "total_records_all_types": total_records,
        "record_type_counts": dict(record_type_counts),
        "dominant_record_length": rectype01_length,
        "encoding": "latin-1",
        "encoding_errors_in_sample": encoding_errors,
        "layout_source": "PEPS Weekly School File Layout (September 1, 2006)",
        "rectype01_positions": RECTYPE01_POSITIONS,
        "rectype11_positions": RECTYPE11_POSITIONS,
        "historical_positions": RECTYPE01_POSITIONS,
        "field_fill_rates_in_sample": field_fill_rates,
        "close_date_format": close_fmt,
        "reinstate_date_format": reinstate_fmt,
        "close_date_range_in_sample": {"min": date_min, "max": date_max},
        "opeid_digit_lengths_in_sample": {str(k): v for k, v in sorted(opeid_lengths.items())},
        "field_samples_first_5": {k: v[:5] for k, v in field_samples.items()},
        "rectype11": {
            "field_fill_rates_in_sample": r11_fill_rates,
            "closure_dt_current_format": r11_close_current_fmt,
            "closure_dt_previous_format": r11_close_previous_fmt,
            "closure_dt_current_range": {"min": r11_date_min, "max": r11_date_max},
            "field_samples_first_5": {k: v[:5] for k, v in r11_samples.items()},
        },
    }


# ---------------------------------------------------------------------------
# Markdown report generator
# ---------------------------------------------------------------------------

def _fmt_int(n: int | None) -> str:
    return f"{n:,}" if n is not None else "unknown"


def build_markdown(closed: dict, wsf: dict, generated_at: str) -> str:
    lines = [
        "# Source Profile Report",
        "",
        f"Generated: {generated_at}",
        "",
        "---",
        "",
        "## 1. Weekly Closed School Search File",
        "",
        f"- **Report date:** {closed.get('report_date')}",
        f"- **ZIP path:** `{Path(closed.get('zip_path', '')).name}`",
        f"- **Declared worksheet dimension:** `{closed.get('declared_dimension')}`  ",
        f"  _(Declared dimension is incorrect — parsers must stream XML to read all rows.)_",
        f"- **Actual rows (including header):** {_fmt_int(closed.get('total_rows_including_header'))}",
        f"- **Data rows:** {_fmt_int(closed.get('data_row_count'))}",
        f"- **Header-reported total:** {_fmt_int(closed.get('header_reported_total'))}",
        f"- **Gap (header total − data rows):** {_fmt_int(closed.get('header_vs_data_gap'))}",
        "",
        "### Columns (from header row)",
        "",
    ]
    for i, h in enumerate(closed.get("header_row", [])):
        lines.append(f"  {i}: `{h}`")
    lines += [
        "",
        "### Date field",
        "",
        f"- **Column index:** {closed.get('date_column_index')}",
        f"- **Formats observed:** {closed.get('date_formats_seen')}",
        f"- **Date range (sampled):** {closed.get('date_range', {}).get('min')} → {closed.get('date_range', {}).get('max')}",
        "",
        "### OPEID",
        "",
        f"- **Digit-length distribution:** {closed.get('opeid_digit_lengths')}",
        f"- **Missing OPEID:** {_fmt_int(closed.get('opeid_missing_count'))}",
        "",
        "### Geography",
        "",
        f"- **Domestic (blank or US country field):** {_fmt_int(closed.get('domestic_count'))}",
        f"- **Foreign:** {_fmt_int(closed.get('foreign_count'))}",
        f"- **Country field absent:** {_fmt_int(closed.get('country_field_absent'))}",
        "",
        "### Duplicates",
        "",
        f"- **Duplicate rows in first 10,000 (by OPEID + name):** {_fmt_int(closed.get('duplicate_rows_in_sample'))}",
        "",
        "### First data row",
        "",
        "```",
        str(closed.get("first_data_row", [])),
        "```",
        "",
        "---",
        "",
        "## 2. Weekly School File — Access Format",
        "",
        f"- **Report date:** {wsf.get('report_date')}",
        f"- **ZIP path:** `{Path(wsf.get('zip_path', '')).name}`",
        f"- **Encoding:** `{wsf.get('encoding')}`",
        f"- **Total records (all types):** {_fmt_int(wsf.get('total_records_all_types'))}",
        f"- **Dominant record length:** {wsf.get('dominant_record_length')} bytes",
        "",
        "### ZIP members",
        "",
    ]
    for m in wsf.get("zip_members", []):
        lines.append(f"  - `{m}`")
    lines += [
        "",
        "### Record types",
        "",
    ]
    for rt, count in sorted(wsf.get("record_type_counts", {}).items()):
        lines.append(f"  - `{rt}`: {_fmt_int(count)} records")
    lines += [
        "",
        "### Historical field positions (1-based, from archived build_closure_status.py)",
        "",
        "| Field | Start | End | Fill rate in sample | Notes |",
        "|---|---|---|---|---|",
    ]
    pos = wsf.get("historical_positions", {})
    fills = wsf.get("field_fill_rates_in_sample", {})
    notes_map = {
        "opeid8": "8-digit OPE ID; suffix `00` = main campus",
        "school_name": "Institution name",
        "location_name": "Location/campus name",
        "state": "2-letter state code",
        "country": "Blank for domestic",
        "close_date": "Closure date — **verify format before use**",
        "reinstate_date": "Reinstatement date — **verify format before use**",
    }
    for field, (start, end) in pos.items():
        fill = fills.get(field)
        fill_str = f"{fill:.1%}" if fill is not None else "—"
        lines.append(f"| `{field}` | {start} | {end} | {fill_str} | {notes_map.get(field, '')} |")
    lines += [
        "",
        "### Date format verification",
        "",
        f"- **Close date** (cols 382–389): `{wsf.get('close_date_format')}`",
        f"- **Reinstatement date** (cols 412–419): `{wsf.get('reinstate_date_format')}`",
        "",
    ]

    close_fmt = wsf.get("close_date_format", "")
    if close_fmt == "YYYYMMDD":
        lines.append(
            "> **Close date format confirmed**: `YYYYMMDD` at positions 382–389. "
            "Historical parser positions are valid for this file."
        )
    else:
        lines.append(
            f"> **WARNING**: Close date format at 382–389 is `{close_fmt}`, "
            "not the expected `YYYYMMDD`. Do not reuse historical parser positions "
            "without manual verification."
        )
    lines += [
        "",
        f"- **Close date range (sampled):** {wsf.get('close_date_range_in_sample', {}).get('min')} → "
        f"{wsf.get('close_date_range_in_sample', {}).get('max')}",
        "",
        "### OPEID digit-length distribution (sample)",
        "",
        f"  {wsf.get('opeid_digit_lengths_in_sample')}",
        "",
        "### Field samples (first 5 values from rectype_01 sample)",
        "",
    ]
    for field, samples in wsf.get("field_samples_first_5", {}).items():
        lines.append(f"- **{field}**: {samples}")
    lines += [
        "",
        "---",
        "",
        "## 3. Key findings and parser requirements",
        "",
        "1. **Closed-school XLSX dimension bug**: The worksheet declares "
        f"`{closed.get('declared_dimension')}` but contains "
        f"{_fmt_int(closed.get('data_row_count'))} data rows. "
        "All parsers must stream the worksheet XML or use a library that ignores the dimension attribute.",
        "",
        f"2. **Closed-school header/row gap**: The header reports "
        f"{_fmt_int(closed.get('header_reported_total'))} records; "
        f"{_fmt_int(closed.get('data_row_count'))} rows are exposed. "
        f"Gap: {_fmt_int(closed.get('header_vs_data_gap'))}. "
        "This gap must be documented in release notes and investigated before "
        "using the header total as a headline figure.",
        "",
        "3. **Closed-school date format change**: "
        + (
            "ISO `YYYY-MM-DD` was used in the April 2026 file. "
            + ("The November 2026 file uses `MM/DD/YYYY`." if "MM/DD/YYYY" in str(closed.get("date_formats_seen")) else f"The November 2026 file uses: {closed.get('date_formats_seen')}.")
        ),
        "",
        f"4. **Weekly School File record length**: {wsf.get('dominant_record_length')} bytes per record (rectype_01).",
        "",
        "5. **Historical positions**: Close date at cols 382–389 reads as "
        f"`{wsf.get('close_date_format')}` in the current file. "
        + ("Positions confirmed — parsers may use them." if wsf.get("close_date_format") == "YYYYMMDD"
           else "**Positions need manual verification before use.**"),
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--wsf-access",
        type=Path,
        default=PROJECT / "data/raw/weekly_school_file/2026-11-05/access/schfile_access_format_20261105.dat.zip",
    )
    p.add_argument(
        "--closed-xlsx",
        type=Path,
        default=PROJECT / "data/raw/weekly_closed_school_search/2026-11-08/WEEKLY_CLOSED_REPORT_20261108070401179490.zip",
    )
    p.add_argument("--wsf-date", default="2026-11-05")
    p.add_argument("--closed-date", default="2026-11-08")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    for path in (args.wsf_access, args.closed_xlsx):
        if not path.exists():
            print(f"ERROR: file not found: {path}")
            return 1

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()

    print("Profiling closed-school XLSX…")
    closed_profile = profile_closed_xlsx(args.closed_xlsx, args.closed_date)
    print(f"  Data rows: {closed_profile.get('data_row_count'):,}  "
          f"Header total: {closed_profile.get('header_reported_total')}  "
          f"Gap: {closed_profile.get('header_vs_data_gap')}")
    print(f"  Declared dim: {closed_profile.get('declared_dimension')}")
    print(f"  Date formats: {closed_profile.get('date_formats_seen')}")

    print("Profiling Weekly School File (Access format)…")
    wsf_profile = profile_wsf_access(args.wsf_access, args.wsf_date)
    print(f"  Total records: {wsf_profile.get('total_records_all_types'):,}")
    print(f"  Record length: {wsf_profile.get('dominant_record_length')} bytes")
    print(f"  Close date format at 382-389: {wsf_profile.get('close_date_format')}")
    print(f"  Reinstate date format at 412-419: {wsf_profile.get('reinstate_date_format')}")

    combined = {
        "generated_at": generated_at,
        "weekly_closed_school_search": closed_profile,
        "weekly_school_file_access": wsf_profile,
    }

    DOCS.mkdir(exist_ok=True)
    slug = args.wsf_date
    json_path = DOCS / f"source_profile_{slug}.json"
    md_path = DOCS / f"SOURCE_PROFILE_{slug}.md"

    json_path.write_text(json.dumps(combined, indent=2), encoding="utf-8")
    md_path.write_text(build_markdown(closed_profile, wsf_profile, generated_at), encoding="utf-8")

    print(f"\nWrote: {md_path}")
    print(f"Wrote: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
