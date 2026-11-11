#!/usr/bin/env python3
"""Push closure events to the Hechinger review Google Sheet.

Usage
-----
  # Weekly refresh — writes to closure_list tab, counts new closures:
  python scripts/push_to_gsheet.py --sheet SHEET_ID

  # Editorial publish — writes reviewed output to published tab:
  python scripts/push_to_gsheet.py --sheet SHEET_ID --tab published

Credentials
-----------
  Uses Application Default Credentials (ADC).  In GitHub Actions the
  google-github-actions/auth step sets credentials automatically via
  Workload Identity Federation.  Locally run:
    gcloud auth application-default login
  The service account must be an Editor on the target Sheet.

Outputs
-------
  data/derived/new_closures_count.txt — count of opeid8s not previously in
  the closure_list tab (always 0 when --tab published is used).

Published-tab filtering contract
---------------------------------
  institution rows: approach2_main_campus_only="true" AND closure_type in
      {institutional_closure, system_consolidation, for_profit_closure}.
      Published closure_type normalised to "institution_closure".

  campus rows: closure_type="branch_closure" AND IPEDS match confirmed AND
      iclevel in {1,2,3} AND parent OPEID6's main campus NOT in this dataset.
      Published closure_type normalised to "campus_closure".

  Excluded: location_administrative, unclassified, merger_absorption, and any
      branch whose parent institution also fully closed in this dataset.

  merger_absorption is excluded by default.  RT11 record-holder evidence
  flags a possible merger but does not prove cessation of operations.
  Set review_required=true and reclassify via editorial override to include.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import google.auth
import gspread
from google.auth.transport.requests import Request as _AuthRequest

PROJECT = Path(__file__).resolve().parent.parent
DERIVED = PROJECT / "data" / "derived"
NEW_COUNT_FILE = DERIVED / "new_closures_count.txt"

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

# Internal review tab: editors see all classification context.
_EDITORIAL_COLS = [
    "opeid8", "school_name", "city", "state",
    "control_label", "iclevel_label", "institution_category",
    "program_level", "credential_level",
    "enrollment_band", "enrollment_headcount_total",
    "closed_date_iso", "closed_year",
    "is_main_campus", "is_suspicious_location",
    "approach2_main_campus_only", "is_traditional_college",
    "closure_type", "closure_type_basis",
    "match_method", "unitid", "opeid6",
]

# Public published tab: Datawrapper and consumers read this.
# 12 original columns (program_level retained; iclevel_label stays in closure_list only)
# + 11 new publication-metadata columns.
_PUBLIC_COLS = [
    "opeid8", "school_name", "city", "state",
    "control_label", "program_level", "credential_level",
    "enrollment_band", "enrollment_headcount_total",
    "closed_date_iso", "closed_year", "closure_type",
    "opeid6", "unitid",
    "closure_scope", "parent_institution_continued",
    "include_institution_totals", "include_campus_totals",
    "sheeo_approach1_include", "sheeo_approach2_include", "sheeo_approach3_include",
    "classification_basis", "closure_event_id",
]

TAB_COLUMNS: dict[str, list[str]] = {
    "closure_list": _EDITORIAL_COLS,
    "published":    _PUBLIC_COLS,
}

# ---------------------------------------------------------------------------
# Publication filters
# ---------------------------------------------------------------------------

# Internal classifier types that map to institution-scope published rows.
_INSTITUTION_TYPES = frozenset({
    "institutional_closure",
    "system_consolidation",
    "for_profit_closure",
})

# Internal classifier types that may become campus-scope published rows.
_CAMPUS_TYPES = frozenset({"branch_closure"})

# Types excluded from published entirely.
# merger_absorption: RT11 evidence flags a candidate but does not prove
# cessation of operations; requires editorial override to publish.
_EXCLUDE_FROM_PUBLISHED = frozenset({
    "location_administrative",
    "unclassified",
    "merger_absorption",
})

# Types excluded from closure_list (non-campus administrative sites).
_EXCLUDE_FROM_CLOSURE_LIST = frozenset({"location_administrative"})

# match_method prefixes that confirm a resolved IPEDS identity.
_MATCHED_PREFIXES = ("matched_opeid8_", "matched_opeid6_")


def _is_matched(match_method: str) -> bool:
    return any(match_method.startswith(p) for p in _MATCHED_PREFIXES)


# ---------------------------------------------------------------------------
# Credentials / client
# ---------------------------------------------------------------------------

_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


def _get_gspread_client() -> gspread.Client:
    creds, _ = google.auth.default(scopes=_SCOPES)
    if not creds.valid:
        creds.refresh(_AuthRequest())
    return gspread.Client(auth=creds)


def _latest_events_csv() -> Path:
    candidates = sorted(DERIVED.glob("fsa_closure_events_*.csv"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No fsa_closure_events_*.csv found in {DERIVED}")
    return candidates[0]


# ---------------------------------------------------------------------------
# Row loaders
# ---------------------------------------------------------------------------

def _load_rows(
    path: Path,
    columns: list[str],
    min_year: int | None = None,
    exclude_types: frozenset[str] | None = None,
) -> list[list[str]]:
    """Return rows as list-of-lists (header first), sorted newest-closed first."""
    with path.open(encoding="utf-8", newline="") as fh:
        raw = list(csv.DictReader(fh))
    if min_year is not None:
        raw = [r for r in raw if r.get("closed_year", "0") >= str(min_year)]
    if exclude_types:
        raw = [r for r in raw if r.get("closure_type", "") not in exclude_types]
    raw.sort(key=lambda r: r.get("closed_date_iso", ""), reverse=True)
    result: list[list[str]] = [columns]
    for row in raw:
        result.append([row.get(c, "") for c in columns])
    return result


def _filter_published_rows(raw: list[dict], min_year: int = 2000) -> list[dict]:
    """
    Pure function: apply publication filters and derive all published fields.

    Returns a list of dicts with every key in _PUBLIC_COLS populated.
    Testable without touching the filesystem.

    Gate for campus rows:
      1. closure_type = "branch_closure"
      2. IPEDS match confirmed (match_method starts with "matched_")
      3. iclevel in {"1","2","3"} — postsecondary identity confirmed
      4. OPEID6's main campus NOT also in this closure dataset — parent continued
    """
    raw = [r for r in raw if r.get("closed_year", "0") >= str(min_year)]

    # Build set of OPEID6s whose institution-level (approach2) campus also closed.
    closed_main_opeid6s: set[str] = set()
    for r in raw:
        if (r.get("approach2_main_campus_only") == "true"
                and r.get("closure_type", "") in _INSTITUTION_TYPES):
            opeid6 = r.get("opeid6", "")
            if opeid6:
                closed_main_opeid6s.add(opeid6)

    pub: list[dict] = []
    for r in raw:
        ctype = r.get("closure_type", "")
        opeid8 = r.get("opeid8", "")
        opeid6 = r.get("opeid6", "")
        closed_date = r.get("closed_date_iso", "")

        if ctype in _EXCLUDE_FROM_PUBLISHED:
            continue

        if r.get("approach2_main_campus_only") == "true" and ctype in _INSTITUTION_TYPES:
            # Full-institution or system-level closure.
            scope = "institution"
            published_ctype = "institution_closure"
            parent_continued = "false"
            incl_inst = "true"
            incl_campus = "false"
            approach2 = "true"
            event_id = f"institution:{opeid6}:{closed_date}"

        elif ctype in _CAMPUS_TYPES:
            # Branch campus — apply all four gates.
            if not _is_matched(r.get("match_method", "")):
                continue  # No confirmed IPEDS identity
            if r.get("iclevel", "") not in {"1", "2", "3"}:
                continue  # No confirmed postsecondary level
            if opeid6 in closed_main_opeid6s:
                continue  # Parent institution also closed — not a standalone campus event
            scope = "campus"
            published_ctype = "campus_closure"
            parent_continued = "true"
            incl_inst = "false"
            incl_campus = "true"
            approach2 = "false"
            event_id = f"campus:{opeid8}:{closed_date}"

        else:
            continue  # main campus with excluded or unrecognised type

        pub.append({
            "opeid8":                     opeid8,
            "school_name":                r.get("school_name", ""),
            "city":                       r.get("city", ""),
            "state":                      r.get("state", ""),
            "control_label":              r.get("control_label", ""),
            "program_level":              r.get("program_level", ""),
            "credential_level":           r.get("credential_level", ""),
            "enrollment_band":            r.get("enrollment_band", ""),
            "enrollment_headcount_total": r.get("enrollment_headcount_total", ""),
            "closed_date_iso":            closed_date,
            "closed_year":                r.get("closed_year", ""),
            "closure_type":               published_ctype,
            "opeid6":                     opeid6,
            "unitid":                     r.get("unitid", ""),
            "closure_scope":              scope,
            "parent_institution_continued": parent_continued,
            "include_institution_totals": incl_inst,
            "include_campus_totals":      incl_campus,
            "sheeo_approach1_include":    "true",
            "sheeo_approach2_include":    approach2,
            "sheeo_approach3_include":    "true",
            "classification_basis":       r.get("closure_type_basis", ""),
            "closure_event_id":           event_id,
        })

    pub.sort(key=lambda r: r.get("closed_date_iso", ""), reverse=True)
    return pub


def _build_published_dicts(path: Path, min_year: int = 2000) -> list[dict]:
    """Load events CSV and return publication-ready records as list of dicts."""
    with path.open(encoding="utf-8", newline="") as fh:
        raw = list(csv.DictReader(fh))
    return _filter_published_rows(raw, min_year)


def _pub_dicts_to_rows(pub: list[dict]) -> list[list]:
    """Convert published dicts to list-of-lists for gspread (header first)."""
    return [_PUBLIC_COLS] + [[d[c] for c in _PUBLIC_COLS] for d in pub]


# ---------------------------------------------------------------------------
# Aggregation helpers for by_year and by_state tabs
# ---------------------------------------------------------------------------

_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_BY_YEAR_HEADER = [
    "year", "month_num", "month",
    # Backward-compatible institution-only breakdowns:
    "total", "for_profit", "nonprofit", "public",
    "non_degree", "certificate", "associate", "bachelor", "graduate",
    # New scope/reconciliation columns:
    "institution_closures", "campus_closures", "all_published_events",
    "unknown_control", "unknown_credential",
]

_BY_STATE_HEADER = [
    "state",
    # Backward-compatible institution-only breakdowns:
    "total", "for_profit", "nonprofit", "public",
    # New scope/reconciliation columns:
    "institution_closures", "campus_closures", "all_published_events",
    "unknown_control",
]


def _empty_grain() -> dict:
    return {
        "inst": 0, "fp": 0, "np": 0, "pub": 0, "unk_ctrl": 0,
        "nd": 0, "cert": 0, "assoc": 0, "bach": 0, "grad": 0, "unk_cred": 0,
        "campus": 0,
    }


def _bump_grain(d: dict, row: dict) -> None:
    scope = row.get("closure_scope", "")
    ctrl  = row.get("control_label", "")
    cred  = row.get("credential_level", "")
    if scope == "institution":
        d["inst"] += 1
        if   ctrl == "for-profit": d["fp"]  += 1
        elif ctrl == "nonprofit":  d["np"]  += 1
        elif ctrl == "public":     d["pub"] += 1
        else:                      d["unk_ctrl"] += 1
        if   cred == "Non-degree":    d["nd"]   += 1
        elif cred == "Certificate":   d["cert"] += 1
        elif cred == "Associate's":   d["assoc"] += 1
        elif cred == "Bachelor's":    d["bach"] += 1
        elif cred in ("Master's", "Doctoral", "Bachelor's or above"):
            d["grad"] += 1
        else:
            d["unk_cred"] += 1
    elif scope == "campus":
        d["campus"] += 1


def _build_by_year_data(pub: list[dict]) -> list[list]:
    """Build by_year tab rows from published dicts."""
    from collections import defaultdict
    annual:  dict = defaultdict(_empty_grain)
    monthly: dict = defaultdict(_empty_grain)

    for r in pub:
        year = r.get("closed_year", "")
        if not year:
            continue
        date_iso = r.get("closed_date_iso", "")
        mn = int(date_iso[5:7]) if len(date_iso) >= 7 and date_iso[5:7].isdigit() else 0
        _bump_grain(annual[year], r)
        if mn:
            _bump_grain(monthly[(year, mn)], r)

    out: list[list] = [_BY_YEAR_HEADER]
    for yr in sorted(annual):
        d = annual[yr]
        inst, camp = d["inst"], d["campus"]
        out.append([
            yr, 0, "",
            inst, d["fp"], d["np"], d["pub"],
            d["nd"], d["cert"], d["assoc"], d["bach"], d["grad"],
            inst, camp, inst + camp,
            d["unk_ctrl"], d["unk_cred"],
        ])
    for (yr, mn) in sorted(monthly):
        d = monthly[(yr, mn)]
        inst, camp = d["inst"], d["campus"]
        out.append([
            yr, mn, _MONTH_NAMES[mn - 1],
            inst, d["fp"], d["np"], d["pub"],
            d["nd"], d["cert"], d["assoc"], d["bach"], d["grad"],
            inst, camp, inst + camp,
            d["unk_ctrl"], d["unk_cred"],
        ])
    return out


def _build_by_state_data(pub: list[dict]) -> list[list]:
    """Build by_state tab rows from published dicts."""
    from collections import defaultdict

    by_state: dict = defaultdict(_empty_grain)
    for r in pub:
        state = r.get("state", "").strip()
        if not state or len(state) != 2:
            continue
        _bump_grain(by_state[state], r)

    return [_BY_STATE_HEADER] + [
        [
            s,
            d["inst"],           # total = institution_closures (backward compat)
            d["fp"], d["np"], d["pub"],
            d["inst"], d["campus"], d["inst"] + d["campus"],
            d["unk_ctrl"],
        ]
        for s, d in sorted(by_state.items())
    ]


# ---------------------------------------------------------------------------
# Sheet helpers
# ---------------------------------------------------------------------------

def _existing_opeid8s(ws: gspread.Worksheet) -> set[str]:
    try:
        col_a = ws.col_values(1)
        return set(col_a[1:]) - {""}
    except Exception:
        return set()


def _open_or_create_tab(sh: gspread.Spreadsheet, tab: str, ncols: int) -> gspread.Worksheet:
    try:
        return sh.worksheet(tab)
    except gspread.exceptions.WorksheetNotFound:
        print(f"  Tab '{tab}' not found — creating it.")
        return sh.add_worksheet(title=tab, rows=50_000, cols=ncols)


def _write_tab(sh: gspread.Spreadsheet, tab: str, data: list[list]) -> None:
    ws = _open_or_create_tab(sh, tab, len(data[0]) if data else 1)
    ws.clear()
    ws.update("A1", data)
    print(f"  '{tab}' tab written ({len(data) - 1} rows)")


# ---------------------------------------------------------------------------
# Main push function
# ---------------------------------------------------------------------------

def push(sheet_id: str, tab: str) -> int:
    """Write closure events to the named tab. Returns count of new opeid8s."""
    gc = _get_gspread_client()
    sh = gc.open_by_key(sheet_id)

    events_path = _latest_events_csv()
    print(f"Loading {events_path.name} …")

    if tab == "published":
        pub_dicts = _build_published_dicts(events_path)
        data = _pub_dicts_to_rows(pub_dicts)
        nrows = len(data) - 1
        inst_n = sum(1 for d in pub_dicts if d["closure_scope"] == "institution")
        camp_n = sum(1 for d in pub_dicts if d["closure_scope"] == "campus")
        print(f"  {nrows:,} records → '{tab}' tab "
              f"({inst_n:,} institution, {camp_n:,} campus)")
        ws = _open_or_create_tab(sh, tab, len(_PUBLIC_COLS))
        ws.clear()
        ws.update("A1", data)
        print(f"  Written {nrows:,} rows to '{tab}' tab.")
        print("Writing aggregated chart tabs…")
        _write_tab(sh, "by_year",  _build_by_year_data(pub_dicts))
        _write_tab(sh, "by_state", _build_by_state_data(pub_dicts))
        return 0

    elif tab == "closure_list":
        columns = _EDITORIAL_COLS
        data = _load_rows(
            events_path, columns,
            exclude_types=_EXCLUDE_FROM_CLOSURE_LIST,
        )
        nrows = len(data) - 1
        print(f"  {nrows:,} records → '{tab}' tab")
        ws = _open_or_create_tab(sh, tab, len(columns))
        existing = _existing_opeid8s(ws)
        new_opeid8s = {row[0] for row in data[1:] if row[0]} - existing
        new_count = len(new_opeid8s)
        print(f"  {new_count} new opeid8(s) not previously in sheet")
        ws.clear()
        ws.update("A1", data)
        print(f"  Written {nrows:,} rows to '{tab}' tab.")
        return new_count

    else:
        raise ValueError(f"Unknown tab: {tab!r}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sheet", required=True, help="Google Sheet ID (from the URL)")
    p.add_argument(
        "--tab",
        default="closure_list",
        choices=list(TAB_COLUMNS),
        help="Tab to write (default: closure_list)",
    )
    args = p.parse_args()

    try:
        new_count = push(args.sheet, args.tab)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    DERIVED.mkdir(parents=True, exist_ok=True)
    count_to_write = new_count if args.tab == "closure_list" else 0
    NEW_COUNT_FILE.write_text(str(count_to_write), encoding="utf-8")
    print(f"  Wrote {NEW_COUNT_FILE.name}: {count_to_write}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
