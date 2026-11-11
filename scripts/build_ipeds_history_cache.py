#!/usr/bin/env python3
"""
Build IPEDS year-cache CSVs for years before the Financial Health cache (2014+).

Reads NCES IPEDS zip archives directly (no extraction):
  HD<year>.zip    — directory file (IC9596_A, IC9697_A, IC9798_HDR, IC98HDAC,
                    IC99_HD, FA2000HD, FA2001HD before 2002):
                    UNITID, OPEID, name, city, state,
                    ICLEVEL, CONTROL, SECTOR, HLOFFER, DEGGRANT, HDEGOFR1
  EFFY<year>.zip  — 12-month enrollment (optional; headcount total)

Writes one CSV per year to --out-dir, named ipeds_history_year_<year>.csv,
with the subset of year-cache columns that build_ipeds_crosswalk.py reads.
The cache year equals the IPEDS table year (HD2013 -> year 2013), matching the
Financial Health collector.

highest_degree uses the HDEGOFR1 coding of the 2014+ cache
(11-14 doctoral, 20 master's, 30 bachelor's, 40 associate's, 0 non-degree).
HD files before 2009 lack HDEGOFR1, so it is derived from HLOFFER + DEGGRANT.

Usage (from project root):
  python scripts/build_ipeds_history_cache.py --source-dir ~/Downloads
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = PROJECT / "data" / "inputs" / "ipeds" / "history_year_cache"

CACHE_FIELDS = [
    "unitid", "opeid", "year", "institution_name", "city", "state",
    "level", "control", "sector", "highest_degree",
    "enrollment_headcount_total", "size",
    "status", "is_active", "date_closed",
]


def _status(cyactive: str, newid: str, closedat: str) -> str:
    """Approximate the 2014+ HD STATUS code from pre-2014 fields.

    A = active; C = combined with another institution (NEWID points to the
    absorbing UNITID); D = closed/out of business.
    """
    if cyactive in {"", "1"}:
        return "A"
    if newid not in {"", "-2", "-1"}:
        return "C"
    return "D" if closedat not in {"", "-2"} or cyactive == "3" else "A"

_ZIP_RE = re.compile(r"^(HD|EFFY)(\d{4})\.zip$", re.IGNORECASE)
# Before 2002 NCES published the directory file under other names.
_LEGACY_HD = {
    "IC9596_A.ZIP": 1995, "IC9697_A.ZIP": 1996, "IC9798_HDR.ZIP": 1997,
    "IC98HDAC.ZIP": 1998, "IC99_HD.ZIP": 1999, "FA2000HD.ZIP": 2000, "FA2001HD.ZIP": 2001,
}


def _hloffer_to_hdegofr1(hloffer: str, deggrant: str) -> str:
    """Map HLOFFER (highest level of offering) to HDEGOFR1 codes."""
    if deggrant == "2":  # not degree-granting
        return "0"
    return {
        "9": "14",            # doctor's
        "8": "20", "7": "20",  # post-master's cert / master's
        "6": "30", "5": "30",  # post-bacc cert / bachelor's
        "4": "40", "3": "40",  # 2-4 yr cert / associate's
        "2": "0", "1": "0", "0": "0",
    }.get(hloffer, "-3")


def _read_zip_csv(path: Path) -> list[dict[str, str]]:
    """Return rows of the CSV in an NCES zip, preferring the original over _rv.

    The Financial Health 2014+ cache reads the original release file; matching
    that keeps one convention across the whole series (verified on 2014: 0
    enrollment mismatches with the original, 34 with the revised file).
    """
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            return []
        original = [n for n in names if not n.lower().endswith("_rv.csv")]
        name = (original or names)[0]
        text = zf.read(name).decode("latin-1")
    rows = list(csv.DictReader(io.StringIO(text)))
    # NCES headers are upper-case but occasionally padded.
    return [{(k or "").strip().upper(): (v or "").strip() for k, v in r.items()} for r in rows]


def _effy_totals(rows: list[dict[str, str]]) -> dict[str, str]:
    """Return {unitid: total 12-month headcount} from EFFY rows."""
    totals: dict[str, str] = {}
    for r in rows:
        is_total = r.get("EFFYLEV") == "1" or r.get("LSTUDY") == "999"
        if is_total and r.get("UNITID"):
            totals.setdefault(r["UNITID"], r.get("EFYTOTLT", ""))
    return totals


def build_year(year: int, hd_rows: list[dict[str, str]], effy: dict[str, str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in hd_rows:
        hdeg = r.get("HDEGOFR1", "")
        if not hdeg:
            hdeg = _hloffer_to_hdegofr1(r.get("HLOFFER", ""), r.get("DEGGRANT", ""))
        out.append({
            "unitid": r.get("UNITID", ""),
            "opeid": r.get("OPEID", ""),
            "year": str(year),
            "institution_name": r.get("INSTNM", ""),
            "city": r.get("CITY", ""),
            "state": r.get("STABBR", ""),
            "level": r.get("ICLEVEL", ""),
            "control": r.get("CONTROL", ""),
            "sector": r.get("SECTOR", ""),
            "highest_degree": hdeg,
            "enrollment_headcount_total": effy.get(r.get("UNITID", ""), ""),
            "size": "",
            "status": _status(r.get("CYACTIVE", ""), r.get("NEWID", ""), r.get("CLOSEDAT", "")),
            "is_active": r.get("CYACTIVE", "") or "1",
            "date_closed": r.get("CLOSEDAT", "") or "-2",
        })
    return out


def find_archives(source_dirs: list[Path]) -> tuple[dict[int, Path], dict[int, Path]]:
    hd: dict[int, Path] = {}
    effy: dict[int, Path] = {}
    for d in source_dirs:
        if not d.exists():
            continue
        for p in sorted(d.rglob("*.zip")):
            if p.name.upper() in _LEGACY_HD:
                hd.setdefault(_LEGACY_HD[p.name.upper()], p)
                continue
            m = _ZIP_RE.match(p.name)
            if not m:
                continue
            kind, year = m.group(1).upper(), int(m.group(2))
            (hd if kind == "HD" else effy).setdefault(year, p)
    return hd, effy


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source-dir", type=Path, action="append", required=True,
                   help="Directory containing HD<year>.zip / EFFY<year>.zip (repeatable)")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--min-year", type=int, default=1995,
                   help="First year to build (1995 lets 1996 closures match, Kelchen's window)")
    p.add_argument("--max-year", type=int, default=2013,
                   help="Last year to build (2014+ comes from the Financial Health cache)")
    args = p.parse_args(argv)

    hd, effy = find_archives(args.source_dir)
    years = [y for y in sorted(hd) if args.min_year <= y <= args.max_year]
    if not years:
        print("ERROR: no HD<year>.zip archives found in range.", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for year in years:
        hd_rows = _read_zip_csv(hd[year])
        totals = _effy_totals(_read_zip_csv(effy[year])) if year in effy else {}
        rows = build_year(year, hd_rows, totals)
        out = args.out_dir / f"ipeds_history_year_{year}.csv"
        with out.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=CACHE_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        print(f"  {year}: {len(rows):,} institutions"
              f"{'' if year in effy else ' (no EFFY — enrollment blank)'} -> {out.name}")

    missing = [y for y in range(args.min_year, args.max_year + 1) if y not in hd]
    if missing:
        print(f"Missing HD archives for: {', '.join(map(str, missing))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
