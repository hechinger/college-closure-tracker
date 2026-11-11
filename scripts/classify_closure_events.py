"""
Phase 8: Classify each FSA closure record by event type.

Reads:  data/derived/fsa_closure_ipeds_enriched_<date>.csv  (latest by default)
        data/derived/fsa_wsf_rt06_<date>.csv                (optional; loaded for diagnostics only)
        data/derived/fsa_wsf_rt11_<date>.csv                (optional; merger heuristic)

Writes: data/derived/fsa_closure_events_<date>.csv
        data/derived/closure_events_summary_<date>.json

Classification rules (applied in priority order):

  location_administrative  — is_suspicious_location == "true"
                             Sub-unit or non-campus site per SHEEO (not a school closure).
                             Flag only: does not remove the record.

  branch_closure           — is_main_campus == "false"
                             Campus of a system; parent system may still be open.
                             Fed Reserve (Kelchen 2025): 90 pct of FSA closures are branches.

  merger_absorption        — RT11 record_holder_desc names another active institution.
                             NOTE: RT06 OPEID-change records are NOT used here — they capture
                             historical corporate-lineage OPEID changes (some 40+ years old),
                             not merger-at-closure events, and produce high false-positive rates.
                             Requires --rt11-csv.

  system_consolidation     — is_main_campus == "true" AND the OPEID6 has >1 distinct campus
                             in the closure dataset, suggesting a whole-system shutdown.
                             Example pattern: multi-campus system closes all locations.

  for_profit_closure       — is_main_campus == "true" AND control == "for-profit"
                             Title IV exit or operational shutdown; Burchart (2026): categorically
                             different from traditional-college closures — usually a business
                             decision, not an enrollment-cliff closure.

  institutional_closure    — is_main_campus == "true" AND is_traditional_college == "true"
                             Public or nonprofit institution gone.

  unclassified             — does not match any rule (missing closure date, no IPEDS, etc.)

Usage (from project root):
  python scripts/classify_closure_events.py
  python scripts/classify_closure_events.py \\
      --rt06-csv data/derived/fsa_wsf_rt06_2026-11-05.csv \\
      --rt11-csv data/derived/fsa_wsf_rt11_2026-11-05.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_ipeds_crosswalk import ENRICHED_FIELDS

PROJECT  = Path(__file__).resolve().parent.parent
DERIVED  = PROJECT / "data" / "derived"
MANUAL   = PROJECT / "data" / "manual"
OVERRIDES_DEFAULT = MANUAL / "closure_type_overrides.csv"

# When an OPEID6 has closed campuses spanning more than this many years,
# we don't call it a consolidation — it's more likely separate closure events.
CONSOLIDATION_WINDOW_YEARS = 5

EVENT_TYPES = (
    "location_administrative",
    "branch_closure",
    "merger_absorption",
    "system_consolidation",
    "for_profit_closure",
    "institutional_closure",
    "unclassified",
)

EVENTS_FIELDS = ENRICHED_FIELDS + [
    "closure_type",
    "closure_type_basis",
]

# Tokens that mark a record_holder_desc as a government body / agency,
# not another institution. Matched against lowercased individual words.
_GOVT_HOLDER_TOKENS = frozenset({
    "state", "agency", "board", "ombudsman", "council",
    "department", "bureau", "commission", "division",
    "office", "authority", "accrediting", "accreditation",
    "approving", "licensing", "postsecondary", "treasury",
    "federal", "court", "chapter", "bankruptcy", "trustee",
    "guaranty", "guarantee", "guaranteed", "veterans", "district",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _looks_like_institution(desc: str) -> bool:
    """
    Heuristic: return True when record_holder_desc looks like another school,
    not a government body or agency. Conservative — false negatives are safer
    than false positives for merger_absorption classification.
    """
    if not desc:
        return False
    cleaned = desc.strip()
    if len(cleaned) < 6:
        return False
    words = set(cleaned.lower().split())
    # Any government/agency keyword → not an institution
    if words & _GOVT_HOLDER_TOKENS:
        return False
    # Must have at least 2 words (a college name is never one word)
    if len(cleaned.split()) < 2:
        return False
    return True


def _build_rt11_index(rt11_path: Path) -> dict[str, str]:
    """
    Return {opeid8: record_holder_desc} from RT11 CSV.
    First non-empty record_holder_desc per OPEID8 wins.
    """
    idx: dict[str, str] = {}
    with rt11_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            opeid8 = row.get("opeid8", "").strip()
            desc   = (row.get("record_holder_desc") or "").strip()
            if opeid8 and desc and opeid8 not in idx:
                idx[opeid8] = desc
    return idx


def _load_overrides(path: Path) -> dict[str, dict]:
    """
    Load editorial closure-type overrides from a CSV file.
    Returns {opeid8: {"closure_type": ..., "closure_type_basis": ...}}.
    Required columns: opeid8, closure_type, closure_type_basis.
    Optional column: editor_note (ignored by the classifier; for human readers).
    Rows with missing opeid8 or closure_type are skipped with a warning.
    """
    overrides: dict[str, dict] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh), 1):
            opeid8 = row.get("opeid8", "").strip()
            ctype  = row.get("closure_type", "").strip()
            basis  = row.get("closure_type_basis", "").strip()
            if not opeid8 or not ctype:
                print(f"  WARNING: overrides row {i} skipped (missing opeid8 or closure_type)",
                      file=sys.stderr)
                continue
            if ctype not in EVENT_TYPES:
                print(f"  WARNING: overrides row {i} skipped (unknown closure_type {ctype!r})",
                      file=sys.stderr)
                continue
            overrides[opeid8] = {"closure_type": ctype, "closure_type_basis": basis}
    return overrides


def _build_rt06_index(
    rt06_path: Path,
    closed_opeid6s: frozenset[str],
) -> dict[str, dict]:
    """
    Return {prev_opeid8: info_dict} for RT06 records where OPEID6 actually changed.

    is_merger_candidate = True when the new institution's OPEID6 is NOT in the
    closure dataset, meaning the absorbing institution is still active.  When the
    new OPEID6 is also closed (chain collapse), the record is not a merger.

    First OPEID6-change record per prev_opeid8 wins (file is sorted by OPEID).
    coa_actn_cd='4' records are excluded; they are location-suffix renumbers, not
    institutional affiliation changes (even the one edge case where OPEID6 differs).
    """
    idx: dict[str, dict] = {}
    with rt06_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("opeid6_changed") != "true":
                continue
            if row.get("coa_actn_cd") == "4":
                continue
            prev8 = row.get("prev_opeid8", "").strip()
            if not prev8 or prev8 in idx:
                continue
            new_opeid6 = row.get("new_opeid6", "").strip()
            idx[prev8] = {
                "new_opeid8":          row.get("new_opeid8", ""),
                "new_opeid6":          new_opeid6,
                "coa_actn_cd":         row.get("coa_actn_cd", ""),
                "effective_date_iso":  row.get("effective_date_iso", ""),
                "is_merger_candidate": new_opeid6 not in closed_opeid6s,
            }
    return idx


def _build_multi_closure_opeid6s(records: list[dict]) -> frozenset[str]:
    """
    Return OPEID6s where >1 distinct OPEID8 appears in the closure dataset
    AND the range of their closure years fits within CONSOLIDATION_WINDOW_YEARS.
    These suggest a whole-system shutdown rather than isolated branch events.
    """
    opeid6_data: dict[str, dict] = defaultdict(lambda: {"opeid8s": set(), "years": set()})
    for r in records:
        opeid6 = r.get("opeid6", "")
        opeid8 = r.get("opeid8", "")
        yr_str = r.get("closed_year", "")
        if opeid6 and opeid8:
            opeid6_data[opeid6]["opeid8s"].add(opeid8)
            if yr_str.isdigit():
                opeid6_data[opeid6]["years"].add(int(yr_str))

    result: set[str] = set()
    for opeid6, data in opeid6_data.items():
        if len(data["opeid8s"]) <= 1:
            continue
        years = data["years"]
        if years and (max(years) - min(years)) <= CONSOLIDATION_WINDOW_YEARS:
            result.add(opeid6)
    return frozenset(result)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_records(
    records: list[dict],
    rt06_idx: dict[str, dict] | None = None,
    rt11_idx: dict[str, str] | None = None,
    overrides: dict[str, dict] | None = None,
) -> list[dict]:
    """
    Apply event classification rules to enriched closure records.
    Returns a new list with closure_type and closure_type_basis added.
    Rules applied in priority order; first match wins.
    Editorial overrides (from data/manual/closure_type_overrides.csv) are applied
    after all automatic rules and supersede them.
    """
    multi_opeid6s = _build_multi_closure_opeid6s(records)
    classified: list[dict] = []

    for r in records:
        opeid8        = r.get("opeid8", "")
        opeid6        = r.get("opeid6", "")
        is_main       = r.get("is_main_campus") == "true"
        is_suspicious = r.get("is_suspicious_location") == "true"
        is_traditional = r.get("is_traditional_college") == "true"
        control       = r.get("control_label", "")

        closure_type = "unclassified"
        basis        = ""

        # Rule 1: sub-unit or administrative location (highest priority)
        if is_suspicious:
            closure_type = "location_administrative"
            basis        = "location_name matched SHEEO sub-unit keyword pattern"

        # Rule 2: branch campus — parent system may still be open
        elif not is_main:
            closure_type = "branch_closure"
            basis        = "OPEID8 suffix != '00' (not last/only campus of its system)"

        # Rule 3: merger/absorption — RT11 record_holder_desc names another institution.
        # RT06 OPEID-change records are NOT used here: they capture historical corporate-
        # lineage changes (some 40+ years before closure) that produce high false-positive
        # rates and cannot distinguish institutional merger-at-closure from prior acquisition.
        elif rt11_idx is not None and _looks_like_institution(rt11_idx.get(opeid8, "")):
            closure_type = "merger_absorption"
            basis        = f"RT11 record_holder_desc: \"{rt11_idx[opeid8]}\""

        # Rule 4: system consolidation — OPEID6 had multiple campuses all closing
        # (public/nonprofit only; for-profit multi-campus shutdowns → for_profit_closure)
        elif is_main and opeid6 in multi_opeid6s and control != "for-profit":
            closure_type = "system_consolidation"
            basis        = (
                f"OPEID6 {opeid6} has >1 campus in closure dataset "
                f"within {CONSOLIDATION_WINDOW_YEARS}-year window"
            )

        # Rule 5: for-profit operational closure or Title IV exit
        elif is_main and control == "for-profit":
            closure_type = "for_profit_closure"
            basis        = "main campus; control = for-profit"

        # Rule 6: traditional institutional closure (public or nonprofit)
        elif is_main and is_traditional:
            closure_type = "institutional_closure"
            basis        = "main campus; traditional college (public or nonprofit, sector 1-4)"

        # Rule 7: main campus but sector unclassified — treat as institutional
        elif is_main:
            closure_type = "institutional_closure"
            basis        = "main campus; sector unclassified or missing"

        # Editorial override: supersedes all automatic rules
        if overrides and opeid8 in overrides:
            ov = overrides[opeid8]
            closure_type = ov["closure_type"]
            basis        = ov["closure_type_basis"]

        classified.append({**r, "closure_type": closure_type, "closure_type_basis": basis})

    return classified


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _latest_derived(derived_dir: Path, pattern: str) -> Path | None:
    files = sorted(derived_dir.glob(pattern), reverse=True)
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
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify FSA closure records by event type.")
    parser.add_argument(
        "--enriched-csv", type=Path, default=None,
        help="Enriched closure CSV (default: latest fsa_closure_ipeds_enriched_*.csv)",
    )
    parser.add_argument(
        "--rt06-csv", type=Path, default=None,
        help="RT06 CSV for direct OPEID6-change merger detection (optional)",
    )
    parser.add_argument(
        "--rt11-csv", type=Path, default=None,
        help="RT11 CSV for merger/absorption heuristic fallback (optional)",
    )
    parser.add_argument(
        "--overrides-csv", type=Path, default=None,
        help=f"Editorial closure-type overrides CSV (default: {OVERRIDES_DEFAULT})",
    )
    parser.add_argument("--no-overrides", action="store_true",
                        help="Skip loading editorial overrides even if the default file exists")
    parser.add_argument("--out-dir", type=Path, default=DERIVED)
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args(argv)

    enriched_path = args.enriched_csv or _latest_derived(
        args.out_dir, "fsa_closure_ipeds_enriched_*.csv"
    )
    if enriched_path is None or not enriched_path.exists():
        print(
            "ERROR: No enriched closure CSV found.\n"
            "       Run build_ipeds_crosswalk.py first.",
            file=sys.stderr,
        )
        return 1

    print(f"Loading enriched closure records from {enriched_path.name} ...")
    records = _load_csv(enriched_path)
    print(f"  {len(records):,} records")

    # Build set of closed OPEID6s — used to identify chain collapses in RT06
    closed_opeid6s: frozenset[str] = frozenset(
        r.get("opeid6", "") for r in records if r.get("opeid6")
    )

    rt06_idx: dict[str, dict] | None = None
    rt06_path = args.rt06_csv or _latest_derived(args.out_dir, "fsa_wsf_rt06_*.csv")
    if rt06_path and rt06_path.exists():
        print(f"Loading RT06 affiliation-change data from {rt06_path.name} ...")
        rt06_idx = _build_rt06_index(rt06_path, closed_opeid6s)
        merger_candidates = sum(1 for v in rt06_idx.values() if v["is_merger_candidate"])
        chain_collapses   = len(rt06_idx) - merger_candidates
        print(f"  {len(rt06_idx):,} OPEID8s with OPEID6 change "
              f"({merger_candidates:,} merger candidates, {chain_collapses:,} chain-collapse exclusions)")
    else:
        print("No RT06 CSV found; RT06 merger detection disabled.")

    rt11_idx: dict[str, str] | None = None
    rt11_path = args.rt11_csv or _latest_derived(args.out_dir, "fsa_wsf_rt11_*.csv")
    if rt11_path and rt11_path.exists():
        print(f"Loading RT11 record-holder data from {rt11_path.name} ...")
        rt11_idx = _build_rt11_index(rt11_path)
        print(f"  {len(rt11_idx):,} OPEID8s with record_holder_desc")
    else:
        print("No RT11 CSV found; RT11 merger heuristic disabled.")

    overrides: dict[str, dict] | None = None
    if not args.no_overrides:
        overrides_path = args.overrides_csv or OVERRIDES_DEFAULT
        if overrides_path.exists():
            print(f"Loading editorial overrides from {overrides_path.name} ...")
            overrides = _load_overrides(overrides_path)
            print(f"  {len(overrides):,} OPEID8 overrides")
        else:
            print("No editorial overrides file found; skipping.")

    print("Classifying ...")
    classified = classify_records(records, rt06_idx, rt11_idx, overrides)

    out_path = args.out_dir / f"fsa_closure_events_{args.date}.csv"
    _write_csv(classified, EVENTS_FIELDS, out_path)

    type_counts = Counter(r["closure_type"] for r in classified)
    control_x_type = Counter(
        (r.get("control_label", "unknown"), r["closure_type"])
        for r in classified
    )

    overrides_path_used = (args.overrides_csv or OVERRIDES_DEFAULT) if not args.no_overrides else None
    summary = {
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        "methodology_version": "1.2",
        "source_file":         enriched_path.name,
        "rt06_file":           rt06_path.name if (rt06_path and rt06_path.exists()) else None,
        "rt11_file":           rt11_path.name if (rt11_path and rt11_path.exists()) else None,
        "overrides_file":      overrides_path_used.name if (overrides_path_used and overrides_path_used.exists()) else None,
        "overrides_count":     len(overrides) if overrides else 0,
        "total_records":       len(classified),
        "closure_type_counts": dict(type_counts.most_common()),
        "closure_type_by_control": {
            f"{ctrl}|{ct}": n
            for (ctrl, ct), n in sorted(control_x_type.items())
        },
        "event_type_notes": {
            "location_administrative": "Sub-unit flag (not removed); verify before publishing",
            "branch_closure":          "OPEID suffix != 00; parent system may still be open",
            "merger_absorption":       "RT11 record_holder_desc heuristic — requires human confirmation",
            "system_consolidation":    (
                f"OPEID6 had >1 campus in closure dataset within "
                f"{CONSOLIDATION_WINDOW_YEARS}-yr window; may undercount (only detects "
                "consolidations where all campuses appear in FSA list)"
            ),
            "for_profit_closure":      "Main campus; for-profit control",
            "institutional_closure":   "Main campus; public or nonprofit (traditional college)",
            "unclassified":            "No parseable closure date, no IPEDS, or catch-all",
        },
    }
    summary_path = args.out_dir / f"closure_events_summary_{args.date}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\nClassification results ({len(classified):,} records):")
    for ct in EVENT_TYPES:
        n = type_counts.get(ct, 0)
        if n:
            print(f"  {ct:<26} {n:>6,}")

    merger_note = ""
    if rt06_idx is None and rt11_idx is None:
        merger_note = "  (merger_absorption requires --rt06-csv or --rt11-csv)"
    print(f"\nOutputs written:")
    print(f"  {out_path}{merger_note}")
    print(f"  {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
