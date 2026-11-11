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


def _load_rows(path: Path, columns: list[str]) -> list[list[str]]:
    """Return rows as a list-of-lists (header first), sorted newest-closed first."""
    with path.open(encoding="utf-8", newline="") as fh:
        raw = list(csv.DictReader(fh))
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
# Main logic
# ---------------------------------------------------------------------------

def push(sheet_id: str, tab: str) -> int:
    """Write closure events to the named tab. Returns count of new opeid8s."""
    gc = _get_gspread_client()
    sh = gc.open_by_key(sheet_id)

    columns = TAB_COLUMNS[tab]
    events_path = _latest_events_csv()
    print(f"Loading {events_path.name} …")
    data = _load_rows(events_path, columns)
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
