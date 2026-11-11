"""Tests for the network-free publication staging bundle."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_publication_bundle import _build_change_report, build_bundle
from build_ipeds_crosswalk import ENRICHED_FIELDS
from classify_closure_events import EVENTS_FIELDS


def _resolved_event(**overrides) -> dict:
    row = {field: "" for field in EVENTS_FIELDS}
    row.update({
        "source_date": "2026-11-08",
        "opeid8": "00100200",
        "opeid6": "001002",
        "location_suffix": "00",
        "is_main_campus": "true",
        "school_name": "Alpha College",
        "city": "Boston",
        "state": "MA",
        "closed_date_iso": "2020-06-01",
        "closed_year": "2020",
        "fsa_closed_date_iso": "2020-06-01",
        "verified_closed_date_iso": "2020-06-15",
        "published_closed_date_iso": "2020-06-15",
        "closure_event_id": "fsa:00100200:2020-06-01",
        "closure_type": "institutional_closure",
        "closure_type_basis": "Editorial evidence",
        "candidate_closure_scope": "institution",
        "location_operations_ceased": "true",
        "parent_continued": "false",
        "review_required": "false",
        "review_status": "resolved",
        "publication_decision": "include_institution",
        "control_label": "public",
        "institution_category": "public_4yr",
        "program_level": "4-year or above",
        "credential_level": "Bachelor's",
        "evidence_fingerprint": "abc",
    })
    row.update(overrides)
    return row


def _write_events(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=EVENTS_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def test_change_report_distinguishes_change_types():
    old = [_resolved_event()]
    new = [_resolved_event(closed_date_iso="2020-06-01", published_closed_date_iso="2020-07-01")]
    current_public = [{
        "closure_event_id": new[0]["closure_event_id"],
        "opeid8": new[0]["opeid8"],
        "school_name": new[0]["school_name"],
        "closure_scope": "institution",
        "closure_type": "institution_closure",
        "closed_date_iso": "2020-07-01",
    }]
    prior_public = [{
        "closure_event_id": old[0]["closure_event_id"],
        "opeid8": old[0]["opeid8"],
        "school_name": old[0]["school_name"],
        "closure_scope": "institution",
        "closure_type": "institution_closure",
        "closed_date_iso": "2020-06-15",
    }]
    changes = _build_change_report(current_public, prior_public)
    assert changes[0]["change_status"] == "date_changed"


def test_build_bundle_writes_validated_tables_without_network(tmp_path):
    events_path = tmp_path / "events.csv"
    _write_events(events_path, [_resolved_event()])
    out_dir = tmp_path / "bundle"

    summary = build_bundle(
        events_path=events_path,
        out_dir=out_dir,
        release_date="2026-11-11",
        overrides_path=None,
        previous_published=None,
    )

    assert summary["validation"] == "passed"
    assert summary["sheet_write_performed"] is False
    assert summary["published_count"] == 1
    assert (out_dir / "published_2026-11-11.csv").exists()
    assert (out_dir / "overrides_2026-11-11.csv").exists()
    validation = json.loads((out_dir / "validation_2026-11-11.json").read_text(encoding="utf-8"))
    assert validation["institution_closures"] == 1
