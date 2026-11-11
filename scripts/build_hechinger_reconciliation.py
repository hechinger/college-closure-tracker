"""
Build a reconciliation table: Hechinger tracker baseline vs. FSA closed-school data.

For each Hechinger row, find matching FSA records (name+state, year±2 window).
Also flag FSA records with US domestic closures in Hechinger's year range that
have no Hechinger match — potential gaps in the tracker.

Output:
  data/derived/hechinger_reconciliation_<date>.csv
  data/derived/hechinger_reconciliation_<date>_summary.json

Usage:
    python scripts/build_hechinger_reconciliation.py
    python scripts/build_hechinger_reconciliation.py --baseline data/raw/hechinger/2026-11-10/hechinger_baseline_2026-11-10.csv
    python scripts/build_hechinger_reconciliation.py --fsa-zip data/raw/weekly_closed_school_search/2026-11-08/WEEKLY_CLOSED_REPORT_20261108070401179490.zip
"""
import argparse
import csv
import io
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_HECHINGER = REPO_ROOT / "data" / "raw" / "hechinger"
RAW_FSA = REPO_ROOT / "data" / "raw" / "weekly_closed_school_search"
DERIVED_DIR = REPO_ROOT / "data" / "derived"

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

# US state abbreviations (for filtering FSA domestic records)
US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC","PR","GU","VI","AS","MP",
}

STOPWORDS = {"college", "university", "school", "of", "the", "and", "at",
             "for", "in", "a", "an", "community", "technical", "institute",
             "technology"}


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------

def normalise_name(name: str) -> str:
    name = (name or "").lower().strip()
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def name_tokens(name: str) -> frozenset[str]:
    return frozenset(t for t in normalise_name(name).split() if t not in STOPWORDS and len(t) > 1)


def name_similarity(a: str, b: str) -> float:
    """Jaccard similarity of non-stopword tokens."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# FSA XLSX loading (re-uses profile_sources streaming approach)
# ---------------------------------------------------------------------------

def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.parse(zf.open("xl/sharedStrings.xml")).getroot()
    result = []
    for si in root.findall(f"{{{NS}}}si"):
        t_el = si.find(f"{{{NS}}}t")
        result.append((t_el.text or "").strip() if t_el is not None else "")
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


def _get_xlsx_bytes(zip_path: Path) -> bytes:
    with zipfile.ZipFile(zip_path) as outer:
        members = outer.namelist()
        if any(m.startswith("xl/") for m in members):
            return zip_path.read_bytes()
        xlsx_members = [m for m in members if m.lower().endswith(".xlsx")]
        if xlsx_members:
            return outer.read(xlsx_members[0])
    raise ValueError(f"No XLSX content found in {zip_path.name}")


def stream_fsa_records(zip_path: Path) -> list[dict]:
    """
    Load FSA closed-school XLSX, skip summary header section and sentinel tail.
    Returns list of dicts with keys: opeid, school_name, state, closed_date, closed_year.
    """
    xlsx_bytes = _get_xlsx_bytes(zip_path)
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as zf:
        shared = _shared_strings(zf)
        ws_root = ET.parse(zf.open("xl/worksheets/sheet1.xml")).getroot()
    sheet_data = ws_root.find(f"{{{NS}}}sheetData")
    if sheet_data is None:
        return []

    all_rows = []
    for row_elem in sheet_data:
        if row_elem.tag != f"{{{NS}}}row":
            continue
        vals = [_cell_value(c, shared) for c in row_elem if c.tag == f"{{{NS}}}c"]
        all_rows.append(vals)

    # Find header row (requires OPEID and CLOSED DATE columns)
    header_idx = None
    for i, row in enumerate(all_rows):
        row_lower = [v.lower().strip() for v in row]
        if any("opeid" in v or "ope id" in v for v in row_lower) and \
           any("closed date" in v or "close date" in v for v in row_lower):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Could not find header row in FSA XLSX")

    header = all_rows[header_idx]
    hdr_lower = [h.lower().strip() for h in header]

    def col(signals: list[str]) -> int | None:
        for sig in signals:
            for i, h in enumerate(hdr_lower):
                if sig in h:
                    return i
        return None

    opeid_col = col(["opeid", "ope id", "ope_id"])
    name_col = col(["school name", "institution name"])
    state_col = col(["state"])
    date_col = col(["closed date", "close date"])
    country_col = col(["country"])

    # Sentinel row
    sentinel_idx = len(all_rows)
    for i in range(header_idx + 1, len(all_rows)):
        first = (all_rows[i][0] if all_rows[i] else "").lower().strip()
        if "closed schools data distribution" in first:
            sentinel_idx = i
            break

    records = []
    for row in all_rows[header_idx + 1 : sentinel_idx]:
        def get(idx):
            if idx is None or idx >= len(row):
                return ""
            return row[idx].strip()

        country = get(country_col)
        state = get(state_col)
        # Only US domestic records
        if country and state not in US_STATES:
            continue

        date_str = get(date_col)
        closed_year = ""
        if date_str:
            # MM/DD/YYYY
            parts = date_str.split("/")
            if len(parts) == 3 and len(parts[2]) == 4:
                closed_year = parts[2]

        records.append({
            "opeid": get(opeid_col),
            "school_name": get(name_col),
            "state": state,
            "closed_date": date_str,
            "closed_year": closed_year,
        })
    return records


# ---------------------------------------------------------------------------
# Hechinger CSV loading
# ---------------------------------------------------------------------------

def load_hechinger(csv_path: Path) -> list[dict]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return [
            {k.strip(): (v or "").strip() for k, v in row.items()}
            for row in reader
            if any((v or "").strip() for v in row.values())
        ]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def find_latest_baseline(raw_hechinger_dir: Path) -> Path | None:
    dated_dirs = sorted(
        (d for d in raw_hechinger_dir.iterdir() if d.is_dir()),
        reverse=True,
    )
    for d in dated_dirs:
        for f in sorted(d.iterdir()):
            if f.suffix == ".csv" and "hechinger_baseline" in f.name:
                return f
    return None


def find_latest_fsa_zip(raw_fsa_dir: Path) -> Path | None:
    zips = []
    for dated_dir in raw_fsa_dir.iterdir():
        if not dated_dir.is_dir():
            continue
        for f in dated_dir.iterdir():
            if f.suffix == ".zip" and "CLOSED_REPORT" in f.name.upper():
                zips.append(f)
    return sorted(zips)[-1] if zips else None


def match_hechinger_to_fsa(
    hechinger_records: list[dict],
    fsa_records: list[dict],
    year_window: int = 2,
) -> list[dict]:
    """
    For each Hechinger row, find the best matching FSA row.

    Matching priority:
      1. opeid6 exact match (if Hechinger has opeid6 column)
      2. name + state Jaccard similarity (threshold 0.5), with ±year_window filter
    Returns list of reconciliation rows.
    """
    # Build FSA lookup by opeid6 (first 6 digits of opeid8)
    fsa_by_opeid6: dict[str, list[dict]] = {}
    for r in fsa_records:
        opeid = r["opeid"].zfill(8)
        opeid6 = opeid[:6]
        fsa_by_opeid6.setdefault(opeid6, []).append(r)

    # Build FSA lookup by state for name-matching fallback
    fsa_by_state: dict[str, list[dict]] = {}
    for r in fsa_records:
        st = r["state"].upper()
        fsa_by_state.setdefault(st, []).append(r)

    results = []
    for h in hechinger_records:
        h_name = h.get("School Name", "")
        h_state = h.get("State", "")
        h_year_str = h.get("Year of Closure", "")
        h_opeid6 = h.get("opeid6", "").strip().zfill(6) if h.get("opeid6", "").strip() else ""

        h_state_abbr = _state_name_to_abbr(h_state)
        h_year = int(h_year_str) if h_year_str.isdigit() else None

        best_match: dict = {}
        best_score = 0.0
        status = "no_fsa_match"

        # --- Pass 1: exact OPEID6 match ---
        if h_opeid6:
            opeid6_candidates = fsa_by_opeid6.get(h_opeid6, [])
            if opeid6_candidates:
                # Pick the candidate closest in year; if tied, use the most recent
                def year_dist(r):
                    if h_year is None or not r["closed_year"].isdigit():
                        return 9999
                    return abs(int(r["closed_year"]) - h_year)
                best_match = min(opeid6_candidates, key=year_dist)
                best_score = 1.0
                status = "matched_opeid6"

        # --- Pass 2: name+state Jaccard fallback ---
        if not best_match:
            candidates = fsa_by_state.get(h_state_abbr, [])
            for fsa in candidates:
                if h_year is not None and fsa["closed_year"].isdigit():
                    if abs(int(fsa["closed_year"]) - h_year) > year_window:
                        continue
                sim = name_similarity(h_name, fsa["school_name"])
                if sim > best_score:
                    best_score = sim
                    best_match = fsa

            MATCH_THRESHOLD = 0.5
            if best_match and best_score >= MATCH_THRESHOLD:
                status = "matched_exact" if best_score >= 0.9 else "matched_fuzzy"
            else:
                status = "no_fsa_match"
                best_match = {}

        results.append({
            "hechinger_name": h_name,
            "hechinger_state": h_state,
            "hechinger_state_abbr": h_state_abbr,
            "hechinger_year": h_year_str,
            "hechinger_opeid6": h_opeid6,
            "hechinger_program_length": h.get("Program Length", ""),
            "hechinger_school_type": h.get("School Type", ""),
            "fsa_opeid": best_match.get("opeid", ""),
            "fsa_school_name": best_match.get("school_name", ""),
            "fsa_state": best_match.get("state", ""),
            "fsa_closed_date": best_match.get("closed_date", ""),
            "fsa_closed_year": best_match.get("closed_year", ""),
            "match_score": f"{best_score:.2f}",
            "match_status": status,
        })
    return results


def _fsa_only_records(
    fsa_records: list[dict],
    hechinger_records: list[dict],
    hechinger_year_min: int,
    hechinger_year_max: int,
    year_window: int = 2,
) -> list[dict]:
    """
    Return FSA records in the Hechinger year range that matched no Hechinger row.
    """
    # Build set of (norm_name, state_abbr) from Hechinger
    matched_keys: set[tuple[str, str]] = set()
    for h in hechinger_records:
        h_state_abbr = _state_name_to_abbr(h.get("State", ""))
        matched_keys.add((normalise_name(h.get("School Name", "")), h_state_abbr))

    unmatched = []
    for f in fsa_records:
        if not f["closed_year"].isdigit():
            continue
        yr = int(f["closed_year"])
        if not (hechinger_year_min - year_window <= yr <= hechinger_year_max + year_window):
            continue
        norm = normalise_name(f["school_name"])
        st = f["state"].upper()
        # Check if any Hechinger row closely matches this FSA record
        found = False
        for hk_norm, hk_st in matched_keys:
            if hk_st != st:
                continue
            ta = frozenset(t for t in norm.split() if t not in STOPWORDS and len(t) > 1)
            tb = frozenset(t for t in hk_norm.split() if t not in STOPWORDS and len(t) > 1)
            if ta and tb and len(ta & tb) / len(ta | tb) >= 0.5:
                found = True
                break
        if not found:
            unmatched.append(f)
    return unmatched


# ---------------------------------------------------------------------------
# State name → abbreviation map
# ---------------------------------------------------------------------------

_STATE_MAP = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC",
    "washington d.c.": "DC", "washington, d.c.": "DC", "d.c.": "DC",
    "puerto rico": "PR", "guam": "GU", "virgin islands": "VI",
    "american samoa": "AS", "northern mariana islands": "MP",
}


def _state_name_to_abbr(state: str) -> str:
    s = state.strip()
    if len(s) == 2:
        return s.upper()
    return _STATE_MAP.get(s.lower(), s.upper()[:2])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_reconciliation(
    baseline_csv: Path,
    fsa_zip: Path,
    out_dir: Path,
    snapshot_date: str,
) -> dict:
    hechinger = load_hechinger(baseline_csv)
    fsa = stream_fsa_records(fsa_zip)

    recon = match_hechinger_to_fsa(hechinger, fsa)

    h_years = [int(r["hechinger_year"]) for r in recon if r["hechinger_year"].isdigit()]
    yr_min = min(h_years) if h_years else 2008
    yr_max = max(h_years) if h_years else 2024

    fsa_only = _fsa_only_records(fsa, hechinger, yr_min, yr_max)

    # Count match statuses
    status_counts: dict[str, int] = {}
    for r in recon:
        s = r["match_status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    # Write reconciliation CSV
    out_dir.mkdir(parents=True, exist_ok=True)
    recon_path = out_dir / f"hechinger_reconciliation_{snapshot_date}.csv"
    recon_fields = [
        "hechinger_name", "hechinger_state", "hechinger_state_abbr",
        "hechinger_year", "hechinger_opeid6", "hechinger_program_length",
        "hechinger_school_type",
        "fsa_opeid", "fsa_school_name", "fsa_state",
        "fsa_closed_date", "fsa_closed_year",
        "match_score", "match_status",
    ]
    with recon_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=recon_fields)
        writer.writeheader()
        writer.writerows(recon)

    # Write FSA-only potential gaps
    fsa_only_path = out_dir / f"hechinger_fsa_only_{snapshot_date}.csv"
    fsa_fields = ["opeid", "school_name", "state", "closed_date", "closed_year"]
    with fsa_only_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fsa_fields)
        writer.writeheader()
        writer.writerows(fsa_only)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": snapshot_date,
        "baseline_csv": str(baseline_csv),
        "fsa_zip": str(fsa_zip),
        "hechinger_row_count": len(hechinger),
        "fsa_total_domestic": len(fsa),
        "hechinger_year_range": f"{yr_min}–{yr_max}",
        "match_status_counts": status_counts,
        "fsa_only_in_year_range": len(fsa_only),
        "reconciliation_csv": str(recon_path),
        "fsa_only_csv": str(fsa_only_path),
        "notes": (
            "match_status values: matched_opeid6 (exact OPEID6 join — most reliable), "
            "matched_exact (Jaccard ≥ 0.9), "
            "matched_fuzzy (0.5 ≤ Jaccard < 0.9), no_fsa_match (< 0.5 or no candidate). "
            f"Year window for name-matching fallback: ±{2} years. "
            "fsa_only_* rows are US domestic FSA records in the Hechinger year range "
            "with no Hechinger match — potential gaps in the tracker requiring review."
        ),
    }
    summary_path = out_dir / f"hechinger_reconciliation_{snapshot_date}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline", type=Path, default=None,
        help="Path to frozen Hechinger baseline CSV",
    )
    parser.add_argument(
        "--fsa-zip", type=Path, default=None,
        help="Path to FSA closed-school ZIP",
    )
    parser.add_argument(
        "--date", default=date.today().isoformat(),
        help="Snapshot date label YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=DERIVED_DIR,
        help="Output directory (default: data/derived/)",
    )
    args = parser.parse_args(argv)

    baseline = args.baseline
    if baseline is None:
        baseline = find_latest_baseline(RAW_HECHINGER)
    if baseline is None:
        print("ERROR: No frozen Hechinger baseline found. "
              "Run freeze_hechinger_baseline.py first.", file=sys.stderr)
        return 1
    if not baseline.exists():
        print(f"ERROR: Baseline not found: {baseline}", file=sys.stderr)
        return 1

    fsa_zip = args.fsa_zip
    if fsa_zip is None:
        fsa_zip = find_latest_fsa_zip(RAW_FSA)
    if fsa_zip is None:
        print("ERROR: No FSA closed-school ZIP found under data/raw/weekly_closed_school_search/.",
              file=sys.stderr)
        return 1
    if not fsa_zip.exists():
        print(f"ERROR: FSA ZIP not found: {fsa_zip}", file=sys.stderr)
        return 1

    try:
        summary = build_reconciliation(baseline, fsa_zip, args.out_dir, args.date)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    counts = summary["match_status_counts"]
    no_match = counts.get("no_fsa_match", 0)
    print(f"Hechinger rows: {summary['hechinger_row_count']}")
    print(f"  matched_opeid6: {counts.get('matched_opeid6', 0)}")
    print(f"  matched_exact : {counts.get('matched_exact', 0)}")
    print(f"  matched_fuzzy : {counts.get('matched_fuzzy', 0)}")
    print(f"  no_fsa_match  : {no_match}")
    print(f"FSA-only (year range, unmatched): {summary['fsa_only_in_year_range']}")
    print(f"Reconciliation: {summary['reconciliation_csv']}")
    print(f"FSA-only gaps : {summary['fsa_only_csv']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
