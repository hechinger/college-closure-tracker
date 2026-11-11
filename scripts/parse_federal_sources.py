"""
Parse both FSA federal source files into normalized, flat CSV tables.

No closure classification is performed here. Records are extracted verbatim
with added OPEID6, location suffix, ISO-normalised dates, and provenance fields.

Outputs (in data/derived/):
  fsa_closed_school_<closed-date>.csv    — from XLSX: one row per closure record
  fsa_wsf_rt01_<wsf-date>.csv            — from WSF rectype_01: one row per school location
  fsa_wsf_rt11_<wsf-date>.csv            — from WSF rectype_11: one row per closure detail record

Usage (from project root):
  python scripts/parse_federal_sources.py
  python scripts/parse_federal_sources.py --out-dir data/derived
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import re
import sys
import zipfile
from pathlib import Path

# Add scripts directory to path so we can import from profile_sources
sys.path.insert(0, str(Path(__file__).resolve().parent))
from profile_sources import (
    stream_xlsx_rows,
    RECTYPE01_POSITIONS,
    RECTYPE06_POSITIONS,
    RECTYPE11_POSITIONS,
)

PROJECT = Path(__file__).resolve().parent.parent
RAW_CLOSED = PROJECT / "data" / "raw" / "weekly_closed_school_search"
RAW_WSF = PROJECT / "data" / "raw" / "weekly_school_file"
DERIVED = PROJECT / "data" / "derived"

# XLSX column-header signals (lowercase)
_DATE_SIGNALS = {"closed date", "close date", "closedate", "closure date"}
_OPEID_SIGNALS = {"opeid", "ope id", "ope_id"}
_DISTRIBUTION_SENTINEL = "closed schools data distribution"

# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _iso_from_mmddyyyy(v: str) -> str:
    """'10/31/2026' -> '2026-10-31'.  Returns '' on failure."""
    v = v.strip()
    if not v:
        return ""
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", v)
    if not m:
        return ""
    try:
        return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:
        return ""


def _iso_from_yyyymmdd(v: str) -> str:
    """'20261031' -> '2026-10-31'.  Returns '' on failure."""
    v = v.strip()
    if len(v) != 8 or not v.isdigit():
        return ""
    try:
        return dt.date(int(v[:4]), int(v[4:6]), int(v[6:])).isoformat()
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# OPEID helpers
# ---------------------------------------------------------------------------

def _split_opeid8(opeid8: str) -> tuple[str, str, str]:
    """
    Return (opeid8_padded, opeid6, location_suffix).
    opeid8_padded: zero-padded to 8 digits.
    opeid6: first 6 digits.
    location_suffix: last 2 digits (00 = main/only campus).
    """
    digits = re.sub(r"\D", "", opeid8).zfill(8)
    return digits, digits[:6], digits[6:]


# ---------------------------------------------------------------------------
# Location-name heuristic (SHEEO 2024: scrutinize LOC NAME for non-campus sites)
# ---------------------------------------------------------------------------

# Sub-unit keywords that suggest a closure record is an academic department,
# off-campus site, or teaching centre rather than a standalone postsecondary campus.
_SUSPICIOUS_LOC_KEYWORDS = (
    "department of", "dept of", "dept.",
    "school of", "college of",
    "teacher education", "teacher ed ", "teaching site",
    "off-campus", "off campus",
    "distance learning", "distance education",
    "continuing education", "continuing ed",
    "extension site", "extension center",
    "satellite site", "satellite center",
    "dual enrollment",
)


def _is_suspicious_location(location_name: str, school_name: str = "") -> bool:
    """
    Return True when location_name contains patterns SHEEO identifies as
    indicating the record is a sub-unit or non-campus site rather than a
    standalone institution.  Flag only — not auto-excluded.
    """
    if not location_name:
        return False
    loc = location_name.lower().strip()
    if not any(kw in loc for kw in _SUSPICIOUS_LOC_KEYWORDS):
        return False
    # If location_name and school_name are the same institution, the keyword hit
    # is part of the school's real name (e.g. "School of Visual Arts"), not a
    # sub-unit label. Only flag when they clearly differ.
    if school_name and loc == school_name.lower().strip():
        return False
    return True


# ---------------------------------------------------------------------------
# Parse FSA closed-school XLSX
# ---------------------------------------------------------------------------

def _find_col(col_map: dict[str, int], signals: list[str]) -> int | None:
    for s in signals:
        if s in col_map:
            return col_map[s]
    return None


def parse_closed_school_xlsx(zip_path: Path, source_date: str) -> list[dict]:
    """
    Parse FSA closed-school XLSX and return list of normalised record dicts.
    source_date: YYYY-MM-DD string from the report filename.
    """
    rows = list(stream_xlsx_rows(zip_path))
    if not rows:
        raise ValueError(f"No rows read from {zip_path.name}")

    # Find column-header row (needs date + OPEID signal)
    header_idx: int | None = None
    for i, r in enumerate(rows[:100]):
        normed = {v.strip().lower() for v in r if v.strip()}
        if normed & _DATE_SIGNALS and normed & _OPEID_SIGNALS:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(
            f"Column header row not found in first 100 rows of {zip_path.name}"
        )

    header = rows[header_idx]
    col = {h.strip().lower(): i for i, h in enumerate(header)}

    opeid_idx = _find_col(col, ["opeid", "ope id", "ope_id"])
    name_idx = _find_col(col, ["school name", "schoolname", "institution name"])
    loc_idx = _find_col(col, ["location"])
    addr_idx = _find_col(col, ["address"])
    city_idx = _find_col(col, ["city"])
    state_idx = _find_col(col, ["state"])
    zip_idx = _find_col(col, ["zip code", "zip"])
    country_idx = _find_col(col, ["country (foreign)", "country"])
    date_idx = _find_col(col, ["closed date", "close date", "closedate", "closure date"])

    # Sentinel row
    sentinel_idx = len(rows)
    for i in range(header_idx + 1, len(rows)):
        if rows[i] and rows[i][0].strip().lower() == _DISTRIBUTION_SENTINEL:
            sentinel_idx = i
            break

    def get(row: list[str], idx: int | None) -> str:
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    records = []
    for row in rows[header_idx + 1 : sentinel_idx]:
        opeid8_raw = get(row, opeid_idx)
        opeid8, opeid6, loc_suffix = _split_opeid8(opeid8_raw)
        closed_date   = get(row, date_idx)
        location_name = get(row, loc_idx)
        school_name   = get(row, name_idx)
        records.append({
            "source_date":           source_date,
            "source_file":           zip_path.name,
            "opeid8":                opeid8,
            "opeid6":                opeid6,
            "location_suffix":       loc_suffix,
            "school_name":           school_name,
            "location_name":         location_name,
            "is_suspicious_location": "true" if _is_suspicious_location(location_name, school_name) else "false",
            "address":               get(row, addr_idx),
            "city":                  get(row, city_idx),
            "state":                 get(row, state_idx),
            "zip":                   get(row, zip_idx),
            "country":               get(row, country_idx),
            "closed_date":           closed_date,
            "closed_date_iso":       _iso_from_mmddyyyy(closed_date),
        })
    return records


CLOSED_SCHOOL_FIELDS = [
    "source_date", "source_file",
    "opeid8", "opeid6", "location_suffix",
    "school_name", "location_name", "is_suspicious_location",
    "address", "city", "state", "zip", "country",
    "closed_date", "closed_date_iso",
]


# ---------------------------------------------------------------------------
# Parse FSA Weekly School File — fixed-width
# ---------------------------------------------------------------------------

def _fwf(line: bytes, start: int, end: int, encoding: str = "latin-1") -> str:
    """Extract fixed-width field [start:end] (1-indexed, inclusive)."""
    s, e = start - 1, end
    if len(line) < e:
        return ""
    return line[s:e].decode(encoding, errors="replace").strip()


def parse_wsf_rectype01(zip_path: Path, source_date: str) -> list[dict]:
    """
    Parse all rectype_01 members from the WSF access-format ZIP.
    Returns list of normalised record dicts.
    """
    p = RECTYPE01_POSITIONS
    records = []
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.namelist()
                   if re.search(r"rectype_01", m, re.IGNORECASE)]
        for member in members:
            with zf.open(member) as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    opeid8_raw = _fwf(line, *p["opeid8"])
                    opeid8, opeid6, loc_suffix = _split_opeid8(opeid8_raw)
                    close_date = _fwf(line, *p["close_date"])
                    reinstate_date = _fwf(line, *p["reinstate_date"])
                    records.append({
                        "source_date": source_date,
                        "source_file": zip_path.name,
                        "member": member,
                        "opeid8": opeid8,
                        "opeid6": opeid6,
                        "location_suffix": loc_suffix,
                        "school_name": _fwf(line, *p["school_name"]),
                        "location_name": _fwf(line, *p["location_name"]),
                        "state": _fwf(line, *p["state"]),
                        "country": _fwf(line, *p["country"]),
                        "close_date": close_date,
                        "close_date_iso": _iso_from_yyyymmdd(close_date),
                        "location_reason": _fwf(line, *p["location_reason"]),
                        "reinstate_date": reinstate_date,
                        "reinstate_date_iso": _iso_from_yyyymmdd(reinstate_date),
                    })
    return records


WSF_RT01_FIELDS = [
    "source_date", "source_file", "member",
    "opeid8", "opeid6", "location_suffix",
    "school_name", "location_name", "state", "country",
    "close_date", "close_date_iso",
    "location_reason",
    "reinstate_date", "reinstate_date_iso",
]


def parse_wsf_rectype06(zip_path: Path, source_date: str) -> list[dict]:
    """
    Parse all rectype_06 (Change of Affiliation) members from the WSF ZIP.

    coa_actn_cd='4' means a location-suffix-only renumber — NOT a merger.
    All other codes where prev_opeid6 != new_opeid6 signal an institutional
    affiliation change (potential merger/absorption).

    effective_date '17000101' is the system sentinel for "no date on record".
    """
    p = RECTYPE06_POSITIONS
    records = []
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.namelist()
                   if re.search(r"rectype_06", m, re.IGNORECASE)]
        for member in members:
            with zf.open(member) as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    prev_raw = _fwf(line, *p["prev_opeid8"])
                    new_raw  = _fwf(line, *p["new_opeid8"])
                    prev8, prev6, prev_suf = _split_opeid8(prev_raw)
                    new8,  new6,  new_suf  = _split_opeid8(new_raw)
                    eff_raw = _fwf(line, *p["effective_date"])
                    # '17000101' is the FSA sentinel for "no effective date"
                    eff_iso = _iso_from_yyyymmdd(eff_raw) if eff_raw != "17000101" else ""
                    records.append({
                        "source_date":          source_date,
                        "source_file":          zip_path.name,
                        "member":               member,
                        "prev_opeid8":          prev8,
                        "prev_opeid6":          prev6,
                        "prev_location_suffix": prev_suf,
                        "indicator":            _fwf(line, *p["indicator"]),
                        "new_opeid8":           new8,
                        "new_opeid6":           new6,
                        "new_location_suffix":  new_suf,
                        "coa_actn_cd":          _fwf(line, *p["coa_actn_cd"]),
                        "effective_date":       eff_raw,
                        "effective_date_iso":   eff_iso,
                        "secondary_cd":         _fwf(line, *p["secondary_cd"]),
                        "opeid6_changed":       "true" if prev6 != new6 else "false",
                    })
    return records


WSF_RT06_FIELDS = [
    "source_date", "source_file", "member",
    "prev_opeid8", "prev_opeid6", "prev_location_suffix",
    "indicator",
    "new_opeid8", "new_opeid6", "new_location_suffix",
    "coa_actn_cd", "effective_date", "effective_date_iso",
    "secondary_cd", "opeid6_changed",
]


def parse_wsf_rectype11(zip_path: Path, source_date: str) -> list[dict]:
    """
    Parse all rectype_11 members from the WSF access-format ZIP.
    Returns list of normalised record dicts.
    """
    p = RECTYPE11_POSITIONS
    records = []
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.namelist()
                   if re.search(r"rectype_11", m, re.IGNORECASE)]
        for member in members:
            with zf.open(member) as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    opeid8_raw = _fwf(line, *p["opeid8"])
                    opeid8, opeid6, loc_suffix = _split_opeid8(opeid8_raw)
                    dt_cur = _fwf(line, *p["closure_dt_current"])
                    dt_prev = _fwf(line, *p["closure_dt_previous"])
                    created = _fwf(line, *p["created_on_dt"])
                    modified = _fwf(line, *p["modified_dt"])
                    records.append({
                        "source_date": source_date,
                        "source_file": zip_path.name,
                        "member": member,
                        "opeid8": opeid8,
                        "opeid6": opeid6,
                        "location_suffix": loc_suffix,
                        "closure_dt_current": dt_cur,
                        "closure_dt_current_iso": _iso_from_yyyymmdd(dt_cur),
                        "closure_dt_previous": dt_prev,
                        "closure_dt_previous_iso": _iso_from_yyyymmdd(dt_prev),
                        "history_cd": _fwf(line, *p["history_cd"]),
                        "unauthorized_loc_ind": _fwf(line, *p["unauthorized_loc_ind"]),
                        "tuition_recovery_fund": _fwf(line, *p["tuition_recovery_fund"]),
                        "perkins_ind": _fwf(line, *p["perkins_ind"]),
                        "known_amount": _fwf(line, *p["known_amount"]),
                        "state_bond_ind": _fwf(line, *p["state_bond_ind"]),
                        "school_bond_amount": _fwf(line, *p["school_bond_amount"]),
                        "record_holder_desc": _fwf(line, *p["record_holder_desc"]),
                        "verified_by": _fwf(line, *p["verified_by"]),
                        "created_on_dt": created,
                        "created_on_dt_iso": _iso_from_yyyymmdd(created),
                        "modified_dt": modified,
                        "modified_dt_iso": _iso_from_yyyymmdd(modified),
                    })
    return records


WSF_RT11_FIELDS = [
    "source_date", "source_file", "member",
    "opeid8", "opeid6", "location_suffix",
    "closure_dt_current", "closure_dt_current_iso",
    "closure_dt_previous", "closure_dt_previous_iso",
    "history_cd", "unauthorized_loc_ind", "tuition_recovery_fund",
    "perkins_ind", "known_amount", "state_bond_ind", "school_bond_amount",
    "record_holder_desc", "verified_by",
    "created_on_dt", "created_on_dt_iso",
    "modified_dt", "modified_dt_iso",
]


# ---------------------------------------------------------------------------
# File discovery helpers
# ---------------------------------------------------------------------------

def _latest_closed_zip() -> tuple[Path, str] | None:
    """Return (zip_path, date_string) for the most recent closed-school ZIP."""
    hits: list[tuple[str, Path]] = []
    for dated_dir in RAW_CLOSED.iterdir():
        if not dated_dir.is_dir():
            continue
        for f in dated_dir.iterdir():
            if f.suffix == ".zip":
                hits.append((dated_dir.name, f))
    if not hits:
        return None
    date_str, path = sorted(hits)[-1]
    return path, date_str


def _latest_wsf_access_zip() -> tuple[Path, str] | None:
    """Return (zip_path, date_string) for the most recent WSF access-format ZIP."""
    hits: list[tuple[str, Path]] = []
    for dated_dir in RAW_WSF.iterdir():
        if not dated_dir.is_dir():
            continue
        for access_dir in dated_dir.iterdir():
            if access_dir.is_dir() and access_dir.name.lower() == "access":
                for f in access_dir.iterdir():
                    if f.suffix == ".zip":
                        hits.append((dated_dir.name, f))
    if not hits:
        return None
    date_str, path = sorted(hits)[-1]
    return path, date_str


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def _write_csv(records: list[dict], fields: list[str], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--closed-zip", type=Path, default=None,
                        help="Path to FSA closed-school ZIP (default: latest in data/raw/)")
    parser.add_argument("--wsf-zip", type=Path, default=None,
                        help="Path to WSF access-format ZIP (default: latest in data/raw/)")
    parser.add_argument("--closed-date", default=None,
                        help="YYYY-MM-DD date label for closed-school file")
    parser.add_argument("--wsf-date", default=None,
                        help="YYYY-MM-DD date label for WSF file")
    parser.add_argument("--out-dir", type=Path, default=DERIVED,
                        help="Output directory (default: data/derived/)")
    args = parser.parse_args(argv)

    # Resolve closed-school file
    closed_zip, closed_date = args.closed_zip, args.closed_date
    if closed_zip is None:
        result = _latest_closed_zip()
        if result is None:
            print("ERROR: No closed-school ZIP found under data/raw/weekly_closed_school_search/",
                  file=sys.stderr)
            return 1
        closed_zip, closed_date = result
    if closed_date is None:
        closed_date = closed_zip.parent.name

    # Resolve WSF file
    wsf_zip, wsf_date = args.wsf_zip, args.wsf_date
    if wsf_zip is None:
        result = _latest_wsf_access_zip()
        if result is None:
            print("ERROR: No WSF access-format ZIP found under data/raw/weekly_school_file/",
                  file=sys.stderr)
            return 1
        wsf_zip, wsf_date = result
    if wsf_date is None:
        wsf_date = wsf_zip.parent.parent.name

    out_dir: Path = args.out_dir

    errors = 0

    # --- Closed-school XLSX ---
    try:
        print(f"Parsing closed-school XLSX: {closed_zip.name} ({closed_date})")
        closed_records = parse_closed_school_xlsx(closed_zip, closed_date)
        out = out_dir / f"fsa_closed_school_{closed_date}.csv"
        _write_csv(closed_records, CLOSED_SCHOOL_FIELDS, out)
        print(f"  {len(closed_records):,} records -> {out}")
    except Exception as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        errors += 1

    # --- WSF rectype_01 ---
    try:
        print(f"Parsing WSF rectype_01: {wsf_zip.name} ({wsf_date})")
        rt01_records = parse_wsf_rectype01(wsf_zip, wsf_date)
        out = out_dir / f"fsa_wsf_rt01_{wsf_date}.csv"
        _write_csv(rt01_records, WSF_RT01_FIELDS, out)
        print(f"  {len(rt01_records):,} records -> {out}")
    except Exception as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        errors += 1

    # --- WSF rectype_06 ---
    try:
        print(f"Parsing WSF rectype_06: {wsf_zip.name} ({wsf_date})")
        rt06_records = parse_wsf_rectype06(wsf_zip, wsf_date)
        out = out_dir / f"fsa_wsf_rt06_{wsf_date}.csv"
        _write_csv(rt06_records, WSF_RT06_FIELDS, out)
        opeid6_changed = sum(1 for r in rt06_records if r["opeid6_changed"] == "true")
        print(f"  {len(rt06_records):,} records -> {out}  ({opeid6_changed:,} with OPEID6 change)")
    except Exception as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        errors += 1

    # --- WSF rectype_11 ---
    try:
        print(f"Parsing WSF rectype_11: {wsf_zip.name} ({wsf_date})")
        rt11_records = parse_wsf_rectype11(wsf_zip, wsf_date)
        out = out_dir / f"fsa_wsf_rt11_{wsf_date}.csv"
        _write_csv(rt11_records, WSF_RT11_FIELDS, out)
        print(f"  {len(rt11_records):,} records -> {out}")
    except Exception as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        errors += 1

    return errors


if __name__ == "__main__":
    sys.exit(main())
