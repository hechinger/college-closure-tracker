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
  Publication is decision-driven. A row must be resolved (editorial) or
  auto_resolved (frozen base table meeting the automatic evidence standard),
  not require review, and carry one of these internally consistent decisions:

  include_institution: location_operations_ceased=true, parent_continued=false
  include_campus:      location_operations_ceased=true, parent_continued=true

  No public scope is inferred from name text, OPEID suffix, IPEDS matching, the
  absence of a parent in the closure file, or RT11 record custody.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import google.auth
import gspread
from google.auth.transport.requests import Request as _AuthRequest

from pull_gsheet_overrides import OVERRIDES_FIELDS

PROJECT = Path(__file__).resolve().parent.parent
DERIVED = PROJECT / "data" / "derived"
LOCAL_OVERRIDES = PROJECT / "data" / "manual" / "closure_type_overrides.csv"
NEW_COUNT_FILE = DERIVED / "new_closures_count.txt"

# ---------------------------------------------------------------------------
# Column definitions
# ---------------------------------------------------------------------------

# Internal review tab: editors see all classification context.
_EDITORIAL_COLS = [
    "opeid8", "closure_event_id", "school_name", "city", "state",
    "control_label", "iclevel_label", "institution_category",
    "program_level", "credential_level",
    "enrollment_band", "enrollment_headcount_total",
    "closed_date_iso", "closed_year", "fsa_closed_date_iso",
    "verified_closed_date_iso", "published_closed_date_iso",
    "date_basis", "date_evidence_url",
    "is_main_campus", "is_suspicious_location",
    "approach2_main_campus_only", "is_traditional_college",
    "closure_type", "closure_type_basis", "candidate_closure_scope",
    "location_operations_ceased", "operations_ceased_basis",
    "parent_continued", "parent_continued_basis",
    "replacement_opeid8", "replacement_effective_date_iso",
    "record_holder_desc", "review_required", "review_reason",
    "review_status", "publication_decision", "evidence_fingerprint",
    "match_method", "unitid", "opeid6",
]

# Public published tab: Datawrapper and consumers read this.
# 12 original columns + 5 publication-metadata columns.
# closure_scope (institution | campus) is the only scope signal needed;
# parent_institution_continued and the include_*/sheeo_approach_* flags
# are all derivable from closure_scope and are omitted.
_PUBLIC_COLS = [
    "opeid8", "school_name", "city", "state",
    "control_label", "institution_category", "program_level", "credential_level",
    "enrollment_band", "enrollment_headcount_total",
    "closed_date_iso", "closed_year", "closure_type",
    "opeid6", "unitid",
    "closure_scope", "parent_continued",
    "classification_basis", "closure_event_id",
    "fsa_closed_date_iso", "verified_closed_date_iso",
    "date_basis", "date_evidence_url",
]

TAB_COLUMNS: dict[str, list[str]] = {
    "closure_list": _EDITORIAL_COLS,
    "published":    _PUBLIC_COLS,
}

_OVERRIDE_PIPELINE_FIELDS = frozenset({
    "closure_event_id", "opeid8", "school_name", "state",
    "fsa_closed_date_iso", "review_reason", "candidate_closure_scope",
    "replacement_opeid8", "replacement_effective_date_iso",
    "evidence_fingerprint",
})

# ---------------------------------------------------------------------------
# Publication filters
# ---------------------------------------------------------------------------

# First closure year in published/by_year/by_state and the review queue.
# 1996 matches Kelchen et al.'s window; IPEDS history starts in 1995.
PUBLISHED_MIN_YEAR = 1996

# Internal classifier types that map to institution-scope published rows.
_INSTITUTION_TYPES = frozenset({
    "institutional_closure",
    "system_consolidation",
    "for_profit_closure",
})

# Internal classifier types that may become campus-scope published rows.
_CAMPUS_TYPES = frozenset({"branch_closure"})

# resolved = editorial decision; auto_resolved = frozen base-table row meeting
# the automatic evidence standard. New closures never get auto_resolved.
_PUBLISHABLE_STATUSES = frozenset({"resolved", "auto_resolved"})

# closure_list is the complete candidate ledger; nothing is silently hidden.
_EXCLUDE_FROM_CLOSURE_LIST = frozenset()

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


def _has_editorial_decision(row: dict | None) -> bool:
    """True when an overrides row carries an editor's decision or note."""
    return bool(row) and any(
        str(row.get(f, "")).strip() for f in ("closure_type", "publication_decision", "editor_note", "reviewed_by")
    )


def _build_overrides_queue(
    events: list[dict],
    existing_rows: list[dict] | None = None,
    min_year: int = PUBLISHED_MIN_YEAR,
) -> list[list[str]]:
    """Merge current review candidates with preserved editorial decisions."""
    existing_rows = existing_rows or []
    existing_by_event: dict[str, dict] = {}
    legacy_rows: list[dict] = []
    for row in existing_rows:
        event_id = str(row.get("closure_event_id", "")).strip()
        if event_id:
            existing_by_event[event_id] = {k: str(v) for k, v in row.items()}
        else:
            legacy_rows.append({k: str(v) for k, v in row.items()})

    merged: list[dict] = []
    seen: set[str] = set()
    for event in events:
        year = str(event.get("closed_year", ""))
        if not year.isdigit() or int(year) < min_year:
            continue
        event_id = str(event.get("closure_event_id", "")).strip()
        if not event_id:
            continue
        if event.get("review_required") != "true" and not _has_editorial_decision(existing_by_event.get(event_id)):
            # Resolved by the pipeline itself (reinstatement, automatic tier or
            # exclusion): no editorial decision to show. Rows an earlier push
            # wrote without a decision are dropped too.
            continue

        old = existing_by_event.get(event_id, {})
        row = {field: str(old.get(field, "")) for field in OVERRIDES_FIELDS}
        pipeline_values = {
            "closure_event_id": event_id,
            "opeid8": event.get("opeid8", ""),
            "school_name": event.get("school_name", ""),
            "state": event.get("state", ""),
            "fsa_closed_date_iso": event.get("fsa_closed_date_iso", "") or event.get("closed_date_iso", ""),
            "review_reason": event.get("review_reason", ""),
            "candidate_closure_scope": event.get("candidate_closure_scope", ""),
            "replacement_opeid8": event.get("replacement_opeid8", ""),
            "replacement_effective_date_iso": event.get("replacement_effective_date_iso", ""),
            "evidence_fingerprint": event.get("evidence_fingerprint", ""),
        }
        for field in _OVERRIDE_PIPELINE_FIELDS:
            row[field] = str(pipeline_values.get(field, ""))

        if not old:
            row["review_status"] = event.get("review_status", "pending") or "pending"
        elif (
            old.get("review_status") == "resolved"
            and old.get("evidence_fingerprint")
            and old.get("evidence_fingerprint") != event.get("evidence_fingerprint")
        ):
            row["review_status"] = "needs_re_review"
        elif event.get("review_status") == "needs_re_review":
            row["review_status"] = "needs_re_review"

        merged.append(row)
        seen.add(event_id)

    # Preserve editorial rows whose source event disappeared so the history is
    # not silently erased. Mark resolved records for re-review.
    current_ids = {str(e.get("closure_event_id", "")).strip() for e in events}
    for event_id, old in existing_by_event.items():
        if event_id in seen or event_id in current_ids or not _has_editorial_decision(old):
            # Handled above, or nothing editorial worth preserving.
            continue
        row = {field: str(old.get(field, "")) for field in OVERRIDES_FIELDS}
        if row.get("review_status") == "resolved":
            row["review_status"] = "needs_re_review"
        reason = row.get("review_reason", "")
        row["review_reason"] = "|".join(x for x in [reason, "source_event_missing"] if x)
        merged.append(row)
    # Old opeid8-keyed rows are dropped once an event-keyed row for the same
    # OPEID8 exists: that row is their migration and supersedes them.
    migrated = {str(r.get("opeid8", "")).strip().zfill(8) for r in merged if r.get("opeid8")}
    for old in legacy_rows:
        if str(old.get("opeid8", "")).strip().zfill(8) in migrated:
            continue
        merged.append({field: str(old.get(field, "")) for field in OVERRIDES_FIELDS})

    status_order = {"needs_re_review": 0, "pending": 1, "needs_more_evidence": 2, "resolved": 3}
    merged.sort(key=lambda r: (
        status_order.get(r.get("review_status", "pending"), 9),
        r.get("fsa_closed_date_iso", ""),
        r.get("opeid8", ""),
    ), reverse=False)
    return [OVERRIDES_FIELDS] + [[row.get(field, "") for field in OVERRIDES_FIELDS] for row in merged]


def _filter_published_rows(raw: list[dict], min_year: int = PUBLISHED_MIN_YEAR) -> list[dict]:
    """
    Publish only explicit decisions with consistent operational facts:
    editorial (resolved) or base-table automatic (auto_resolved), both set by
    the classifier.

    This function deliberately does not reconstruct scope from OPEID suffix,
    IPEDS identity, or the absence of another closure record.
    """
    pub: list[dict] = []
    for r in raw:
        if r.get("review_status") not in _PUBLISHABLE_STATUSES or r.get("review_required") != "false":
            continue

        decision = r.get("publication_decision", "")
        internal_type = r.get("closure_type", "")
        location_ceased = r.get("location_operations_ceased", "")
        parent_continued = r.get("parent_continued", "")
        if decision == "include_institution":
            if internal_type not in _INSTITUTION_TYPES or not (
                location_ceased == "true" and parent_continued == "false"
            ):
                continue
            scope = "institution"
            published_ctype = "institution_closure"
        elif decision == "include_campus":
            if internal_type not in _CAMPUS_TYPES or not (
                location_ceased == "true" and parent_continued == "true"
            ):
                continue
            scope = "campus"
            published_ctype = "campus_closure"
        else:
            continue

        opeid8 = r.get("opeid8", "")
        opeid6 = r.get("opeid6", "")
        closed_date = (
            r.get("published_closed_date_iso", "")
            or r.get("verified_closed_date_iso", "")
            or r.get("closed_date_iso", "")
        )
        closed_year = closed_date[:4] if len(closed_date) >= 4 else r.get("closed_year", "")
        if not closed_year.isdigit() or int(closed_year) < min_year:
            continue

        event_id = r.get("closure_event_id", "")
        if not event_id:
            continue

        pub.append({
            "opeid8":                     opeid8,
            "school_name":                r.get("school_name", ""),
            "city":                       r.get("city", ""),
            "state":                      r.get("state", ""),
            "control_label":              r.get("control_label", ""),
            "institution_category":       r.get("institution_category", ""),
            "program_level":              r.get("program_level", ""),
            "credential_level":           r.get("credential_level", ""),
            "enrollment_band":            r.get("enrollment_band", ""),
            "enrollment_headcount_total": r.get("enrollment_headcount_total", ""),
            "closed_date_iso":            closed_date,
            "closed_year":                closed_year,
            "closure_type":               published_ctype,
            "opeid6":                     opeid6,
            "unitid":                     r.get("unitid", ""),
            "closure_scope":              scope,
            "parent_continued":           parent_continued,
            "classification_basis":       r.get("closure_type_basis", ""),
            "closure_event_id":           event_id,
            "fsa_closed_date_iso":         r.get("fsa_closed_date_iso", "") or r.get("closed_date_iso", ""),
            "verified_closed_date_iso":    r.get("verified_closed_date_iso", ""),
            "date_basis":                 r.get("date_basis", ""),
            "date_evidence_url":          r.get("date_evidence_url", ""),
        })

    event_ids = [r["closure_event_id"] for r in pub]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("Duplicate closure_event_id in published rows")
    pub.sort(key=lambda r: r.get("closed_date_iso", ""), reverse=True)
    return pub


def _build_published_dicts(path: Path, min_year: int = PUBLISHED_MIN_YEAR) -> list[dict]:
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


def _validate_publish_bundle(
    pub: list[dict],
    by_year: list[list],
    by_state: list[list],
) -> None:
    """Fail before the first Sheet write if schema or totals do not reconcile."""
    event_ids = [r.get("closure_event_id", "") for r in pub]
    if any(not value for value in event_ids):
        raise ValueError("Published row missing closure_event_id")
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("Published closure_event_id values are not unique")

    for row in pub:
        scope = row.get("closure_scope", "")
        expected_type = "institution_closure" if scope == "institution" else "campus_closure"
        if scope not in {"institution", "campus"} or row.get("closure_type") != expected_type:
            raise ValueError(f"Invalid published scope/type for {row.get('closure_event_id', '')}")
        expected_parent = "false" if scope == "institution" else "true"
        if row.get("parent_continued") != expected_parent:
            raise ValueError(f"Invalid parent_continued for {row.get('closure_event_id', '')}")

    if not by_year or by_year[0] != _BY_YEAR_HEADER:
        raise ValueError("by_year schema mismatch")
    if not by_state or by_state[0] != _BY_STATE_HEADER:
        raise ValueError("by_state schema mismatch")

    annual = [r for r in by_year[1:] if r[1] == 0]
    annual_inst = sum(r[12] for r in annual)
    annual_campus = sum(r[13] for r in annual)
    expected_inst = sum(1 for r in pub if r.get("closed_year") and r["closure_scope"] == "institution")
    expected_campus = sum(1 for r in pub if r.get("closed_year") and r["closure_scope"] == "campus")
    if (annual_inst, annual_campus) != (expected_inst, expected_campus):
        raise ValueError("by_year totals do not reconcile to published rows")

    state_rows = [r for r in pub if len(r.get("state", "").strip()) == 2]
    state_inst = sum(r[5] for r in by_state[1:])
    state_campus = sum(r[6] for r in by_state[1:])
    expected_state_inst = sum(1 for r in state_rows if r["closure_scope"] == "institution")
    expected_state_campus = sum(1 for r in state_rows if r["closure_scope"] == "campus")
    if (state_inst, state_campus) != (expected_state_inst, expected_state_campus):
        raise ValueError("by_state totals do not reconcile to published rows")


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


# Editor-confirmed outcomes where operations continued under another
# institution; excluded from closure counts and listed on the mergers tab.
MERGER_TYPES = {
    "merger_absorption": "merger",
    "affiliation_or_governance_change": "affiliation",
}

MERGERS_HEADER = [
    "school_name", "city", "state", "opeid8", "control_label", "program_level",
    "fsa_closed_date_iso", "fsa_closed_year", "outcome", "basis",
    "evidence_url", "replacement_opeid8", "closure_event_id",
]


def _build_mergers_data(path: Path) -> list[list]:
    """Rows for the mergers tab: editor-confirmed mergers and affiliations, newest first."""
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [
            r for r in csv.DictReader(fh)
            if r.get("review_status") == "resolved" and r.get("closure_type") in MERGER_TYPES
        ]
    rows.sort(key=lambda r: r.get("fsa_closed_date_iso", "") or r.get("closed_date_iso", ""), reverse=True)
    out: list[list] = [MERGERS_HEADER]
    for r in rows:
        fsa_date = r.get("fsa_closed_date_iso", "") or r.get("closed_date_iso", "")
        out.append([
            r.get("school_name", ""), r.get("city", ""), r.get("state", ""), r.get("opeid8", ""),
            r.get("control_label", ""), r.get("program_level", ""), fsa_date, fsa_date[:4],
            MERGER_TYPES[r["closure_type"]], r.get("closure_type_basis", ""),
            r.get("date_evidence_url", ""), r.get("replacement_opeid8", ""), r.get("closure_event_id", ""),
        ])
    return out


def _build_readme_data(generated_at: str = "") -> list[list]:
    """Return README tab content as a list of single-cell rows."""
    return [
        ["Hechinger Report — College Closure Tracker"],
        [f"Generated: {generated_at}" if generated_at else ""],
        [""],
        ["PUBLICATION RULE"],
        ["FSA closed-school rows are candidates, not proof that educational operations ceased."],
        ["Two paths enter published:"],
        ["  auto_resolved — closures in the frozen base table that meet the automatic evidence standard:"],
        ["    campus = exact IPEDS identity + another location of the same OPEID6 active in RT01 on the close date;"],
        ["    institution = main location + exact IPEDS identity + no other RT01 location active on the close date."],
        ["  resolved — an editor's decision in the overrides tab (always wins over auto_resolved)."],
        ["FSA's location file keeps stale 'open' rows for defunct schools, so a main-location closure"],
        ["with another location still listed counts as an institution closure only when either:"],
        ["  no school under the OPEID6 is ACTIVE in IPEDS after the closure year (IPEDS checked through the latest year), or"],
        ["  (base table only) the Financial Health tracker had an editor-confirmed institution_closure"],
        ["  for the same UNITID as of the freeze."],
        ["IPEDS STATUS = C (combined with another institution) always sends the row to editors as a possible merger."],
        ["Every closure that first appears AFTER the base table was frozen goes to editor review,"],
        ["even if it meets the automatic standard (review_reason includes new_since_baseline)."],
        ["Headline products should filter published to closure_scope = institution; campus rows remain separately filterable."],
        [""],
        ["TABS"],
        ["closure_list — complete candidate ledger and structured evidence; no candidate type is hidden."],
        ["overrides — one combined pending/resolved review queue keyed by closure_event_id."],
        ["published — resolved and auto_resolved institution_closure and campus_closure rows only."],
        ["by_year — institution-only total plus separate campus/all-event columns."],
        ["by_state — institution-only total plus separate campus/all-event columns."],
        ["mergers — editor-confirmed mergers and affiliations (excluded from closure counts), with basis and source."],
        [""],
        ["OVERRIDES WORKFLOW"],
        ["review_status: pending | resolved | needs_more_evidence | needs_re_review"],
        ["  (auto_resolved is pipeline-only and never appears as an editor choice)"],
        ["publication_decision: include_institution | include_campus | exclude"],
        ["include_institution requires location_operations_ceased=true and parent_continued=false."],
        ["include_campus requires location_operations_ceased=true and parent_continued=true."],
        ["If source evidence changes, evidence_fingerprint forces needs_re_review."],
        [""],
        ["MERGERS / CONSOLIDATIONS"],
        ["RT11 identifies the organization holding records; it is never merger evidence."],
        ["RT06 creates an exact-replacement review signal only when the dated old-to-new OPEID"],
        ["relationship is near the event and the exact successor is active in RT01."],
        ["Names, shared addresses, or several nearby closures may trigger review only."],
        ["Operations continuing through a successor => exclude as merger/affiliation."],
        ["Institution ceased but assets/students/programs transferred => institution closure with context."],
        [""],
        ["HOW THIS COMPARES (full methods note: README.md in the GitHub repo)"],
        ["SHEEO method: an OPEID6 counts only when EVERY location is closed; closure year = last location."],
        ["  One stale 'open' FSA row drops the school; mergers count as closures. Source of the earlier"],
        ["  Hechinger figure of 'nearly 300' (287) associate-or-higher closures, 2008-2023."],
        ["Kelchen et al. (Federal Reserve FEDS 2025-003): main campus (OPEID ...00) closed, first closure;"],
        ["  IPEDS used to match institutions and add characteristics, not to test continued operation."],
        ["Ours: main campus closed AND evidence the institution stopped (no other open location, no active"],
        ["  IPEDS reporting afterwards, or editor confirmation); possible mergers go to editors."],
        ["As of the November 2026 build: 1,749 main-campus closures 1996-Nov 2023 vs Kelchen's 1,661-1,671;"],
        ["  2008-2023: SHEEO method 848, ours 1,037, 816 on both lists; associate-or-higher 415 vs SHEEO's 287."],
        ["  scripts/benchmark_sheeo_method.py reruns the SHEEO comparison weekly and explains each difference."],
        ["LIMITATIONS: disappearing from IPEDS is evidence, not proof (some mergers vanish quietly);"],
        ["  recent years rely on editors because IPEDS lags; campus counts cover IPEDS-reported campuses only;"],
        ["  public under-2-year district/career-tech programs are excluded (SHEEO and Kelchen count them)."],
        ["OUT OF SCOPE (excluded automatically, reason in closure_type; editors can override): locations with no"],
        ["  IPEDS record of their own (incl. sites matched only via the parent college, e.g. K-12 dual-enrollment sites), foreign locations, exact identifier replacements, public under-2-year"],
        ["  district/career-tech programs, IPEDS administrative units. Published table starts in 1996."],
        [""],
        ["DATES AND IPEDS"],
        ["fsa_closed_date_iso remains immutable; closed_date_iso is the verified public date when available."],
        ["program_level uses historical IPEDS ICLEVEL: 4-year or above | 2-year | Less than 2-year."],
        ["Approach 3 is distinct OPEID6 systems with a genuine closure, never a sum of event rows."],
    ]


def _write_tab(sh: gspread.Spreadsheet, tab: str, data: list[list]) -> None:
    ws = _open_or_create_tab(sh, tab, len(data[0]) if data else 1)
    ws.clear()
    ws.update("A1", data)
    print(f"  '{tab}' tab written ({len(data) - 1} rows)")


def _existing_records(ws: gspread.Worksheet) -> list[dict]:
    try:
        rows = ws.get_all_records(default_blank="")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _merge_local_overrides(sheet_rows: list[dict], local_path: Path = LOCAL_OVERRIDES) -> list[dict]:
    """Add committed local override rows the Sheet does not hold yet.

    The publish workflow pulls the Sheet's overrides tab over the local CSV, so
    a decision recorded only locally would be lost. The Sheet wins for any
    closure_event_id it already holds (editors may have changed it there).
    """
    if not local_path.exists():
        return sheet_rows
    in_sheet = {str(r.get("closure_event_id", "")).strip() for r in sheet_rows}
    with local_path.open(encoding="utf-8", newline="") as fh:
        local = [r for r in csv.DictReader(fh)
                 if (r.get("closure_event_id") or "").strip() and r["closure_event_id"].strip() not in in_sheet]
    return list(sheet_rows) + local


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
        by_year = _build_by_year_data(pub_dicts)
        by_state = _build_by_state_data(pub_dicts)
        _validate_publish_bundle(pub_dicts, by_year, by_state)
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
        _write_tab(sh, "by_year", by_year)
        _write_tab(sh, "by_state", by_state)
        _write_tab(sh, "mergers", _build_mergers_data(events_path))
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

        # Build and validate the companion review queue before clearing either
        # tab, so local preparation failures leave the Sheet untouched.
        with events_path.open(encoding="utf-8", newline="") as fh:
            event_dicts = list(csv.DictReader(fh))
        overrides_ws = _open_or_create_tab(sh, "overrides", len(OVERRIDES_FIELDS))
        queue = _build_overrides_queue(event_dicts, _merge_local_overrides(_existing_records(overrides_ws)))

        ws.clear()
        ws.update("A1", data)
        print(f"  Written {nrows:,} rows to '{tab}' tab.")
        overrides_ws.clear()
        overrides_ws.update("A1", queue)
        print(f"  Merged review queue written to 'overrides' ({len(queue) - 1:,} rows).")
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
