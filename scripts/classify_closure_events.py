"""
Phase 8: Classify each FSA closure candidate and attach publication evidence.

Reads:  data/derived/fsa_closure_ipeds_enriched_<date>.csv  (latest by default)
        data/derived/fsa_wsf_rt01_<date>.csv                (location status/reinstatement)
        data/derived/fsa_wsf_rt06_<date>.csv                (identifier relationships)
        data/derived/fsa_wsf_rt11_<date>.csv                (record custody only)

Writes: data/derived/fsa_closure_events_<date>.csv
        data/derived/closure_events_summary_<date>.json

Automatic labels are candidates, not editorial findings.  An FSA close record
proves that a federal location record closed; it does not prove that instruction
ceased.  Two paths lead to publication:

  * auto_resolved — the event is in the frozen base table
    (data/manual/auto_publish_baseline.csv) AND meets the automatic
    structured-evidence standard (see _auto_decision).
  * resolved — an event-specific editorial override sets the facts and
    decision.  Overrides always take precedence.

Every other candidate, including any event first seen after the base table was
frozen, has review_required=true and is excluded from published output.

RT11 is never merger evidence: its field is the organization holding closed-
school records.  RT06 can only create an exact-replacement review signal when a
dated old-to-new relationship is close to the event and the replacement OPEID8
is active in RT01.  Names and addresses are not used to make that decision.

Usage (from project root):
  python scripts/classify_closure_events.py
  python scripts/classify_closure_events.py \\
      --rt06-csv data/derived/fsa_wsf_rt06_2026-11-05.csv \\
      --rt11-csv data/derived/fsa_wsf_rt11_2026-11-05.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
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
# Frozen base table: event IDs eligible for automatic publication. Events that
# first appear after the freeze always go to editorial review, even when they
# meet the automatic evidence standard. Keyed by event ID (not date) because
# FSA can add records with backdated close dates.
AUTO_BASELINE_DEFAULT = MANUAL / "auto_publish_baseline.csv"
# Editorially confirmed closures from the Financial Health tracker
# (scripts/fetch_financial_health_closures.py).
FH_CLOSURES_DEFAULT = MANUAL / "external" / "financial_health_closures.csv"

# When an OPEID6 has closed campuses spanning more than this many years,
# we don't call it a consolidation — it's more likely separate closure events.
CONSOLIDATION_WINDOW_YEARS = 5

# A structured identifier relationship is relevant to the closure only when
# its effective date is near the FSA event.  The real RT06 file contains many
# relationships from decades before the eventual close.
REPLACEMENT_EVENT_WINDOW_DAYS = 730

# Micronesia, Marshall Islands, Palau: sovereign nations in free association
# with the U.S., not territories. FSA codes them as states with no country.
FREELY_ASSOCIATED_STATES = frozenset({"FM", "MH", "PW"})

EVENT_TYPES = (
    "location_administrative",
    "branch_closure",
    "merger_absorption",
    "system_consolidation",
    "institutional_closure",
    "title_iv_withdrawal",
    "reinstated",
    "active",
    "identifier_replacement",
    "affiliation_or_governance_change",
    "location_closed_with_institution",
    "location_not_in_ipeds",
    "location_combined_in_ipeds",
    "public_career_tech_program",
    "foreign_location",
    "unclassified",
)

# The first classifier used a separate closure type for for-profit schools.
# Ownership now lives in control_label, while every whole-school closure uses
# institutional_closure.  Normalize the old value at input boundaries so
# historical editorial decisions remain readable without emitting it again.
LEGACY_EVENT_TYPE_ALIASES = {
    "for_profit_closure": "institutional_closure",
}


def normalize_closure_type(value: object) -> str:
    closure_type = str(value or "").strip()
    return LEGACY_EVENT_TYPE_ALIASES.get(closure_type, closure_type)


REVIEW_STATUSES = frozenset({"pending", "resolved", "needs_more_evidence", "needs_re_review"})
# Pipeline-only status for base-table rows that meet the automatic
# structured-evidence standard. Editors never set it; a resolved override
# always takes precedence.
AUTO_REVIEW_STATUS = "auto_resolved"
PUBLICATION_DECISIONS = frozenset({"include_institution", "include_campus", "exclude"})

EVENTS_FIELDS = ENRICHED_FIELDS + [
    "closure_event_id",
    "fsa_closed_date_iso",
    "verified_closed_date_iso",
    "published_closed_date_iso",
    "date_basis",
    "date_evidence_url",
    "location_operations_ceased",
    "operations_ceased_basis",
    "parent_continued",
    "parent_continued_basis",
    "candidate_closure_scope",
    "replacement_opeid8",
    "replacement_opeid6",
    "replacement_effective_date_iso",
    "record_holder_desc",
    "review_required",
    "review_reason",
    "review_status",
    "publication_decision",
    "evidence_fingerprint",
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
    Legacy review helper retained for compatibility.

    Its result must never determine merger/closure classification. RT11's
    record_holder_desc identifies a records custodian, not a successor.
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


def _parse_iso(value: str) -> date | None:
    value = (value or "").strip()
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _closure_event_id(row: dict) -> str:
    """Stable source-event key; editorial date corrections do not change it."""
    opeid8 = (row.get("opeid8") or "").strip()
    source_date = (row.get("closed_date_iso") or "unknown-date").strip()
    return f"fsa:{opeid8}:{source_date}"


def _evidence_fingerprint(row: dict, rt01_row: dict | None, replacement: dict | None) -> str:
    """Hash only source facts whose change should force editorial re-review."""
    payload = {
        "opeid8": row.get("opeid8", ""),
        "opeid6": row.get("opeid6", ""),
        "fsa_closed_date_iso": row.get("closed_date_iso", ""),
        "location_suffix": row.get("location_suffix", ""),
        "is_main_campus": row.get("is_main_campus", ""),
        "match_method": row.get("match_method", ""),
        "unitid": row.get("unitid", ""),
        "rt01_close_date_iso": (rt01_row or {}).get("close_date_iso", ""),
        "rt01_reinstate_date_iso": (rt01_row or {}).get("reinstate_date_iso", ""),
        "replacement_opeid8": (replacement or {}).get("new_opeid8", ""),
        "replacement_effective_date_iso": (replacement or {}).get("effective_date_iso", ""),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _build_rt01_indexes(rt01_path: Path) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Return exact OPEID8 and OPEID6-group indexes from the Weekly School File."""
    by_opeid8: dict[str, dict] = {}
    by_opeid6: dict[str, list[dict]] = defaultdict(list)
    with rt01_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            opeid8 = (row.get("opeid8") or "").strip()
            opeid6 = (row.get("opeid6") or "").strip()
            if not opeid8:
                continue
            by_opeid8.setdefault(opeid8, row)
            if opeid6:
                by_opeid6[opeid6].append(row)
    return by_opeid8, dict(by_opeid6)


def _active_at(row: dict, when: date) -> bool:
    """Whether an RT01 location is active on a date, using close/reinstate facts."""
    close_date = _parse_iso(row.get("close_date_iso", ""))
    reinstate_date = _parse_iso(row.get("reinstate_date_iso", ""))
    if close_date is None:
        return True
    if close_date > when:
        return True
    return bool(reinstate_date and close_date < reinstate_date <= when)


def _is_reinstated(rt01_row: dict | None, event_date: date | None) -> bool:
    if not rt01_row or event_date is None:
        return False
    reinstate_date = _parse_iso(rt01_row.get("reinstate_date_iso", ""))
    close_date = _parse_iso(rt01_row.get("close_date_iso", "")) or event_date
    return bool(reinstate_date and reinstate_date > close_date and reinstate_date >= event_date)


def _parent_continuation(
    row: dict,
    rt01_by_opeid6: dict[str, list[dict]] | None,
) -> tuple[str, str]:
    """Derive tri-state parent continuation from exact sibling RT01 statuses."""
    event_date = _parse_iso(row.get("closed_date_iso", ""))
    opeid6 = (row.get("opeid6") or "").strip()
    opeid8 = (row.get("opeid8") or "").strip()
    if event_date is None or not opeid6 or not rt01_by_opeid6:
        return "unknown", "No dated RT01 sibling evidence available"

    siblings = [r for r in rt01_by_opeid6.get(opeid6, []) if r.get("opeid8") != opeid8]
    active = [r for r in siblings if _active_at(r, event_date)]
    if active:
        ids = ",".join(sorted(r.get("opeid8", "") for r in active)[:5])
        return "true", f"RT01 sibling location active on event date: {ids}"

    # For a main location, a complete RT01 group with no active sibling supports
    # (but does not by itself prove) that no parent operation continued.
    if row.get("is_main_campus") == "true" and rt01_by_opeid6.get(opeid6):
        return "false", "RT01 contains no other location active on event date"
    return "unknown", "No active RT01 sibling found; absence is not proof of parent cessation"


def _qualifying_replacement(
    row: dict,
    rt06_idx: dict[str, dict] | None,
    rt01_by_opeid8: dict[str, dict] | None,
) -> dict | None:
    """Return a dated, active exact replacement relation or None."""
    if not rt06_idx or not rt01_by_opeid8:
        return None
    rel = rt06_idx.get((row.get("opeid8") or "").strip())
    if not rel:
        return None
    event_date = _parse_iso(row.get("closed_date_iso", ""))
    effective_date = _parse_iso(rel.get("effective_date_iso", ""))
    new_opeid8 = (rel.get("new_opeid8") or "").strip()
    successor = rt01_by_opeid8.get(new_opeid8)
    if not event_date or not effective_date or not successor:
        return None
    if abs((event_date - effective_date).days) > REPLACEMENT_EVENT_WINDOW_DAYS:
        return None
    if not _active_at(successor, max(event_date, effective_date)):
        return None
    return rel


def _load_overrides(path: Path) -> dict[str, dict]:
    """
    Load resolved editorial decisions from the combined review/overrides CSV.

    New rows are keyed by closure_event_id. Legacy OPEID8-only rows remain
    readable during migration and are keyed as ``legacy:<opeid8>``. Pending or
    needs-review rows are intentionally not returned as active overrides.
    """
    overrides: dict[str, dict] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh), 1):
            raw_opeid8 = (row.get("opeid8") or "").strip()
            opeid8 = raw_opeid8.zfill(8) if raw_opeid8 else ""
            event_id = (row.get("closure_event_id") or "").strip()
            ctype = normalize_closure_type(row.get("closure_type"))
            basis = (row.get("closure_type_basis") or "").strip()
            status = (row.get("review_status") or "").strip()

            # Backward-compatible migration: old files contained decisions only.
            if not status and ctype:
                status = "resolved"
            elif not status and (opeid8 or event_id) and not ctype:
                print(f"  WARNING: overrides row {i} skipped (legacy row missing closure_type)",
                      file=sys.stderr)
                continue
            if status != "resolved":
                continue
            if not (event_id or opeid8) or not ctype or not basis:
                print(f"  WARNING: overrides row {i} skipped (resolved row missing event/OPEID, type, or basis)",
                      file=sys.stderr)
                continue
            if ctype not in EVENT_TYPES:
                print(f"  WARNING: overrides row {i} skipped (unknown closure_type {ctype!r})",
                      file=sys.stderr)
                continue

            decision = (row.get("publication_decision") or "").strip()
            if not decision:
                if ctype in {"institutional_closure", "system_consolidation"}:
                    decision = "include_institution"
                elif ctype == "branch_closure":
                    decision = "include_campus"
                else:
                    decision = "exclude"
            if decision not in PUBLICATION_DECISIONS:
                print(f"  WARNING: overrides row {i} skipped (unknown publication_decision {decision!r})",
                      file=sys.stderr)
                continue

            value = {
                "closure_type": ctype,
                "closure_type_basis": basis,
                "review_status": status,
                "publication_decision": decision,
                "verified_closed_date_iso": (row.get("verified_closed_date_iso") or "").strip(),
                "date_basis": (row.get("date_basis") or "").strip(),
                "date_evidence_url": (row.get("date_evidence_url") or "").strip(),
                "location_operations_ceased": (row.get("location_operations_ceased") or "").strip(),
                "operations_ceased_basis": (row.get("operations_ceased_basis") or "").strip(),
                "parent_continued": (row.get("parent_continued") or "").strip(),
                "parent_continued_basis": (row.get("parent_continued_basis") or "").strip(),
                "evidence_fingerprint": (row.get("evidence_fingerprint") or "").strip(),
            }
            key = event_id or f"legacy:{opeid8}"
            overrides[key] = value
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


def _auto_decision(
    closure_type: str,
    parent_continued: str,
    match_method: str,
    review_reasons: list[str],
) -> str:
    """
    Return include_campus/include_institution when a candidate meets the
    automatic structured-evidence standard, else "".

    Campus: branch_closure (exact IPEDS identity, postsecondary ICLEVEL, RT01
    sibling active on the event date).  Institution: main-location candidate
    with exact IPEDS identity and no RT01 location active on the event date.
    Reinstatements, exact replacements, and name-pattern rows never qualify
    because they carry other closure types.
    """
    if not match_method.startswith("matched_") or "ambiguous_ipeds_match" in review_reasons:
        return ""
    if "ipeds_status_combined_with_other_institution" in review_reasons:
        return ""  # possible merger: an editor decides
    if "ipeds_parent_inactive_after_closure" in review_reasons:
        return ""  # parent ended without a nearby main-campus close: editor decides
    if closure_type == "branch_closure" and parent_continued == "true":
        return "include_campus"
    if closure_type == "institutional_closure" and parent_continued == "false":
        return "include_institution"
    return ""


def _load_fh_closures(path: Path) -> dict[str, list[dict]]:
    """Return {unitid: [confirmed institution_closure rows]} from the FH snapshot."""
    if not path.exists():
        return {}
    out: dict[str, list[dict]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("cut_type") == "institution_closure" and row.get("status") == "confirmed":
                out[(row.get("unitid") or "").strip()].append(row)
    return dict(out)


def _institution_ceased_evidence(
    row: dict,
    parent_continued: str,
    parent_basis: str,
    fh_closures: dict[str, list[dict]] | None,
) -> tuple[str, str, str]:
    """
    Second look at a main-location candidate whose RT01 group still lists an
    open location.  RT01 keeps stale location rows for defunct schools (ITT,
    Brightwood, Bethany all show "open" locations years after closing), so an
    open RT01 row is not proof the institution continued.

    Returns (parent_continued, parent_basis, extra_review_reason).  Sets
    parent_continued=false when either:
      * IPEDS: no institution under this OPEID6 reports to IPEDS after the
        closure year, and a later IPEDS year exists to check; or
      * the Financial Health tracker has an editorially confirmed
        institution_closure for this UNITID announced within 3 years before
        (or 1 year after) the FSA close date.
    """
    closed = _parse_iso(row.get("closed_date_iso", ""))
    year = row.get("closed_year", "")

    unitid = (row.get("unitid") or "").strip()
    for fh in (fh_closures or {}).get(unitid, []):
        announced = _parse_iso(fh.get("announcement_date", ""))
        if closed and announced and -365 <= (closed - announced).days <= 3 * 365:
            return (
                "false",
                f"Financial Health tracker confirmed institution closure announced "
                f"{fh.get('announcement_date')}: {fh.get('source_url', '')}",
                "fh_confirmed_institution_closure",
            )

    last = row.get("ipeds_opeid6_last_year", "")
    max_year = row.get("ipeds_cache_max_year", "")
    if year.isdigit() and last.isdigit() and max_year.isdigit():
        if int(year) < int(max_year) and int(last) <= int(year):
            return (
                "false",
                f"No institution under OPEID6 {row.get('opeid6', '')} reports to IPEDS after "
                f"{year} (IPEDS checked through {max_year}); the open RT01 location is stale",
                "",
            )
        if int(last) > int(year):
            return (
                parent_continued,
                f"{parent_basis}; OPEID6 still reports to IPEDS in {last}",
                "ipeds_reporting_continued_after_closure",
            )
    return parent_continued, parent_basis, ""


def _ipeds_parent_gone(row: dict) -> bool:
    """True when no institution under the OPEID6 is active in IPEDS after the
    closure year, and a later IPEDS year exists to check."""
    year = row.get("closed_year", "")
    last = row.get("ipeds_opeid6_last_year", "")
    max_year = row.get("ipeds_cache_max_year", "")
    return (year.isdigit() and last.isdigit() and max_year.isdigit()
            and int(year) < int(max_year) and int(last) <= int(year))


def _main_closed_near(main_dates, event_date: date | None, days: int = 365) -> bool:
    """True when the OPEID6's main campus ("00") closed within `days` of the event."""
    return bool(event_date) and any(abs((d - event_date).days) <= days for d in main_dates)


def _auto_exclusion(row: dict, is_main: bool, closure_type: str, review_reasons: list[str]) -> tuple[str, str] | None:
    """Return (closure_type, basis) for rows that can never be published, else None."""
    if (row.get("country") or "").strip() or (row.get("state") or "").strip() in FREELY_ASSOCIATED_STATES:
        # U.S. territories (PR, GU, VI, AS, MP) are in scope; the freely
        # associated states are sovereign nations, though FSA gives them a
        # state code and no country.
        return "foreign_location", "Location outside the United States and its territories; out of scope"
    if closure_type == "location_closed_with_institution":
        return closure_type, (
            "Location closed with its institution (main campus closed within a year; parent not active "
            "in IPEDS afterwards); counted once via the institution closure")
    if closure_type == "identifier_replacement":
        return closure_type, "Exact replacement identifier active in RT01; operations continued under it"
    if closure_type in {"public_career_tech_program", "location_administrative"}:
        return closure_type, (
            "Public less-than-2-year program (IPEDS sector 7), typically run by a school district or "
            "career-tech center; not counted as a college closure"
            if closure_type == "public_career_tech_program"
            else "IPEDS administrative unit (sector 0), not an instructional institution")
    if not is_main and "postsecondary_location_identity_unresolved" in review_reasons:
        return "location_not_in_ipeds", (
            "Additional location with no IPEDS record of its own; cannot be verified as a campus, "
            "so not counted as a campus closure")
    if not is_main and "ipeds_status_combined_with_other_institution" in review_reasons:
        return "location_combined_in_ipeds", (
            "IPEDS records this unit as combined with another institution; the location likely "
            "continued under it, so not counted as a campus closure")
    return None


def _load_auto_baseline(path: Path) -> frozenset[str]:
    """Return the frozen set of base-table event IDs (empty if no file)."""
    if not path.exists():
        return frozenset()
    with path.open(encoding="utf-8", newline="") as fh:
        return frozenset(
            (row.get("closure_event_id") or "").strip()
            for row in csv.DictReader(fh)
            if (row.get("closure_event_id") or "").strip()
        )


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def classify_records(
    records: list[dict],
    rt06_idx: dict[str, dict] | None = None,
    rt11_idx: dict[str, str] | None = None,
    overrides: dict[str, dict] | None = None,
    rt01_by_opeid8: dict[str, dict] | None = None,
    rt01_by_opeid6: dict[str, list[dict]] | None = None,
    auto_baseline: frozenset[str] | None = None,
    fh_closures: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """
    Attach candidate labels, structured evidence, and editorial decisions.

    Base-table events (auto_baseline) that meet the automatic evidence
    standard are auto_resolved. Every other candidate, including any event
    first seen after the baseline freeze, stays review_required=true. A
    resolved event-specific override always takes precedence.
    """
    multi_opeid6s = _build_multi_closure_opeid6s(records)
    main_close_dates: dict[str, list[date]] = defaultdict(list)
    for rec in records:
        main_date = _parse_iso(rec.get("closed_date_iso", ""))
        if rec.get("location_suffix") == "00" and main_date:
            main_close_dates[rec.get("opeid6", "")].append(main_date)
    classified: list[dict] = []

    for r in records:
        opeid8        = r.get("opeid8", "")
        opeid6        = r.get("opeid6", "")
        is_main       = r.get("is_main_campus") == "true"
        is_suspicious = r.get("is_suspicious_location") == "true"
        is_traditional = r.get("is_traditional_college") == "true"
        control       = r.get("control_label", "")

        event_id = _closure_event_id(r)
        event_date = _parse_iso(r.get("closed_date_iso", ""))
        rt01_row = (rt01_by_opeid8 or {}).get(opeid8)
        reinstated = _is_reinstated(rt01_row, event_date)
        parent_continued, parent_basis = _parent_continuation(r, rt01_by_opeid6)
        replacement = _qualifying_replacement(r, rt06_idx, rt01_by_opeid8)
        fingerprint = _evidence_fingerprint(r, rt01_row, replacement)
        record_holder = (rt11_idx or {}).get(opeid8, "")

        closure_type = "unclassified"
        basis = "FSA closed-school record; operational closure not yet verified"
        candidate_scope = "institution" if is_main else "campus"
        location_ceased = "unknown"
        operations_basis = "FSA establishes a closed federal location record, not cessation of instruction"
        review_status = "pending"
        publication_decision = ""
        review_reasons: list[str] = []

        if reinstated:
            closure_type = "reinstated"
            basis = "RT01 reinstatement date reverses the FSA close event"
            location_ceased = "false"
            operations_basis = basis
            review_status = "resolved"
            publication_decision = "exclude"
        elif replacement:
            closure_type = "identifier_replacement"
            basis = (
                f"RT06 exact OPEID relationship to {replacement.get('new_opeid8', '')} "
                f"effective {replacement.get('effective_date_iso', '')}; replacement active in RT01"
            )
            review_reasons.append("exact_identifier_replacement_needs_operational_review")
        elif is_suspicious:
            # Text patterns are triage only and cannot determine whether a site
            # was administrative, secondary-school, or postsecondary.
            closure_type = "unclassified"
            basis = "Location-name pattern triggered review; name text is not classification evidence"
            review_reasons.append("location_name_requires_review")
        elif not is_main:
            matched = r.get("match_method", "").startswith("matched_")
            # A campus closure needs the LOCATION's own IPEDS record (its own
            # OPEID8 in IPEDS), not just the parent's.
            own_record = r.get("match_method", "").startswith("matched_opeid8")
            postsecondary = r.get("iclevel", "") in {"1", "2", "3"}
            parent_gone = _ipeds_parent_gone(r)
            last_active = r.get("ipeds_opeid6_last_year", "")
            if (matched and postsecondary and parent_continued == "unknown" and last_active.isdigit()
                    and r.get("closed_year", "").isdigit() and int(last_active) > int(r["closed_year"])):
                # No open RT01 sibling on file, but the parent kept reporting
                # to IPEDS as active: structured evidence it continued.
                parent_continued = "true"
                parent_basis = f"No open RT01 sibling on file, but OPEID6 {opeid6} active in IPEDS in {last_active}"
            if parent_continued in {"true", "unknown"} and matched and postsecondary and parent_gone and _main_closed_near(
                main_close_dates.get(opeid6, ()), event_date,
            ):
                # Chain shutdown: RT01's "open" sibling is stale (or absent).
                # The location closed as part of an institution closure, which
                # the main-campus row already counts.
                closure_type = "location_closed_with_institution"
                basis = (
                    f"Location closed with its institution: main campus of OPEID6 {opeid6} closed within "
                    f"a year and no institution under it is active in IPEDS after {r.get('closed_year', '')}"
                )
                parent_continued = "false"
                parent_basis = "No continuing parent: IPEDS shows the institution ceased"
                location_ceased = "true"
            elif parent_continued == "true" and own_record and postsecondary:
                closure_type = "branch_closure"
                basis = "Campus candidate: location's own IPEDS record and evidence the parent continued"
                if parent_gone:
                    # Parent ended without a nearby main-campus close: editor decides.
                    review_reasons.append("ipeds_parent_inactive_after_closure")
            else:
                closure_type = "unclassified"
                basis = "Additional-location candidate lacks enough structured evidence for campus closure"
                if parent_continued != "true":
                    review_reasons.append("parent_continuation_unknown")
                if not (own_record and postsecondary):
                    # Matching only the PARENT's IPEDS record (via OPEID6) does
                    # not make a location a campus: these are mostly extension
                    # and teaching sites, including K-12 dual-enrollment sites.
                    review_reasons.append("postsecondary_location_identity_unresolved")
        elif control == "for-profit":
            closure_type = "institutional_closure"
            basis = "Main-location closure candidate; historical IPEDS control = for-profit"
        elif is_traditional:
            closure_type = "institutional_closure"
            basis = "Main-location closure candidate; historical IPEDS public/nonprofit identity"
        elif r.get("sector") == "8":
            closure_type = "institutional_closure"
            basis = "Main-location closure candidate; historical IPEDS nonprofit less-than-2-year institution"
        elif r.get("sector") == "7":
            # Public less-than-2-year: overwhelmingly adult programs run by K-12
            # districts, BOCES and career-tech centers (Kelchen et al. note the
            # same). Not counted as college closures.
            closure_type = "public_career_tech_program"
            basis = ("Public less-than-2-year program (IPEDS sector 7), typically run by a school district "
                     "or career-tech center; not counted as a college closure")
        elif r.get("sector") == "0":
            closure_type = "location_administrative"
            basis = "IPEDS administrative unit (sector 0), not an instructional institution"
        else:
            closure_type = "unclassified"
            basis = "Main-location candidate lacks resolved historical IPEDS identity"
            review_reasons.append("institution_identity_unresolved")

        if closure_type == "institutional_closure" and parent_continued != "false":
            # Financial Health evidence is a one-time input for the base table
            # only; closures first seen after the freeze get plain editor review.
            in_base = event_id in (auto_baseline or frozenset())
            parent_continued, parent_basis, extra_reason = _institution_ceased_evidence(
                r, parent_continued, parent_basis, fh_closures if in_base else None,
            )
            if extra_reason:
                review_reasons.append(extra_reason)

        if not reinstated:
            review_reasons.append("operational_closure_unverified")
        if opeid6 in multi_opeid6s:
            review_reasons.append("possible_system_consolidation")
        if r.get("match_method", "").startswith("review_"):
            review_reasons.append("ambiguous_ipeds_match")
        if r.get("ipeds_combined_after_closure") == "true":
            # IPEDS STATUS=C: combined with another institution — merger candidate.
            review_reasons.append("ipeds_status_combined_with_other_institution")

        # RT11 is retained solely as record-custody context. Its presence never
        # changes closure_type or publication_decision.
        if record_holder:
            review_reasons.append("record_custodian_available")

        review_required = "false" if reinstated else "true"

        # Rows that can never be published under the rule. Base-table rows
        # are excluded automatically; rows first seen after the freeze go to
        # editors with the suggested exclusion attached.
        exclusion = "" if reinstated else _auto_exclusion(r, is_main, closure_type, review_reasons)
        if exclusion:
            excl_type, excl_basis = exclusion
            # Foreign locations are a scope rule, not an editorial judgment:
            # excluded even when new since the base table.
            if event_id in (auto_baseline or frozenset()) or excl_type == "foreign_location":
                closure_type = excl_type
                review_required = "false"
                review_status = AUTO_REVIEW_STATUS
                publication_decision = "exclude"
                basis = excl_basis
                review_reasons = [x for x in review_reasons if x != "operational_closure_unverified"]
            else:
                basis = f"{excl_basis} (suggested; new since the base table — editor must confirm)"
                review_reasons.append(f"suggested_exclude_{excl_type}")
        auto = "" if exclusion else _auto_decision(closure_type, parent_continued, r.get("match_method", ""), review_reasons)
        if auto and event_id in (auto_baseline or frozenset()):
            review_required = "false"
            review_status = AUTO_REVIEW_STATUS
            publication_decision = auto
            location_ceased = "true"
            operations_basis = (
                "Automatic base-table tier: FSA closed-location record, exact IPEDS identity, "
                "dated RT01 sibling evidence, no reinstatement or exact replacement"
            )
            review_reasons = [x for x in review_reasons if x != "operational_closure_unverified"]
        elif auto:
            review_reasons.append("new_since_baseline")
            basis = f"{basis}; meets automatic standard but is new since the base table — editor must confirm"
        ov = None
        if overrides:
            ov = overrides.get(event_id) or overrides.get(f"legacy:{opeid8}")
        if ov:
            saved_fingerprint = ov.get("evidence_fingerprint", "")
            if saved_fingerprint and saved_fingerprint != fingerprint:
                review_status = "needs_re_review"
                review_required = "true"
                review_reasons.append("source_evidence_changed")
            else:
                closure_type = ov["closure_type"]
                basis = ov["closure_type_basis"]
                review_status = "resolved"
                review_required = "false"
                publication_decision = ov["publication_decision"]
                location_ceased = ov.get("location_operations_ceased") or location_ceased
                operations_basis = ov.get("operations_ceased_basis") or operations_basis
                parent_continued = ov.get("parent_continued") or parent_continued
                parent_basis = ov.get("parent_continued_basis") or parent_basis

                # Safe defaults for legacy resolved rows. New review rows should
                # store these facts explicitly.
                if publication_decision == "include_institution":
                    location_ceased = "true" if location_ceased == "unknown" else location_ceased
                    parent_continued = "false" if parent_continued == "unknown" else parent_continued
                elif publication_decision == "include_campus":
                    location_ceased = "true" if location_ceased == "unknown" else location_ceased
                    parent_continued = "true" if parent_continued == "unknown" else parent_continued

        verified_date = (ov or {}).get("verified_closed_date_iso", "") if ov and review_status == "resolved" else ""
        published_date = verified_date or (r.get("closed_date_iso", "") if publication_decision.startswith("include_") else "")
        date_basis = (ov or {}).get("date_basis", "") if ov and review_status == "resolved" else ""
        date_evidence_url = (ov or {}).get("date_evidence_url", "") if ov and review_status == "resolved" else ""

        classified.append({
            **r,
            "closure_event_id": event_id,
            "fsa_closed_date_iso": r.get("closed_date_iso", ""),
            "verified_closed_date_iso": verified_date,
            "published_closed_date_iso": published_date,
            "date_basis": date_basis,
            "date_evidence_url": date_evidence_url,
            "location_operations_ceased": location_ceased,
            "operations_ceased_basis": operations_basis,
            "parent_continued": parent_continued,
            "parent_continued_basis": parent_basis,
            "candidate_closure_scope": candidate_scope,
            "replacement_opeid8": (replacement or {}).get("new_opeid8", ""),
            "replacement_opeid6": (replacement or {}).get("new_opeid6", ""),
            "replacement_effective_date_iso": (replacement or {}).get("effective_date_iso", ""),
            "record_holder_desc": record_holder,
            "review_required": review_required,
            "review_reason": "|".join(dict.fromkeys(review_reasons)),
            "review_status": review_status,
            "publication_decision": publication_decision,
            "evidence_fingerprint": fingerprint,
            "closure_type": closure_type,
            "closure_type_basis": basis,
        })

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
        "--rt01-csv", type=Path, default=None,
        help="RT01 CSV for location status, sibling activity, and reinstatement evidence",
    )
    parser.add_argument(
        "--rt06-csv", type=Path, default=None,
        help="RT06 CSV for dated exact identifier-relationship review signals",
    )
    parser.add_argument(
        "--rt11-csv", type=Path, default=None,
        help="RT11 CSV for closed-school record-custody context",
    )
    parser.add_argument(
        "--overrides-csv", type=Path, default=None,
        help=f"Editorial closure-type overrides CSV (default: {OVERRIDES_DEFAULT})",
    )
    parser.add_argument("--no-overrides", action="store_true",
                        help="Skip loading editorial overrides even if the default file exists")
    parser.add_argument(
        "--auto-baseline-csv", type=Path, default=AUTO_BASELINE_DEFAULT,
        help=f"Frozen base-table event IDs eligible for automatic publication (default: {AUTO_BASELINE_DEFAULT})",
    )
    parser.add_argument(
        "--freeze-auto-baseline", action="store_true",
        help="One-time: write every currently auto-eligible event ID to --auto-baseline-csv. "
             "Refuses to overwrite an existing baseline.",
    )
    parser.add_argument(
        "--fh-closures-csv", type=Path, default=FH_CLOSURES_DEFAULT,
        help=f"Financial Health tracker confirmed closures (default: {FH_CLOSURES_DEFAULT})",
    )
    parser.add_argument("--out-dir", type=Path, default=DERIVED)
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args(argv)

    if args.freeze_auto_baseline and args.auto_baseline_csv.exists():
        print(
            f"ERROR: {args.auto_baseline_csv} already exists. The base table is frozen;\n"
            "       new closures must go through editorial review. Delete the file\n"
            "       deliberately if you really intend to re-freeze.",
            file=sys.stderr,
        )
        return 1

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

    rt01_by_opeid8: dict[str, dict] | None = None
    rt01_by_opeid6: dict[str, list[dict]] | None = None
    rt01_path = args.rt01_csv or _latest_derived(args.out_dir, "fsa_wsf_rt01_*.csv")
    if rt01_path and rt01_path.exists():
        print(f"Loading RT01 location-status data from {rt01_path.name} ...")
        rt01_by_opeid8, rt01_by_opeid6 = _build_rt01_indexes(rt01_path)
        print(f"  {len(rt01_by_opeid8):,} exact OPEID8 location records")
    else:
        print("No RT01 CSV found; parent-continuation and reinstatement evidence unavailable.")

    # Build set of closed OPEID6s — used to identify chain collapses in RT06
    closed_opeid6s: frozenset[str] = frozenset(
        r.get("opeid6", "") for r in records if r.get("opeid6")
    )

    rt06_idx: dict[str, dict] | None = None
    rt06_path = args.rt06_csv or _latest_derived(args.out_dir, "fsa_wsf_rt06_*.csv")
    if rt06_path and rt06_path.exists():
        print(f"Loading RT06 identifier relationships from {rt06_path.name} ...")
        rt06_idx = _build_rt06_index(rt06_path, closed_opeid6s)
        print(f"  {len(rt06_idx):,} old-to-new OPEID8 relationships (date-gated during classification)")
    else:
        print("No RT06 CSV found; identifier-replacement review signals unavailable.")

    rt11_idx: dict[str, str] | None = None
    rt11_path = args.rt11_csv or _latest_derived(args.out_dir, "fsa_wsf_rt11_*.csv")
    if rt11_path and rt11_path.exists():
        print(f"Loading RT11 record-holder data from {rt11_path.name} ...")
        rt11_idx = _build_rt11_index(rt11_path)
        print(f"  {len(rt11_idx):,} OPEID8s with record_holder_desc")
    else:
        print("No RT11 CSV found; record-custody context unavailable.")

    overrides: dict[str, dict] | None = None
    if not args.no_overrides:
        overrides_path = args.overrides_csv or OVERRIDES_DEFAULT
        if overrides_path.exists():
            print(f"Loading editorial overrides from {overrides_path.name} ...")
            overrides = _load_overrides(overrides_path)
            print(f"  {len(overrides):,} resolved editorial decisions")
        else:
            print("No editorial overrides file found; skipping.")

    if args.freeze_auto_baseline:
        # Every current event is base-table; the freeze records which qualify.
        auto_baseline = frozenset(_closure_event_id(r) for r in records)
    else:
        auto_baseline = _load_auto_baseline(args.auto_baseline_csv)
        print(f"Auto-publish base table: {len(auto_baseline):,} event IDs"
              + ("" if auto_baseline else " (none — every candidate requires review)"))

    fh_closures = _load_fh_closures(args.fh_closures_csv)
    print(f"Financial Health confirmed institution closures: {sum(map(len, fh_closures.values())):,}"
          + ("" if fh_closures else f" (none — {args.fh_closures_csv} missing or empty)"))

    print("Classifying ...")
    classified = classify_records(
        records,
        rt06_idx=rt06_idx,
        rt11_idx=rt11_idx,
        overrides=overrides,
        rt01_by_opeid8=rt01_by_opeid8,
        rt01_by_opeid6=rt01_by_opeid6,
        auto_baseline=auto_baseline,
        fh_closures=fh_closures,
    )

    if args.freeze_auto_baseline:
        frozen = sorted(
            r["closure_event_id"] for r in classified
            if r["review_status"] == AUTO_REVIEW_STATUS
        )
        args.auto_baseline_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.auto_baseline_csv.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["closure_event_id", "frozen_on"])
            writer.writerows([event_id, args.date] for event_id in frozen)
        print(f"  Froze {len(frozen):,} auto-eligible event IDs to {args.auto_baseline_csv}")

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
        "methodology_version": "2.0",
        "source_file":         enriched_path.name,
        "rt01_file":           rt01_path.name if (rt01_path and rt01_path.exists()) else None,
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
            "branch_closure": "Candidate only: exact IPEDS identity plus an active RT01 sibling; requires resolution",
            "institutional_closure": "Candidate only: main location plus historical IPEDS identity; requires resolution",
            "identifier_replacement": "Dated RT06 old-to-new relationship plus active exact RT01 successor; requires review",
            "merger_absorption": "Editorial outcome only; RT11 is never merger evidence",
            "system_consolidation": "Editorial outcome only; clustered OPEID6 closes merely trigger review",
            "reinstated": "RT01 reinstatement reverses the close event; excluded",
            "location_closed_with_institution": "Non-main location of a chain that shut down (main closed within a year, parent gone from IPEDS); counted once via the institution row",
            "unclassified": "Structured evidence is insufficient for a campus or institution candidate",
        },
    }
    summary_path = args.out_dir / f"closure_events_summary_{args.date}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\nClassification results ({len(classified):,} records):")
    for ct in EVENT_TYPES:
        n = type_counts.get(ct, 0)
        if n:
            print(f"  {ct:<26} {n:>6,}")

    print(f"\nOutputs written:")
    print(f"  {out_path}")
    print(f"  {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
