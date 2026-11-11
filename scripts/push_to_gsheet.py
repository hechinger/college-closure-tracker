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

# Columns written to the editorial review tab (closure_list).
# Editors see classification context and can spot errors before publish.
_EDITORIAL_COLS = [
    "opeid8",
    "school_name",
    "city",
    "state",
    "control_label",
    "institution_category",
    "enrollment_band",
    "enrollment_headcount_total",
    "closed_date_iso",
    "closed_year",
    "approach2_main_campus_only",
    "is_traditional_college",
    "closure_type",
    "closure_type_basis",
]

# Columns written to the published tab (Datawrapper reads this).
_PUBLIC_COLS = [
    "opeid8",
    "school_name",
    "city",
    "state",
    "control_label",
    "iclevel_label",
    "institution_category",
    "credential_level",
    "enrollment_band",
    "enrollment_headcount_total",
    "closed_date_iso",
    "closed_year",
    "closure_type",
]

TAB_COLUMNS: dict[str, list[str]] = {
    "closure_list": _EDITORIAL_COLS,
    "published":    _PUBLIC_COLS,
}


# ---------------------------------------------------------------------------
# Helpers
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


def _load_rows(
    path: Path,
    columns: list[str],
    min_year: int | None = None,
) -> list[list[str]]:
    """Return rows as a list-of-lists (header first), sorted newest-closed first."""
    with path.open(encoding="utf-8", newline="") as fh:
        raw = list(csv.DictReader(fh))
    if min_year is not None:
        raw = [r for r in raw if r.get("closed_year", "0") >= str(min_year)]
    raw.sort(key=lambda r: r.get("closed_date_iso", ""), reverse=True)
    result: list[list[str]] = [columns]
    for row in raw:
        result.append([row.get(c, "") for c in columns])
    return result


def _existing_opeid8s(ws: gspread.Worksheet) -> set[str]:
    """Return opeid8 values already in the sheet (column A, header skipped)."""
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


# ---------------------------------------------------------------------------
# Aggregated chart tabs
# ---------------------------------------------------------------------------

_MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

def _agg_counts() -> dict:
    return {"total": 0, "for_profit": 0, "nonprofit": 0, "public": 0,
            "non_degree": 0, "certificate": 0, "associate": 0, "bachelor": 0, "graduate": 0}


def _bump(d: dict, ctrl: str, cred: str) -> None:
    d["total"] += 1
    if ctrl == "for-profit":   d["for_profit"] += 1
    elif ctrl == "nonprofit":  d["nonprofit"] += 1
    elif ctrl == "public":     d["public"] += 1
    if cred == "Non-degree":    d["non_degree"] += 1
    elif cred == "Certificate": d["certificate"] += 1
    elif cred == "Associate's": d["associate"] += 1
    elif cred == "Bachelor's":  d["bachelor"] += 1
    elif cred in ("Master's", "Doctoral"): d["graduate"] += 1


def _approach2_rows(path: Path, min_year: int = 2000) -> list[dict]:
    """Load events CSV filtered to approach2 (institutional closures) from min_year on."""
    with path.open(encoding="utf-8", newline="") as fh:
        raw = list(csv.DictReader(fh))
    return [r for r in raw
            if r.get("approach2_main_campus_only") == "true"
            and r.get("closed_year", "0") >= str(min_year)]


def _build_by_year_data(path: Path) -> list[list]:
    """Annual + monthly closure counts by control type and credential level."""
    from collections import defaultdict
    rows = _approach2_rows(path)
    annual:  dict = defaultdict(_agg_counts)
    monthly: dict = defaultdict(_agg_counts)
    for r in rows:
        year = r.get("closed_year", "")
        if not year:
            continue
        ctrl = r.get("control_label", "")
        cred = r.get("credential_level", "")
        date_iso = r.get("closed_date_iso", "")
        mn = int(date_iso[5:7]) if len(date_iso) >= 7 and date_iso[5:7].isdigit() else 0
        _bump(annual[year], ctrl, cred)
        if mn:
            _bump(monthly[(year, mn)], ctrl, cred)
    header = ["year","month_num","month","total","for_profit","nonprofit","public",
              "non_degree","certificate","associate","bachelor","graduate"]
    out = [header]
    for yr in sorted(annual):
        d = annual[yr]
        out.append([yr, 0, "", d["total"], d["for_profit"], d["nonprofit"], d["public"],
                    d["non_degree"], d["certificate"], d["associate"], d["bachelor"], d["graduate"]])
    for (yr, mn) in sorted(monthly):
        d = monthly[(yr, mn)]
        out.append([yr, mn, _MONTH_NAMES[mn-1], d["total"], d["for_profit"], d["nonprofit"],
                    d["public"], d["non_degree"], d["certificate"], d["associate"], d["bachelor"], d["graduate"]])
    return out


def _build_by_state_data(path: Path) -> list[list]:
    """Closure counts by US state/territory, 2000-present, approach2."""
    from collections import defaultdict
    rows = _approach2_rows(path)
    by_state: dict = defaultdict(lambda: {"total":0,"for_profit":0,"nonprofit":0,"public":0})
    for r in rows:
        state = r.get("state", "").strip()
        if not state or len(state) != 2:
            continue
        ctrl = r.get("control_label", "")
        d = by_state[state]
        d["total"] += 1
        if ctrl == "for-profit":  d["for_profit"] += 1
        elif ctrl == "nonprofit": d["nonprofit"] += 1
        elif ctrl == "public":    d["public"] += 1
    header = ["state","total","for_profit","nonprofit","public"]
    return [header] + [[s, d["total"], d["for_profit"], d["nonprofit"], d["public"]]
                        for s, d in sorted(by_state.items())]


def _write_tab(sh: gspread.Spreadsheet, tab: str, data: list[list]) -> None:
    ws = _open_or_create_tab(sh, tab, len(data[0]) if data else 1)
    ws.clear()
    ws.update("A1", data)
    print(f"  '{tab}' tab written ({len(data)-1} rows)")


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def push(sheet_id: str, tab: str) -> int:
    """Write closure events to the named tab. Returns count of new opeid8s."""
    gc = _get_gspread_client()
    sh = gc.open_by_key(sheet_id)

    columns = TAB_COLUMNS[tab]
    events_path = _latest_events_csv()
    print(f"Loading {events_path.name} …")
    min_year = 2000 if tab == "published" else None
    data = _load_rows(events_path, columns, min_year=min_year)
    nrows = len(data) - 1
    print(f"  {nrows:,} records → '{tab}' tab")

    ws = _open_or_create_tab(sh, tab, len(columns))

    new_count = 0
    if tab == "closure_list":
        existing = _existing_opeid8s(ws)
        new_opeid8s = {row[0] for row in data[1:] if row[0]} - existing
        new_count = len(new_opeid8s)
        print(f"  {new_count} new opeid8(s) not previously in sheet")

    # Full refresh: clear then rewrite in one batch.
    ws.clear()
    ws.update("A1", data)
    print(f"  Written {nrows:,} rows to '{tab}' tab.")

    if tab == "published":
        print("Writing aggregated chart tabs…")
        _write_tab(sh, "by_year",  _build_by_year_data(events_path))
        _write_tab(sh, "by_state", _build_by_state_data(events_path))

    return new_count


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
    # Always write the count file so the CI github-script step never sees a missing file.
    count_to_write = new_count if args.tab == "closure_list" else 0
    NEW_COUNT_FILE.write_text(str(count_to_write), encoding="utf-8")
    print(f"  Wrote {NEW_COUNT_FILE.name}: {count_to_write}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
