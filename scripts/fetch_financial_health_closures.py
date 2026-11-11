#!/usr/bin/env python3
"""
Extract editorially confirmed closures from the Financial Health tracker.

The Financial Health tracker (hechinger/Private_Financial_Health_Tracker)
publishes data/college_cuts.json, refreshed daily, in which editors record
institution and campus closure announcements with a status and source URL.
This script reduces that file to a slim CSV the classifier reads:

  data/manual/external/financial_health_closures.csv

This is a ONE-TIME input for the frozen base table: the classifier applies
FH evidence only to events in data/manual/auto_publish_baseline.csv.  It is
not refreshed weekly; closures first seen after the freeze get editor review.

Usage
-----
  gh api repos/hechinger/Private_Financial_Health_Tracker/contents/data/college_cuts.json \
      -H "Accept: application/vnd.github.raw" > college_cuts.json
  python scripts/fetch_financial_health_closures.py --json college_cuts.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT / "data" / "manual" / "external" / "financial_health_closures.csv"

CLOSURE_CUT_TYPES = frozenset({"institution_closure", "campus_closure"})

FIELDS = [
    "unitid", "institution_name", "state", "cut_type", "status",
    "announcement_date", "effective_term", "source_url", "source_generated_at",
]


def extract_closures(data: dict) -> list[dict[str, str]]:
    """Return one row per closure cut with a numeric IPEDS UNITID."""
    schools = data.get("schools", {})
    items = schools.values() if isinstance(schools, dict) else schools
    rows: list[dict[str, str]] = []
    for school in items:
        unitid = str(school.get("unitid") or "").strip()
        if not unitid.isdigit():
            continue  # non-US or unmatched entries use synthetic keys
        for cut in school.get("cuts", []) or []:
            if cut.get("cut_type") not in CLOSURE_CUT_TYPES:
                continue
            rows.append({
                "unitid": unitid,
                "institution_name": str(school.get("institution_name") or ""),
                "state": str(school.get("state") or ""),
                "cut_type": cut["cut_type"],
                "status": str(cut.get("status") or ""),
                "announcement_date": str(cut.get("announcement_date") or ""),
                "effective_term": str(cut.get("effective_term") or ""),
                "source_url": str(cut.get("source_url") or ""),
                "source_generated_at": str(data.get("generated_at") or ""),
            })
    rows.sort(key=lambda r: (r["announcement_date"], r["unitid"]))
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", type=Path, required=True, help="college_cuts.json from the Financial Health tracker")
    p.add_argument("--output", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(argv)

    try:
        data = json.loads(args.json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not read {args.json}: {exc}", file=sys.stderr)
        return 1
    rows = extract_closures(data)
    if not rows:
        # Refuse to overwrite a good snapshot with an empty or malformed fetch.
        print("ERROR: no closure records found; snapshot left unchanged.", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    confirmed = sum(1 for r in rows if r["status"] == "confirmed" and r["cut_type"] == "institution_closure")
    print(f"  {len(rows)} closure records ({confirmed} confirmed institution closures) "
          f"from FH data generated {data.get('generated_at', '?')} -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
