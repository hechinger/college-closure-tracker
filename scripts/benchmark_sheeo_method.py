#!/usr/bin/env python3
"""
Benchmark: replicate SHEEO's closure method and reconcile it with ours.

SHEEO's R script (used for the Hechinger tracker's earlier "nearly 300
colleges offering an associate degree or higher, 2008-2023" figure) works at
the OPEID6 level on WSF rectype_01:

  * keep sch_type < 4 (public, nonprofit, proprietary)
  * an OPEID6 counts as closed only when EVERY location row has a close date
    (R's NA handling drops the OPEID6 if any location is open) AND
    max(location suffix) + 1 == number of rows (no numbering gaps)
  * the closure year is the year the LAST location closed

This script ports that rule faithfully, then compares it with our
institution closures (resolved/auto_resolved include_institution events) and
labels every disagreement:

  sheeo_only: satellite OPEID6, held for our editors (with reason),
              deliberately excluded (with type), no main-campus record
  ours_only:  a location still listed open (stale row), numbering gap,
              not in rectype_01

No network access, no Sheet writes.

Usage (from project root):
  python scripts/benchmark_sheeo_method.py [--wsf-zip ...] [--events-csv ...]
      [--start-year 2008] [--end-year 2023]
"""

from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import sys
import zipfile
from datetime import date
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DERIVED = PROJECT / "data" / "derived"
RAW_WSF = PROJECT / "data" / "raw" / "weekly_school_file"

# SHEEO pgm_length codes for associate degree or higher
# (5 associate, 6 bachelor's, 7 first professional, 8 master's or doctor's,
#  13 master's, 14 doctor's).
ASSOCIATE_PLUS = frozenset({5, 6, 7, 8, 13, 14})

# Review reasons that appear on nearly every row and explain nothing.
_GENERIC_REASONS = frozenset({
    "operational_closure_unverified", "record_custodian_available", "possible_system_consolidation",
})

OUT_FIELDS = [
    "opeid6", "school_name", "state", "sheeo_close_year", "sheeo_pgm_length", "sheeo_sch_type",
    "our_close_year", "status", "reason",
]


def _sub(line: str, start: int, end: int) -> str:
    """R substr(x, start, end): 1-indexed, inclusive."""
    return line[start - 1:end]


def _int(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.lstrip("-").isdigit() else None


def parse_rt01_lines(lines) -> list[dict]:
    """Parse WSF rectype_01 fixed-width lines with SHEEO's positions."""
    rows = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        opeid = _sub(line, 3, 10)
        rows.append({
            "opeid6": opeid[:6],
            "suffix": _int(opeid[6:8]),
            "school_name": _sub(line, 12, 81).strip(),
            "state": _sub(line, 247, 248),
            "sch_type": _int(_sub(line, 357, 357)),
            "close_year": _int(_sub(line, 382, 385)),
            "pgm_length": _int(_sub(line, 355, 356)),
        })
    return rows


def sheeo_closures(rows: list[dict]) -> dict[str, dict]:
    """Apply SHEEO's rule; return {opeid6: closure row}."""
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        if r["sch_type"] is not None and r["sch_type"] < 4:
            groups[r["opeid6"]].append(r)
    out: dict[str, dict] = {}
    for opeid6, grp in groups.items():
        if any(r["suffix"] is None or r["close_year"] is None for r in grp):
            continue
        if max(r["suffix"] for r in grp) + 1 != len(grp):
            continue
        last = max(r["close_year"] for r in grp)
        latest = [r for r in grp if r["close_year"] == last]
        states = {r["state"] for r in latest}
        out[opeid6] = {
            **latest[0],
            "state": latest[0]["state"] if len(states) == 1 and len(latest) == 1 else "Multiple",
            "closed_count": len(grp),
        }
    return out


def _ours(events: list[dict]) -> tuple[dict[str, list[int]], dict[str, list[dict]]]:
    inst: dict[str, list[int]] = collections.defaultdict(list)
    mains: dict[str, list[dict]] = collections.defaultdict(list)
    for e in events:
        if e.get("location_suffix") == "00":
            mains[e.get("opeid6", "")].append(e)
        year = e.get("closed_year", "")
        if (e.get("publication_decision") == "include_institution" and e.get("review_required") == "false"
                and year.isdigit()):
            inst[e.get("opeid6", "")].append(int(year))
    return inst, mains


def _sheeo_only_reason(opeid6: str, mains: dict[str, list[dict]], inst: dict[str, list[int]]) -> str:
    if not opeid6.startswith("0"):
        return "satellite OPEID6 (not an institution ID)"
    if not mains.get(opeid6):
        return "no main-campus (00) record in FSA closed-school file"
    latest = max(mains[opeid6], key=lambda e: e.get("closed_date_iso", ""))
    if latest.get("review_required") == "true":
        reasons = [x for x in latest.get("review_reason", "").split("|") if x and x not in _GENERIC_REASONS]
        return "held for editor review: " + (reasons[0] if reasons else "other")
    if latest.get("publication_decision") == "exclude":
        return "deliberately excluded: " + latest.get("closure_type", "")
    if inst.get(opeid6):
        return "published, but close year differs by more than 2"
    return "other"


def _ours_only_reason(locations: list[dict]) -> str:
    locs = [r for r in locations if r["sch_type"] is not None and r["sch_type"] < 4]
    if not locs:
        return "not in WSF rectype_01 (or sch_type >= 4)"
    if any(r["close_year"] is None for r in locs):
        return "a location still listed open (stale row); SHEEO drops the OPEID6"
    suffixes = [r["suffix"] for r in locs if r["suffix"] is not None]
    if suffixes and max(suffixes) + 1 != len(locs):
        return "location numbering gap; SHEEO drops the OPEID6"
    return "SHEEO last-location year outside window"


def reconcile(rt01_rows: list[dict], events: list[dict], start: int, end: int) -> tuple[list[dict], dict]:
    sheeo = sheeo_closures(rt01_rows)
    inst, mains = _ours(events)
    locations: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rt01_rows:
        locations[r["opeid6"]].append(r)

    in_window = lambda y: y is not None and start <= y <= end  # noqa: E731
    sheeo_w = {o: r for o, r in sheeo.items() if in_window(r["close_year"])}
    ours_w = {o for o, years in inst.items() if any(in_window(y) for y in years)}

    def matched(o: str) -> bool:
        s = sheeo.get(o)
        return bool(s) and any(abs(y - s["close_year"]) <= 2 for y in inst.get(o, []))

    out: list[dict] = []
    for o, s in sorted(sheeo_w.items()):
        ours_years = inst.get(o, [])
        status = "both" if matched(o) else "sheeo_only"
        out.append({
            "opeid6": o, "school_name": s["school_name"], "state": s["state"],
            "sheeo_close_year": s["close_year"], "sheeo_pgm_length": s["pgm_length"] if s["pgm_length"] is not None else "",
            "sheeo_sch_type": s["sch_type"],
            "our_close_year": min(ours_years, key=lambda y: abs(y - s["close_year"])) if ours_years else "",
            "status": status,
            "reason": "" if status == "both" else _sheeo_only_reason(o, mains, inst),
        })
    for o in sorted(ours_w - set(sheeo_w)):
        if matched(o):
            continue  # SHEEO counts it, just in a year outside the window
        main = (mains.get(o) or [{}])[0]
        out.append({
            "opeid6": o, "school_name": main.get("school_name", ""), "state": main.get("state", ""),
            "sheeo_close_year": "", "sheeo_pgm_length": "", "sheeo_sch_type": "",
            "our_close_year": min(y for y in inst[o] if in_window(y)),
            "status": "ours_only", "reason": _ours_only_reason(locations.get(o, [])),
        })

    deg = [s for s in sheeo_w.values() if s["pgm_length"] in ASSOCIATE_PLUS]
    summary = {
        "window": f"{start}-{end}",
        "sheeo_closures": len(sheeo_w),
        "sheeo_associate_plus": len(deg),
        "sheeo_associate_plus_for_profit_share": round(
            sum(1 for s in deg if s["sch_type"] == 3) / len(deg), 3) if deg else None,
        "our_institution_closures": len(ours_w),
        "both": sum(1 for r in out if r["status"] == "both"),
        "sheeo_only": dict(collections.Counter(r["reason"] for r in out if r["status"] == "sheeo_only")),
        "ours_only": dict(collections.Counter(r["reason"] for r in out if r["status"] == "ours_only")),
    }
    return out, summary


def _latest(pattern_dir: Path, pattern: str) -> Path | None:
    files = sorted(pattern_dir.glob(pattern), reverse=True)
    return files[0] if files else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--wsf-zip", type=Path, default=None, help="WSF access-format zip (default: latest)")
    p.add_argument("--events-csv", type=Path, default=None, help="fsa_closure_events CSV (default: latest)")
    p.add_argument("--start-year", type=int, default=2008)
    p.add_argument("--end-year", type=int, default=2023)
    p.add_argument("--out-dir", type=Path, default=DERIVED)
    p.add_argument("--date", default=date.today().isoformat())
    args = p.parse_args(argv)

    wsf = args.wsf_zip or _latest(RAW_WSF, "*/access/schfile_access_format_*.zip")
    events_path = args.events_csv or _latest(DERIVED, "fsa_closure_events_*.csv")
    if not wsf or not wsf.exists() or not events_path or not events_path.exists():
        print("ERROR: need a WSF zip and an fsa_closure_events CSV.", file=sys.stderr)
        return 1

    with zipfile.ZipFile(wsf) as zf:
        member = next((m for m in zf.namelist() if "rectype_01" in m), None)
        if member is None:
            print(f"ERROR: no rectype_01 member in {wsf}", file=sys.stderr)
            return 1
        rt01 = parse_rt01_lines(io.TextIOWrapper(zf.open(member), encoding="latin-1"))
    with events_path.open(encoding="utf-8", newline="") as fh:
        events = list(csv.DictReader(fh))

    rows, summary = reconcile(rt01, events, args.start_year, args.end_year)
    summary.update({"wsf_file": wsf.name, "events_file": events_path.name})

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = args.out_dir / f"benchmark_sheeo_{args.date}.csv"
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    (args.out_dir / f"benchmark_sheeo_{args.date}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Rows -> {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
