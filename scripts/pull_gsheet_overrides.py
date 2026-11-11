#!/usr/bin/env python3
"""Pull editorial overrides from the Google Sheet and write closure_type_overrides.csv.

Reads the 'overrides' tab of the Hechinger review Sheet (columns: opeid8,
closure_type, closure_type_basis, editor_note) and writes the result to
data/manual/closure_type_overrides.csv so the classifier picks it up on the
next run.

Usage
-----
  python scripts/pull_gsheet_overrides.py --sheet SHEET_ID [--tab overrides]
      [--output data/manual/closure_type_overrides.csv]

Validation
----------
  Rows with an empty opeid8 or an unrecognised closure_type are skipped with a
  warning. The script exits 1 if any rows fail validation, so CI catches a
  misconfigured sheet before the classifier runs.

Credentials
-----------
  Uses Application Default Credentials (ADC).  In GitHub Actions the
  google-github-actions/auth step sets credentials automatically via
  Workload Identity Federation.  Locally run:
    gcloud auth application-default login
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import google.auth
import gspread
from google.auth.transport.requests import Request as _AuthRequest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify_closure_events import EVENT_TYPES

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT / "data" / "manual" / "closure_type_overrides.csv"
OVERRIDES_FIELDS = ["opeid8", "closure_type", "closure_type_basis", "editor_note"]

_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


def _get_gspread_client() -> gspread.Client:
    creds, _ = google.auth.default(scopes=_SCOPES)
    if not creds.valid:
        creds.refresh(_AuthRequest())
    return gspread.Client(auth=creds)

VALID_EVENT_TYPES = frozenset(EVENT_TYPES)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pull_tab(sheet_id: str, tab: str) -> list[dict[str, str]]:
    """Fetch all rows from the named tab as list-of-dicts."""
    gc = _get_gspread_client()
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(tab)
    return ws.get_all_records(default_blank="")


def _validate_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    """Return (valid_rows, error_count). Prints warnings for bad rows."""
    valid: list[dict[str, str]] = []
    errors = 0
    for i, row in enumerate(rows, 1):
        opeid8 = str(row.get("opeid8", "")).strip().zfill(8)
        ctype  = str(row.get("closure_type", "")).strip()
        if not opeid8:
            print(f"  WARNING: row {i} skipped — empty opeid8", file=sys.stderr)
            errors += 1
            continue
        if not ctype:
            print(f"  WARNING: row {i} ({opeid8}) skipped — empty closure_type", file=sys.stderr)
            errors += 1
            continue
        if ctype not in VALID_EVENT_TYPES:
            print(
                f"  WARNING: row {i} ({opeid8}) skipped — unknown closure_type {ctype!r};"
                f" valid: {sorted(VALID_EVENT_TYPES)}",
                file=sys.stderr,
            )
            errors += 1
            continue
        valid.append({
            "opeid8":               opeid8,
            "closure_type":         ctype,
            "closure_type_basis":   str(row.get("closure_type_basis", "")).strip(),
            "editor_note":          str(row.get("editor_note", "")).strip(),
        })
    return valid, errors


def _write_csv(rows: list[dict[str, str]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OVERRIDES_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sheet", required=True, help="Google Sheet ID (from the URL)")
    p.add_argument("--tab", default="overrides", help="Sheet tab to read (default: overrides)")
    p.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output CSV path (default: data/manual/closure_type_overrides.csv)",
    )
    args = p.parse_args()

    print(f"Pulling '{args.tab}' tab from sheet {args.sheet[:8]}…")
    try:
        raw_rows = _pull_tab(args.sheet, args.tab)
    except gspread.exceptions.WorksheetNotFound:
        print(f"ERROR: tab '{args.tab}' not found in sheet.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"  {len(raw_rows)} rows fetched")
    valid_rows, errors = _validate_rows(raw_rows)
    print(f"  {len(valid_rows)} valid overrides, {errors} skipped")

    if errors:
        print(
            f"ERROR: {errors} row(s) failed validation — fix the sheet before publishing.",
            file=sys.stderr,
        )
        return 1

    _write_csv(valid_rows, args.output)
    print(f"  Written {len(valid_rows)} overrides to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
