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
# 12 original columns + 5 publication-metadata columns.
# closure_scope (institution | campus) is the only scope signal needed;
# parent_institution_continued and the include_*/sheeo_approach_* flags
# are all derivable from closure_scope and are omitted.
_PUBLIC_COLS = [
    "opeid8", "school_name", "city", "state",
    "control_label", "program_level", "credential_level",
    "enrollment_band", "enrollment_headcount_total",
    "closed_date_iso", "closed_year", "closure_type",
    "opeid6", "unitid",
    "closure_scope",
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


def _build_readme_data(generated_at: str = "") -> list[list]:
    """Return README tab content as a list of single-cell rows."""
    lines = [
        ["Hechinger Report — College Closure Tracker"],
        [f"Generated: {generated_at}" if generated_at else ""],
        [""],
        ["ABOUT THIS SPREADSHEET"],
        ["Tracks U.S. college and university closures using Federal Student Aid (FSA) data,"],
        ["enriched with IPEDS institutional records. Refreshed weekly. Editorial overrides"],
        ["are maintained in the overrides tab."],
        [""],
        ["TABS"],
        [""],
        ["published — public-facing dataset (also read by Datawrapper charts)"],
        ["  Two mutually exclusive event types in the closure_type column:"],
        ["    institution_closure  The entire institution (or last campus of a system) closed."],
        ["    campus_closure       A branch campus closed; parent institution continued."],
        [""],
        ["  Filter recipes:"],
        ["    Institution headline count : closure_scope = institution"],
        ["    Campus closures only       : closure_scope = campus"],
        ["    All closure events         : no filter"],
        ["    By time period             : filter closed_year"],
        [""],
        ["  Column definitions:"],
        ["    opeid8                     8-digit FSA school identifier (OPE ID)"],
        ["    school_name                Name at time of closure (from FSA database)"],
        ["    city                       City"],
        ["    state                      Two-letter US state code"],
        ["    control_label              Institutional control: public | nonprofit | for-profit"],
        ["    program_level              Highest credential offered:"],
        ["                               4-year or above | 2-year | Less than 2-year"],
        ["    credential_level           Specific credential level:"],
        ["                               Bachelor's | Associate's | Certificate | Non-degree |"],
        ["                               Master's | Doctoral | Bachelor's or above"],
        ["    enrollment_band            IPEDS official size category at or near closure"],
        ["    enrollment_headcount_total Total headcount enrollment from IPEDS"],
        ["    closed_date_iso            FSA administrative close date (YYYY-MM-DD)"],
        ["                               This is when Title IV participation ended, not"],
        ["                               necessarily when students last attended."],
        ["    closed_year                Year derived from closed_date_iso"],
        ["    closure_type               institution_closure or campus_closure"],
        ["    opeid6                     6-digit system identifier (parent of opeid8)"],
        ["    unitid                     IPEDS unit identifier matched to this campus"],
        ["    closure_scope              institution or campus (see above)"],
        ["    classification_basis       Evidence used for automatic classification"],
        ["    closure_event_id           Stable unique identifier:"],
        ["                               institution:{opeid6}:{date} or campus:{opeid8}:{date}"],
        [""],
        ["closure_list — full internal dataset for editorial review"],
        ["  Contains all classified records including administrative sites, unclassified"],
        ["  records, and merger candidates. Use to verify classifications and populate"],
        ["  the overrides tab."],
        ["  Additional columns beyond published:"],
        ["    iclevel_label              IPEDS level code abbreviation (4yr+ | 2-4yr | <2yr)"],
        ["    institution_category       IPEDS sector label (public_4yr, for_profit_2yr, etc.)"],
        ["    is_main_campus             true if FSA OPEID suffix = 00"],
        ["    is_suspicious_location     true if name matches SHEEO sub-unit keyword pattern"],
        ["    approach2_main_campus_only true for the institution-level (approach 2) record"],
        ["    is_traditional_college     true for public/nonprofit 4yr or 2yr institutions"],
        ["    closure_type               Internal classifier type (see valid values below)"],
        ["    closure_type_basis         Reason for automatic classification"],
        ["    match_method               IPEDS matching method used"],
        [""],
        ["overrides — editorial closure-type overrides"],
        ["  Each row overrides the automatic classification for one OPEID8."],
        ["  Populated manually by editors. The pipeline reads this tab on each publish run."],
        ["  Required columns:"],
        ["    opeid8             8-digit FSA identifier to override"],
        ["    school_name        School name (for readability; not used by pipeline)"],
        ["    state              State (for readability; not used by pipeline)"],
        ["    closed_date_iso    Close date (for readability; not used by pipeline)"],
        ["    closure_type       Override type (must be one of the valid values below)"],
        ["    closure_type_basis Documented reason for override — required"],
        ["    editor_note        Optional additional context"],
        [""],
        ["  Valid closure_type values for overrides:"],
        ["    institutional_closure    Confirmed operational closure (public/nonprofit)"],
        ["    for_profit_closure       Confirmed operational closure (for-profit)"],
        ["    system_consolidation     Confirmed multi-campus system shutdown"],
        ["    branch_closure           Confirmed branch campus closure"],
        ["    merger_absorption        Confirmed merger or absorption"],
        ["    location_administrative  Confirmed non-campus administrative site"],
        ["    unclassified             Unable to confirm event type"],
        [""],
        ["  Handling merger candidates:"],
        ["    Records automatically detected as merger candidates (closure_type ="],
        ["    merger_absorption in closure_list) are excluded from published by default."],
        ["    RT11 record-holder evidence flags a possible merger but does not prove"],
        ["    that the institution ceased educational operations."],
        ["    To reclassify a confirmed operational closure:"],
        ["      1. Find the record in closure_list (closure_type = merger_absorption)"],
        ["      2. Add a row in this overrides tab with its opeid8"],
        ["      3. Set closure_type = institutional_closure (or for_profit_closure)"],
        ["      4. Document the evidence in closure_type_basis"],
        ["    To confirm a genuine merger (institution lives on under another name):"],
        ["      No action needed — it stays excluded from published closure counts."],
        [""],
        ["by_year — annual and monthly closure counts (from published tab)"],
        ["  Rows with month_num = 0 are annual totals."],
        ["  Rows with month_num 1-12 are monthly breakdowns."],
        ["    year                   Closure year"],
        ["    month_num              0 = annual; 1-12 = month"],
        ["    month                  Month abbreviation or blank for annual rows"],
        ["    total                  Institution closures (= institution_closures)"],
        ["    for_profit             Institution closures: for-profit control"],
        ["    nonprofit              Institution closures: nonprofit control"],
        ["    public                 Institution closures: public control"],
        ["    non_degree             Institution closures: non-degree-granting"],
        ["    certificate            Institution closures: certificate programs"],
        ["    associate              Institution closures: associate's degree"],
        ["    bachelor               Institution closures: bachelor's degree"],
        ["    graduate               Institution closures: master's or doctoral"],
        ["    institution_closures   Full institution closures (= total)"],
        ["    campus_closures        Branch campus closures (parent continued)"],
        ["    all_published_events   institution_closures + campus_closures"],
        ["    unknown_control        Institution closures with no recognized control"],
        ["    unknown_credential     Institution closures with no recognized credential"],
        ["  Reconciliation:"],
        ["    for_profit + nonprofit + public + unknown_control = institution_closures"],
        ["    non_degree + certificate + associate + bachelor + graduate"],
        ["      + unknown_credential = institution_closures"],
        ["    all_published_events = institution_closures + campus_closures"],
        [""],
        ["by_state — closure counts by US state (from published tab)"],
        ["    state                  Two-letter state code"],
        ["    total                  Institution closures (= institution_closures)"],
        ["    for_profit             Institution closures: for-profit control"],
        ["    nonprofit              Institution closures: nonprofit control"],
        ["    public                 Institution closures: public control"],
        ["    institution_closures   Full institution closures"],
        ["    campus_closures        Branch campus closures in this state"],
        ["    all_published_events   institution_closures + campus_closures"],
        ["    unknown_control        Institution closures with no recognized control"],
        ["  Note: an institution closure is counted once in its own state only."],
        [""],
        ["DATA SOURCES"],
        ["  FSA closed school list — weekly export, US Department of Education"],
        ["  IPEDS Institutional Characteristics — 2014-2025 annual data"],
        ["  RT11 record holder data — merger/absorption candidate detection"],
        [""],
        ["METHODOLOGY"],
        ["  Classification follows SHEEO framework (Kelchen 2024, Burchart 2026)."],
        ["  Institution closures use the SHEEO Approach 2 definition: one record per"],
        ["  institution, counting only the last or only campus of each system."],
        ["  Campus closures are independent branch closures where the parent continued."],
        ["  The headline institution closure count: published tab, closure_scope = institution."],
    ]
    return lines


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
        from datetime import datetime, timezone
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _write_tab(sh, "readme", _build_readme_data(generated_at))
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
