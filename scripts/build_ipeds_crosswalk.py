"""
Phase 7: Build the historical IPEDS crosswalk and enrich FSA closure records.

Step 1 — Raw crosswalk: load IPEDS year-cache CSVs (2014-2025),
  deduplicate, write data/derived/ipeds_opeid_unitid_by_year.csv.

Step 2 — Enrichment: for each FSA closed-school record, find the UNITID
  from the last IPEDS year before the closure and attach ICLEVEL, CONTROL,
  SECTOR, and enrollment.  Ambiguous or unmatched records go to a review
  queue CSV.

Matching priority (per HANDOFF: exact historical OPEID8 preferred):
  1. Exact OPEID8 match — tries years Y-1, Y-2, … up to MAX_LOOKBACK
     years back or MIN_YEAR, whichever is later
  2. OPEID6 match producing exactly one distinct UNITID in that window
  3. Everything else — written to the review queue

Review queue entries:
  review_opeid8_multi_unitid : OPEID8 found but >1 UNITID in that year
  review_opeid6_ambiguous    : OPEID6 found but >1 UNITID
  no_match                   : in-window, no hit

Coverage note: year cache is 2014-2025 only.  Closures in 2014 or earlier
receive match_method "no_ipeds_coverage" and are NOT added to the review
queue; they are an expected gap.

Usage (from project root):
  python scripts/build_ipeds_crosswalk.py
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
YEAR_CACHE_DIR = (
    Path(r"C:\Users\mv3031\Desktop\Financial_Health_Project")
    / "Financial Health Tracker - For Github"
    / "ipeds" / "cache" / "year_cache"
)
DERIVED = PROJECT / "data" / "derived"

MISSING_CODES: frozenset[str] = frozenset({"-2", "-3", ""})
MIN_YEAR  = 2014   # earliest year in our cache
MAX_LOOKBACK = 5   # years before closure to search

LEVEL_LABELS         = {"1": "4yr+", "2": "2-4yr", "3": "<2yr"}
PROGRAM_LEVEL_LABELS = {"1": "4-year or above", "2": "2-year", "3": "Less than 2-year"}
CONTROL_LABELS = {"1": "public", "2": "nonprofit", "3": "for-profit"}

# IPEDS sector codes → human-readable category (Burchart / SHEEO distinction)
SECTOR_LABELS = {
    "1": "public_4yr",
    "2": "private_nonprofit_4yr",
    "3": "private_nonprofit_2yr",
    "4": "public_2yr",
    "5": "public_lt2yr",
    "6": "for_profit_4yr",
    "7": "for_profit_2yr",
    "8": "for_profit_lt2yr",
    "9": "unclassified",
    "0": "unclassified",
}

# Traditional college = public or nonprofit, 4-year or 2-year (not vocational/lt2yr).
# Sector codes 1-4 per SHEEO / Burchart "traditional college" definition.
TRADITIONAL_SECTORS = frozenset({"1", "2", "3", "4"})

_REVIEW_METHODS = frozenset({
    "review_opeid8_multi_unitid",
    "review_opeid6_ambiguous",
    "no_match",
})


# ---------------------------------------------------------------------------
# Step 1 — Raw crosswalk
# ---------------------------------------------------------------------------

CROSSWALK_FIELDS = [
    "opeid8", "opeid6", "unitid", "year",
    "institution_name", "city", "state",
    "level", "control", "sector",
    "enrollment_headcount_total",
    "size",
    "highest_degree",
]

# IPEDS HDEGOFR1 / highest degree offered codes
CREDENTIAL_LEVEL_LABELS: dict[str, str] = {
    "0":  "Non-degree",
    "11": "Certificate",
    "12": "Certificate",
    "13": "Associate's",
    "14": "Associate's",
    "20": "Bachelor's",
    "30": "Master's",
    "40": "Doctoral",
}

# IPEDS institutional size categories — official federal classification
# Source: IPEDS Data Dictionary, variable INSTSIZE / SIZE
SIZE_LABELS = {
    "1": "Under 1,000",
    "2": "1,000–4,999",
    "3": "5,000–9,999",
    "4": "10,000–19,999",
    "5": "20,000 and above",
}


def _is_valid_opeid8(opeid: str) -> bool:
    return bool(opeid) and opeid not in MISSING_CODES and len(opeid) == 8 and opeid.isdigit()


def load_year_cache(cache_dir: Path) -> list[dict]:
    """Load all year-cache CSVs; return rows with valid OPEID8 and UNITID."""
    rows: list[dict] = []
    for f in sorted(cache_dir.glob("ipeds_financial_health_year_*.csv")):
        with f.open(encoding="utf-8-sig", newline="") as fh:
            for raw in csv.DictReader(fh):
                opeid  = (raw.get("opeid")  or "").strip()
                unitid = (raw.get("unitid") or "").strip()
                year   = (raw.get("year")   or "").strip()
                if not _is_valid_opeid8(opeid):
                    continue
                if not unitid or unitid in MISSING_CODES:
                    continue
                if not year.isdigit():
                    continue
                rows.append({
                    "opeid8":                    opeid,
                    "opeid6":                    opeid[:6],
                    "unitid":                    unitid,
                    "year":                      year,
                    "institution_name":          (raw.get("institution_name")          or "").strip(),
                    "city":                      (raw.get("city")                      or "").strip(),
                    "state":                     (raw.get("state")                     or "").strip(),
                    "level":                     (raw.get("level")                     or "").strip(),
                    "control":                   (raw.get("control")                   or "").strip(),
                    "sector":                    (raw.get("sector")                    or "").strip(),
                    "enrollment_headcount_total": (raw.get("enrollment_headcount_total") or "").strip(),
                    "size":                      (raw.get("size")                      or "").strip(),
                    "highest_degree":            (raw.get("highest_degree")            or "").strip(),
                })
    return rows


def build_opeid8_index(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """
    Return {(opeid8, year): row}, first occurrence per key.
    Sets row["_multi_unitid"] = True when >1 UNITID maps to the same (opeid8, year).
    """
    index: dict[tuple[str, str], dict] = {}
    multi: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        key = (row["opeid8"], row["year"])
        multi[key].add(row["unitid"])
        if key not in index:
            index[key] = row
    for key, unitids in multi.items():
        if len(unitids) > 1:
            index[key]["_multi_unitid"] = True
    return index


def build_opeid6_rows_index(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """Return {(opeid6, year): [row, …]} for OPEID6 fallback lookups."""
    index: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        index[(row["opeid6"], row["year"])].append(row)
    return dict(index)


def write_crosswalk_csv(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CROSSWALK_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Step 2 — Enrich FSA closed-school records
# ---------------------------------------------------------------------------

ENRICHED_FIELDS = [
    "source_date", "source_file",
    "opeid8", "opeid6", "location_suffix",
    # Derived from OPEID suffix — the Fed paper finds 90 % of PEPS closures are branches
    "is_main_campus",
    "school_name", "location_name", "is_suspicious_location", "city", "state", "country",
    "closed_date", "closed_date_iso", "closed_year",
    "unitid", "ipeds_institution_name", "ipeds_state",
    "iclevel", "iclevel_label", "control", "control_label", "sector",
    # Human-readable sector label (SHEEO / Burchart "traditional college" distinction)
    "institution_category",
    "is_traditional_college",
    "program_level",
    "credential_level",
    "enrollment_headcount_total",
    # IPEDS official size category (INSTSIZE): 1=Under 1,000; 2=1,000-4,999; 3=5,000-9,999; 4=10,000-19,999; 5=20,000+
    "ipeds_size_code",
    "enrollment_band",
    "ipeds_year",
    "match_method",
    # SHEEO three counting approaches (Measuring College Closures with Federal Education Data, 2024)
    "approach1_all_campuses",        # always true  — each OPEID8 = one closure
    "approach2_main_campus_only",    # true if is_main_campus — full institution closures only
    "approach3_system_level",        # always true  — but count deduplicated by OPEID6 for totals
]

REVIEW_FIELDS = [
    "opeid8", "opeid6", "location_suffix",
    "school_name", "state",
    "closed_date_iso", "closed_year",
    "match_method", "review_reason",
    "opeid6_candidate_unitids",
]


def _closed_year(iso_date: str) -> int | None:
    if iso_date and len(iso_date) >= 4 and iso_date[:4].isdigit():
        return int(iso_date[:4])
    return None


def _enrollment_band(size_code: str) -> str:
    """Map IPEDS size code (1–5) to the official federal size label."""
    return SIZE_LABELS.get(size_code.strip(), "")


def _credential_level(iclevel: str, highest_degree: str) -> str:
    """Derive credential level using iclevel as primary source.

    iclevel is reliably matched per record; highest_degree can reflect the
    parent institution's offering and is only used to distinguish within the
    4-year tier (bachelor's / master's / doctoral).
    """
    if iclevel == "2":
        return "Associate's"
    if iclevel == "3":
        return "Non-degree" if highest_degree == "0" else "Certificate"
    if iclevel == "1":
        return {"20": "Bachelor's", "30": "Master's", "40": "Doctoral"}.get(
            highest_degree, "Bachelor's or above"
        )
    return ""


def _empty_ipeds() -> dict:
    return {
        "unitid": "", "ipeds_institution_name": "", "ipeds_state": "",
        "iclevel": "", "iclevel_label": "", "control": "", "control_label": "",
        "sector": "", "institution_category": "", "is_traditional_college": "",
        "program_level": "", "credential_level": "",
        "enrollment_headcount_total": "", "ipeds_size_code": "", "enrollment_band": "",
        "ipeds_year": "",
    }


def _ipeds_from_row(row: dict) -> dict:
    level      = row.get("level", "")
    ctrl       = row.get("control", "")
    sector     = row.get("sector", "")
    enroll     = row.get("enrollment_headcount_total", "")
    size_code  = row.get("size", "")
    return {
        "unitid":                    row.get("unitid", ""),
        "ipeds_institution_name":    row.get("institution_name", ""),
        "ipeds_state":               row.get("state", ""),
        "iclevel":                   level,
        "iclevel_label":             LEVEL_LABELS.get(level, ""),
        "control":                   ctrl,
        "control_label":             CONTROL_LABELS.get(ctrl, ""),
        "sector":                    sector,
        "institution_category":      SECTOR_LABELS.get(sector, ""),
        "is_traditional_college":    "true" if sector in TRADITIONAL_SECTORS else "false",
        "program_level":             PROGRAM_LEVEL_LABELS.get(level, ""),
        "credential_level":          _credential_level(level, row.get("highest_degree", "").strip()),
        "enrollment_headcount_total": enroll,
        "ipeds_size_code":           size_code,
        "enrollment_band":           _enrollment_band(size_code),
        "ipeds_year":                row.get("year", ""),
    }


def enrich_closure_records(
    closure_records: list[dict],
    opeid8_idx: dict[tuple[str, str], dict],
    opeid6_rows_idx: dict[tuple[str, str], list[dict]],
) -> tuple[list[dict], list[dict]]:
    """
    For each FSA closure record, find the best IPEDS match.
    Returns (enriched_rows, review_rows).
    """
    enriched: list[dict] = []
    review:   list[dict] = []

    for rec in closure_records:
        opeid8    = rec.get("opeid8", "")
        opeid6    = rec.get("opeid6", "")
        iso_date  = rec.get("closed_date_iso", "")
        closed_yr = _closed_year(iso_date)

        location_suffix = rec.get("location_suffix", "")
        is_main = location_suffix == "00"

        base: dict = {
            "source_date":     rec.get("source_date", ""),
            "source_file":     rec.get("source_file", ""),
            "opeid8":          opeid8,
            "opeid6":          opeid6,
            "location_suffix": location_suffix,
            "is_main_campus":  "true" if is_main else "false",
            "school_name":           rec.get("school_name", ""),
            "location_name":         rec.get("location_name", ""),
            "is_suspicious_location": rec.get("is_suspicious_location", ""),
            "city":                  rec.get("city", ""),
            "state":           rec.get("state", ""),
            "country":         rec.get("country", ""),
            "closed_date":     rec.get("closed_date", ""),
            "closed_date_iso": iso_date,
            "closed_year":     str(closed_yr) if closed_yr is not None else "",
        }

        match: dict | None = None
        method               = "no_match"
        review_reason        = ""
        opeid6_candidates_str = ""

        if closed_yr is None:
            method = "no_closure_date"
        elif closed_yr <= MIN_YEAR:
            # Closure in 2014 or earlier: the year-before would be 2013,
            # which is not in our cache.  Expected gap, not a data quality flag.
            method = "no_ipeds_coverage"
        else:
            search_start = min(closed_yr - 1, 2025)
            search_end   = max(closed_yr - MAX_LOOKBACK, MIN_YEAR)

            # Pass 1: exact OPEID8 match (most reliable per HANDOFF)
            for yr in range(search_start, search_end - 1, -1):
                row = opeid8_idx.get((opeid8, str(yr)))
                if row is None:
                    continue
                if row.get("_multi_unitid"):
                    method = "review_opeid8_multi_unitid"
                    review_reason = f"OPEID8 {opeid8} maps to multiple UNITIDs in {yr}"
                else:
                    match  = row
                    method = f"matched_opeid8_yr{yr}"
                break

            # Pass 2: OPEID6 fallback if still unmatched
            if match is None and method == "no_match" and opeid6 and opeid6 != "000000":
                for yr in range(search_start, search_end - 1, -1):
                    yr_rows = opeid6_rows_idx.get((opeid6, str(yr)), [])
                    if not yr_rows:
                        continue
                    unitids = {r["unitid"] for r in yr_rows}
                    if len(unitids) == 1:
                        match  = yr_rows[0]
                        method = f"matched_opeid6_single_yr{yr}"
                    else:
                        method = "review_opeid6_ambiguous"
                        review_reason = (
                            f"OPEID6 {opeid6} maps to {len(unitids)} UNITIDs "
                            f"in {yr}: " + "; ".join(sorted(unitids)[:10])
                        )
                        opeid6_candidates_str = "|".join(sorted(unitids))
                    break

        # Build enriched row (all records appear here)
        enriched.append({
            **base,
            **(_ipeds_from_row(match) if match else _empty_ipeds()),
            "match_method":           method,
            # SHEEO three counting approaches (declared per row; totals in summary JSON)
            "approach1_all_campuses":     "true",
            "approach2_main_campus_only": "true" if is_main else "false",
            "approach3_system_level":     "true",
        })

        # Review queue (only actionable mismatches)
        if method in _REVIEW_METHODS:
            review.append({
                "opeid8":                   opeid8,
                "opeid6":                   opeid6,
                "location_suffix":          rec.get("location_suffix", ""),
                "school_name":              rec.get("school_name", ""),
                "state":                    rec.get("state", ""),
                "closed_date_iso":          iso_date,
                "closed_year":              str(closed_yr) if closed_yr is not None else "",
                "match_method":             method,
                "review_reason":            review_reason or method,
                "opeid6_candidate_unitids": opeid6_candidates_str,
            })

    return enriched, review


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _latest_closed_csv(derived_dir: Path) -> Path | None:
    files = sorted(derived_dir.glob("fsa_closed_school_*.csv"), reverse=True)
    return files[0] if files else None


def _load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_csv(records: list[dict], fields: list[str], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


# ---------------------------------------------------------------------------
# Publication table
# ---------------------------------------------------------------------------

PUBLICATION_TABLE_FIELDS = [
    "closed_date_iso",
    "school_name",
    "city", "state",
    "institution_category",
    "control_label",
    "iclevel_label",
    "enrollment_band",
    "is_main_campus",
    "is_suspicious_location",
    "match_method",
    "opeid8",
    "unitid",
    "review_flag",
]

# match_methods that mean no IPEDS record was attached
_NO_IPEDS_METHODS = frozenset({
    "review_opeid8_multi_unitid",
    "review_opeid6_ambiguous",
    "no_match",
})


def write_publication_table(
    enriched: list[dict], out_path: Path, min_year: int = 2015
) -> list[dict]:
    """
    Filter enriched records to publication-ready rows and write a CSV.

    Inclusion criteria (approach 2 default view):
      - approach2_main_campus_only == "true"  (last/only campus of its system)
      - closed_year >= min_year              (IPEDS coverage floor)

    Rows with no parseable closure date fail the year filter naturally.

    Adds a `review_flag` column for the editor:
      suspicious_location  — location_name matched a SHEEO sub-unit pattern
      no_ipeds_match       — in-window but no IPEDS record attached
      (pipe-separated if both apply; empty string if clean)

    Returns the filtered rows (before writing) so callers can inspect them.
    """
    rows: list[dict] = []
    for r in enriched:
        yr_str = r.get("closed_year", "")
        if not yr_str.isdigit():
            continue
        if int(yr_str) < min_year:
            continue
        if r.get("approach2_main_campus_only") != "true":
            continue

        flags: list[str] = []
        if r.get("is_suspicious_location") == "true":
            flags.append("suspicious_location")
        if r.get("match_method") in _NO_IPEDS_METHODS:
            flags.append("no_ipeds_match")

        rows.append({**r, "review_flag": "|".join(flags)})

    # Sort by close date descending (ISO dates sort correctly as strings)
    rows.sort(key=lambda r: r.get("closed_date_iso", ""), reverse=True)

    _write_csv(rows, PUBLICATION_TABLE_FIELDS, out_path)
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build IPEDS crosswalk and enrich FSA closures.")
    parser.add_argument("--year-cache-dir", type=Path, default=YEAR_CACHE_DIR)
    parser.add_argument(
        "--closure-csv", type=Path, default=None,
        help="FSA closed-school CSV (default: latest fsa_closed_school_*.csv in --out-dir)",
    )
    parser.add_argument("--out-dir", type=Path, default=DERIVED)
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args(argv)

    if not args.year_cache_dir.exists():
        print(f"ERROR: Year-cache directory not found:\n  {args.year_cache_dir}", file=sys.stderr)
        return 1

    # --- Step 1: Build indexes ---
    print("Loading IPEDS year-cache files...")
    cache_rows = load_year_cache(args.year_cache_dir)
    if not cache_rows:
        print("ERROR: No valid rows loaded from year cache.", file=sys.stderr)
        return 1
    years_found = sorted({r["year"] for r in cache_rows})
    print(f"  {len(cache_rows):,} rows  ({years_found[0]}-{years_found[-1]})")

    crosswalk_path = args.out_dir / "ipeds_opeid_unitid_by_year.csv"
    write_crosswalk_csv(cache_rows, crosswalk_path)
    print(f"  Raw crosswalk -> {crosswalk_path}")

    opeid8_idx      = build_opeid8_index(cache_rows)
    opeid6_rows_idx = build_opeid6_rows_index(cache_rows)
    multi_count = sum(1 for row in opeid8_idx.values() if row.get("_multi_unitid"))
    print(f"  {len(opeid8_idx):,} unique (opeid8, year) keys; {multi_count} ambiguous")

    # --- Step 2: Enrich ---
    closure_csv = args.closure_csv or _latest_closed_csv(args.out_dir)
    if closure_csv is None or not closure_csv.exists():
        print(
            "ERROR: No FSA closed-school CSV found.\n"
            "       Run parse_federal_sources.py first.",
            file=sys.stderr,
        )
        return 1

    print(f"\nLoading FSA closure records from {closure_csv.name} ...")
    closures = _load_csv(closure_csv)
    print(f"  {len(closures):,} records")

    enriched, review = enrich_closure_records(closures, opeid8_idx, opeid6_rows_idx)

    enriched_path = args.out_dir / f"fsa_closure_ipeds_enriched_{args.date}.csv"
    review_path   = args.out_dir / f"ipeds_crosswalk_review_queue_{args.date}.csv"
    pub_path      = args.out_dir / f"fsa_closure_publication_table_{args.date}.csv"
    _write_csv(enriched, ENRICHED_FIELDS, enriched_path)
    _write_csv(review,   REVIEW_FIELDS,   review_path)
    pub_rows = write_publication_table(enriched, pub_path)

    method_counts = Counter(r["match_method"] for r in enriched)
    matched  = sum(v for k, v in method_counts.items() if k.startswith("matched_"))
    no_cov   = method_counts.get("no_ipeds_coverage", 0)
    no_date  = method_counts.get("no_closure_date", 0)
    no_match = method_counts.get("no_match", 0)

    # SHEEO three counting approaches
    approach1 = len(enriched)
    approach2 = sum(1 for r in enriched if r.get("approach2_main_campus_only") == "true")
    approach3 = len({r["opeid6"] for r in enriched if r.get("opeid6")})

    # State breakdown (approach 2 — institutional closures only)
    state_counts = Counter(
        r["state"] for r in enriched
        if r.get("approach2_main_campus_only") == "true" and r.get("state")
    )

    # Control breakdown across all records
    control_counts = Counter(r.get("control_label") or "unknown" for r in enriched)

    # Enrollment bands (where IPEDS data available)
    band_counts = Counter(
        r["enrollment_band"] for r in enriched if r.get("enrollment_band")
    )

    summary = {
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "methodology_version":   "1.0",
        "ipeds_years_loaded":    years_found,
        "crosswalk_rows":        len(cache_rows),
        "closure_records_input": len(closures),
        "matched":               matched,
        "no_ipeds_coverage":     no_cov,
        "no_closure_date":       no_date,
        "no_match":              no_match,
        "review_queue_rows":     len(review),
        "method_counts":         dict(sorted(method_counts.items())),
        # SHEEO three counting approaches (Burns 2024, "Measuring College Closures with Federal Education Data")
        "sheeo_counts": {
            "approach1_all_campuses":     approach1,
            "approach2_main_campus_only": approach2,
            "approach3_system_level":     approach3,
            "note_approach1": "Each individual 8-digit OPEID closure = one event (most inclusive)",
            "note_approach2": (
                "Only main campus (OPEID suffix '00') closures = full institution gone. "
                "Fed Reserve (Kelchen et al. 2025): 90 pct of PEPS records are branch campuses."
            ),
            "note_approach3": "Distinct OPEID6 systems with any campus closure; each system counted once",
        },
        # Geographic and institutional breakdowns
        "state_counts_approach2":   dict(state_counts.most_common()),
        "control_counts_all":       dict(control_counts.most_common()),
        "enrollment_band_counts":   dict(band_counts.most_common()),
    }
    summary_path = args.out_dir / f"ipeds_crosswalk_summary_{args.date}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\nEnrichment results ({len(closures):,} records):")
    print(f"  matched (opeid8 / opeid6): {matched:,}")
    print(f"  no IPEDS coverage (<2015): {no_cov:,}")
    print(f"  no closure date          : {no_date:,}")
    print(f"  no match (in-window)     : {no_match:,}")
    print(f"  review queue rows        : {len(review):,}")
    pub_flagged = sum(1 for r in pub_rows if r.get("review_flag"))

    print(f"\nOutputs written:")
    print(f"  {crosswalk_path}")
    print(f"  {enriched_path}")
    print(f"  {review_path}")
    print(f"  {pub_path}  ({len(pub_rows):,} rows; {pub_flagged} flagged for review)")
    print(f"  {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
