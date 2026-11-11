"""
Freeze the Hechinger tracker baseline as a dated, immutable snapshot.

Reads the Hechinger college-closures XLSX and writes the main sheet as a
canonical CSV to data/raw/hechinger/<today>/ alongside a provenance manifest.

Accepted source files:
  college_closures.xlsx          -- legacy export (no opeid6)
  College Closure Data (...).xlsx -- Google Drive export; uses "Original" sheet,
                                     includes opeid6 (stored as float, e.g. 41221.0)

The freeze normalises float-formatted cells: "2008.0" -> "2008",
opeid6 "41221.0" -> "041221" (6-digit zero-padded string).

Usage:
    python scripts/freeze_hechinger_baseline.py
    python scripts/freeze_hechinger_baseline.py --source path/to/export.xlsx
    python scripts/freeze_hechinger_baseline.py --source path/to/export.csv
    python scripts/freeze_hechinger_baseline.py --date 2026-11-10
"""
import argparse
import csv
import hashlib
import io
import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_GD_EXPORT = REPO_ROOT / "College Closure Data (last or only campus) Updated 2-10-25.xlsx"
DEFAULT_SOURCE = _GD_EXPORT if _GD_EXPORT.exists() else REPO_ROOT / "college_closures.xlsx"
RAW_DIR = REPO_ROOT / "data" / "raw" / "hechinger"

REQUIRED_COLUMNS = {"School Name", "State", "Year of Closure"}
EXPECTED_COLUMNS = {"School Name", "State", "Program Length", "School Type",
                    "Year of Closure", "opeid6"}
# Preferred sheet names searched in order (lowercase); "original" is the Google Drive export sheet
MAIN_SHEET_NAMES = {"original", "college_closures", "sheet1"}

# Float-valued columns that should be normalised to plain integers
_FLOAT_INT_COLUMNS = {"Year of Closure", "pgm_length"}

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


# ---------------------------------------------------------------------------
# XLSX parsing
# ---------------------------------------------------------------------------

def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.parse(zf.open("xl/sharedStrings.xml")).getroot()
    result = []
    for si in root.findall(f"{{{NS}}}si"):
        # Plain <t> (simple string)
        t_el = si.find(f"{{{NS}}}t")
        if t_el is not None:
            result.append((t_el.text or "").strip())
            continue
        # Rich text: concatenate all <r><t> run segments
        parts = []
        for r in si.findall(f"{{{NS}}}r"):
            rt = r.find(f"{{{NS}}}t")
            if rt is not None:
                parts.append(rt.text or "")
        result.append("".join(parts).strip())
    return result


def _cell_value(c: ET.Element, shared: list[str]) -> str:
    t = c.get("t", "")
    v_el = c.find(f"{{{NS}}}v")
    raw = (v_el.text or "") if v_el is not None else ""
    if t == "s":
        idx = int(raw) if raw else -1
        return shared[idx] if 0 <= idx < len(shared) else ""
    if t == "inlineStr":
        is_el = c.find(f"{{{NS}}}is")
        t_txt = is_el.find(f"{{{NS}}}t") if is_el is not None else None
        return (t_txt.text or "").strip() if t_txt is not None else ""
    return raw


def _sheet_member_map(zf: zipfile.ZipFile) -> dict[str, str]:
    """Return {sheet_name_lower: xl/worksheets/sheetN.xml} from workbook.xml."""
    wb = ET.parse(zf.open("xl/workbook.xml")).getroot()
    wb_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    # Build rId -> member map from workbook relationships
    rels_member = "xl/_rels/workbook.xml.rels"
    rid_map: dict[str, str] = {}
    if rels_member in zf.namelist():
        rels_root = ET.parse(zf.open(rels_member)).getroot()
        for rel in rels_root:
            rid_map[rel.get("Id", "")] = "xl/" + rel.get("Target", "").lstrip("/")
    result: dict[str, str] = {}
    for sheet in wb.iter(f"{{{wb_ns}}}sheet"):
        name = (sheet.get("name") or "").strip().lower()
        rid = sheet.get(f"{{{r_ns}}}id") or sheet.get("r:id") or ""
        member = rid_map.get(rid, "")
        if member:
            result[name] = member
    return result


def read_xlsx_sheet(xlsx_path: Path, prefer_names: set[str]) -> tuple[list[str], list[list[str]]]:
    """
    Read a sheet from an XLSX file.
    Returns (header_row, data_rows) where both are lists of strings.
    prefer_names: lowercase sheet names to try first (before sheet1).
    Raises ValueError if the file cannot be read or no usable sheet is found.
    """
    if not xlsx_path.exists():
        raise FileNotFoundError(xlsx_path)
    with zipfile.ZipFile(xlsx_path) as zf:
        shared = _shared_strings(zf)
        sheet_map = _sheet_member_map(zf)
        # Try preferred names first, then any name, then sheet1 fallback
        candidates = list(prefer_names & set(sheet_map))
        for other in sorted(sheet_map):
            if other not in prefer_names:
                candidates.append(other)
        # Also fallback to hardcoded sheet1
        if "xl/worksheets/sheet1.xml" in zf.namelist() and not candidates:
            candidates = ["xl/worksheets/sheet1.xml"]
        chosen_member = None
        for name in candidates:
            member = sheet_map.get(name, name)
            if member in zf.namelist():
                chosen_member = member
                break
        if not chosen_member:
            raise ValueError(f"No usable worksheet found in {xlsx_path.name}. "
                             f"Sheets: {list(sheet_map)}")
        ws_root = ET.parse(zf.open(chosen_member)).getroot()
    sheet_data = ws_root.find(f"{{{NS}}}sheetData")
    if sheet_data is None:
        return [], []
    all_rows = [
        [_cell_value(c, shared) for c in row.findall(f"{{{NS}}}c")]
        for row in sheet_data
        if row.tag == f"{{{NS}}}row"
    ]
    if not all_rows:
        return [], []
    header = all_rows[0]
    data = all_rows[1:]
    return header, data


def read_csv_source(csv_path: Path) -> tuple[list[str], list[list[str]]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    if not rows:
        return [], []
    return rows[0], rows[1:]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_header(header: list[str]) -> list[str]:
    """Return list of error strings; empty means valid."""
    present = set(h.strip() for h in header)
    missing = REQUIRED_COLUMNS - present
    if missing:
        return [f"Missing required columns: {sorted(missing)}"]
    return []


def _normalise_float_int(value: str) -> str:
    """Convert "2008.0" -> "2008", "0.0" -> "0". Pass through non-floats unchanged."""
    v = value.strip()
    if not v:
        return v
    try:
        f = float(v)
        if f == int(f):
            return str(int(f))
    except ValueError:
        pass
    return v


def _normalise_opeid6(value: str) -> str:
    """Convert "41221.0" or "41221" to zero-padded 6-digit string "041221"."""
    v = _normalise_float_int(value)
    if not v:
        return v
    if v.isdigit():
        return v.zfill(6)
    return v


def normalise_rows(header: list[str], data: list[list[str]]) -> list[dict[str, str]]:
    """Pad short rows, strip whitespace, normalise float-int columns, return list of dicts."""
    n = len(header)
    clean_header = [h.strip() for h in header]
    result = []
    for row in data:
        padded = row + [""] * max(0, n - len(row))
        record: dict[str, str] = {}
        for i in range(n):
            col = clean_header[i]
            val = padded[i].strip()
            if col in _FLOAT_INT_COLUMNS:
                val = _normalise_float_int(val)
            elif col == "opeid6":
                val = _normalise_opeid6(val)
            record[col] = val
        result.append(record)
    return result


# ---------------------------------------------------------------------------
# SHA-256
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Main freeze logic
# ---------------------------------------------------------------------------

def freeze(
    source: Path,
    snapshot_date: str,
    out_dir: Path | None = None,
) -> dict:
    """
    Freeze the Hechinger baseline from source into out_dir.

    Returns the manifest dict.
    Raises ValueError on schema errors.
    """
    today = snapshot_date  # e.g. "2026-11-10"
    dest = out_dir if out_dir is not None else RAW_DIR / today

    if source.suffix.lower() in {".xlsx", ".xls"}:
        header, data_rows = read_xlsx_sheet(source, MAIN_SHEET_NAMES)
    elif source.suffix.lower() == ".csv":
        header, data_rows = read_csv_source(source)
    else:
        raise ValueError(f"Unsupported file type: {source.suffix}")

    errors = validate_header(header)
    if errors:
        raise ValueError(f"Header validation failed for {source.name}: {errors}")

    records = normalise_rows(header, data_rows)
    # Drop fully-blank rows
    records = [r for r in records if any(v for v in r.values())]

    source_sha256 = sha256_file(source)

    # Serialize to canonical CSV bytes
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header, lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    csv_bytes = buf.getvalue().encode("utf-8")
    csv_sha256 = sha256_bytes(csv_bytes)

    # Year distribution
    yr_field = "Year of Closure"
    yr_counts: dict[str, int] = {}
    for r in records:
        yr = r.get(yr_field, "").strip()
        yr_counts[yr] = yr_counts.get(yr, 0) + 1

    dest.mkdir(parents=True, exist_ok=True)
    csv_name = f"hechinger_baseline_{today}.csv"
    csv_path = dest / csv_name
    csv_path.write_bytes(csv_bytes)

    has_opeid6 = "opeid6" in header
    manifest = {
        "snapshot_date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_file": str(source),
        "source_sha256": source_sha256,
        "csv_file": str(csv_path),
        "csv_sha256": csv_sha256,
        "row_count": len(records),
        "columns": header,
        "has_opeid6": has_opeid6,
        "year_distribution": dict(sorted(yr_counts.items())),
        "notes": (
            "Frozen snapshot of The Hechinger Report college-closures tracker "
            "(SHEEO-sourced, Google Drive export updated 2025-02-10). "
            + ("opeid6 present (6-digit zero-padded); use for OPEID-based matching to FSA. "
               if has_opeid6 else
               "No opeid6 column; matching to federal records requires name+state join. ")
            + "Do not overwrite or modify this file; write a new dated snapshot for updates."
        ),
    }
    manifest_path = dest / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=DEFAULT_SOURCE,
        help="Path to Hechinger export (XLSX or CSV). "
             "Default: Google Drive export if present, else college_closures.xlsx",
    )
    parser.add_argument(
        "--date", default=date.today().isoformat(),
        help="Snapshot date label YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=None,
        help="Override output directory (default: data/raw/hechinger/<date>/)",
    )
    args = parser.parse_args(argv)

    try:
        manifest = freeze(args.source, args.date, args.out_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    dest_dir = Path(manifest["csv_file"]).parent
    print(f"Frozen {manifest['row_count']} rows -> {dest_dir}")
    yr_dist = manifest["year_distribution"]
    total_blank = yr_dist.get("", 0)
    yr_range = [y for y in yr_dist if y]
    if yr_range:
        print(f"  Year range: {min(yr_range)}–{max(yr_range)} "
              f"({total_blank} blank years)")
    print(f"  Source SHA-256: {manifest['source_sha256'][:16]}…")
    print(f"  CSV   SHA-256: {manifest['csv_sha256'][:16]}…")
    print(f"  Manifest: {dest_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
