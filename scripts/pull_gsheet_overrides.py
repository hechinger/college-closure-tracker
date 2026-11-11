#!/usr/bin/env python3
"""Pull the combined editorial review/overrides queue from Google Sheets.

Pending and resolved rows live in the same tab. The complete queue is written
locally; the classifier applies only review_status=resolved rows.

Usage
-----
  python scripts/pull_gsheet_overrides.py --sheet SHEET_ID [--tab overrides]
      [--output data/manual/closure_type_overrides.csv]

Validation
----------
  Resolved rows require a decision, valid closure type, documented basis, and
  internally consistent operational facts. Pending rows do not require an
  editorial decision. The script exits 1 if any row fails validation.

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
from classify_closure_events import (
    EVENT_TYPES,
    PUBLICATION_DECISIONS,
    REVIEW_STATUSES,
)

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT / "data" / "manual" / "closure_type_overrides.csv"

# One combined review and decision record per closure_event_id. Context and
# candidate fields are pipeline-owned; decision/evidence fields are editorial.
OVERRIDES_FIELDS = [
    "closure_event_id",
    "opeid8",
    "school_name",
    "state",
    "fsa_closed_date_iso",
    "review_reason",
    "candidate_closure_scope",
    "review_status",
    "publication_decision",
    "closure_type",
    "closure_type_basis",
    "location_operations_ceased",
    "operations_ceased_basis",
    "parent_continued",
    "parent_continued_basis",
    "verified_closed_date_iso",
    "date_basis",
    "date_evidence_url",
    "replacement_opeid8",
    "replacement_effective_date_iso",
    "evidence_fingerprint",
    "editor_note",
    "reviewed_by",
    "reviewed_on",
]

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
VALID_REVIEW_STATUSES = frozenset(REVIEW_STATUSES)
VALID_PUBLICATION_DECISIONS = frozenset(PUBLICATION_DECISIONS)
_TRI_STATE = frozenset({"true", "false", "unknown"})
_EVIDENCE_REQUIRED_TYPES = frozenset({
    "merger_absorption", "system_consolidation", "identifier_replacement",
    "affiliation_or_governance_change",
})


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
        raw_id = str(row.get("opeid8", "")).strip()
        opeid8 = raw_id.zfill(8) if raw_id else ""
        event_id = str(row.get("closure_event_id", "")).strip()
        ctype = str(row.get("closure_type", "")).strip()
        basis = str(row.get("closure_type_basis", "")).strip()
        status = str(row.get("review_status", "")).strip()
        decision = str(row.get("publication_decision", "")).strip()
        location_ceased = str(row.get("location_operations_ceased", "")).strip()
        parent_continued = str(row.get("parent_continued", "")).strip()

        # Migration for the original decisions-only tab.
        if not status:
            status = "resolved" if ctype else "pending"
        if not decision and status == "resolved" and ctype:
            if ctype in {"institutional_closure", "for_profit_closure", "system_consolidation"}:
                decision = "include_institution"
            elif ctype == "branch_closure":
                decision = "include_campus"
            else:
                decision = "exclude"

        if not (event_id or opeid8):
            print(f"  WARNING: row {i} skipped — empty closure_event_id and opeid8", file=sys.stderr)
            errors += 1
            continue
        if status not in VALID_REVIEW_STATUSES:
            print(f"  WARNING: row {i} ({event_id or opeid8}) skipped — unknown review_status {status!r}",
                  file=sys.stderr)
            errors += 1
            continue

        if status == "resolved":
            if not ctype or ctype not in VALID_EVENT_TYPES:
                print(f"  WARNING: row {i} ({event_id or opeid8}) skipped — invalid resolved closure_type {ctype!r}",
                      file=sys.stderr)
                errors += 1
                continue
            if not basis:
                print(f"  WARNING: row {i} ({event_id or opeid8}) skipped — resolved row needs closure_type_basis",
                      file=sys.stderr)
                errors += 1
                continue
            if decision not in VALID_PUBLICATION_DECISIONS:
                print(f"  WARNING: row {i} ({event_id or opeid8}) skipped — invalid publication_decision {decision!r}",
                      file=sys.stderr)
                errors += 1
                continue
            if location_ceased and location_ceased not in _TRI_STATE:
                print(f"  WARNING: row {i} ({event_id or opeid8}) skipped — invalid location_operations_ceased",
                      file=sys.stderr)
                errors += 1
                continue
            if parent_continued and parent_continued not in _TRI_STATE:
                print(f"  WARNING: row {i} ({event_id or opeid8}) skipped — invalid parent_continued",
                      file=sys.stderr)
                errors += 1
                continue
            if decision == "include_institution" and not (
                location_ceased == "true" and parent_continued == "false"
            ):
                print(f"  WARNING: row {i} ({event_id or opeid8}) skipped — institution inclusion requires location_operations_ceased=true and parent_continued=false",
                      file=sys.stderr)
                errors += 1
                continue
            if decision == "include_campus" and not (
                location_ceased == "true" and parent_continued == "true"
            ):
                print(f"  WARNING: row {i} ({event_id or opeid8}) skipped — campus inclusion requires location_operations_ceased=true and parent_continued=true",
                      file=sys.stderr)
                errors += 1
                continue
            evidence_url = str(row.get("date_evidence_url", "")).strip()
            if ctype in _EVIDENCE_REQUIRED_TYPES and not evidence_url:
                print(f"  WARNING: row {i} ({event_id or opeid8}) skipped — {ctype} needs date_evidence_url",
                      file=sys.stderr)
                errors += 1
                continue

        valid.append({
            field: (
                event_id if field == "closure_event_id" else
                opeid8 if field == "opeid8" else
                status if field == "review_status" else
                decision if field == "publication_decision" else
                ctype if field == "closure_type" else
                basis if field == "closure_type_basis" else
                str(row.get("closed_date_iso", "")).strip()
                    if field == "fsa_closed_date_iso" and not row.get(field) else
                str(row.get(field, "")).strip()
            )
            for field in OVERRIDES_FIELDS
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
