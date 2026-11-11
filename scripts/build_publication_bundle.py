#!/usr/bin/env python3
"""Build and validate a local Google-Sheets publication bundle.

This command performs no network writes. It materializes the exact tables that
would be sent to Google Sheets, plus a release-to-release change report and a
machine-readable validation summary.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from pull_gsheet_overrides import OVERRIDES_FIELDS
from push_to_gsheet import (
    _EDITORIAL_COLS,
    _build_by_state_data,
    _build_by_year_data,
    _build_overrides_queue,
    _build_published_dicts,
    _build_mergers_data,
    _build_readme_data,
    _latest_events_csv,
    _load_rows,
    _pub_dicts_to_rows,
    _validate_publish_bundle,
)

PROJECT = Path(__file__).resolve().parent.parent
DERIVED = PROJECT / "data" / "derived"
MANUAL_OVERRIDES = PROJECT / "data" / "manual" / "closure_type_overrides.csv"
DEFAULT_OUT = DERIVED / "publication_staging"


def _read_dicts(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_table(path: Path, rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(rows)


def _write_dicts(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _build_change_report(current: list[dict], previous: list[dict]) -> list[dict]:
    """Return added/removed/reclassified/date-changed rows keyed by stable event ID."""
    current_idx = {r.get("closure_event_id", ""): r for r in current if r.get("closure_event_id")}
    previous_idx = {r.get("closure_event_id", ""): r for r in previous if r.get("closure_event_id")}
    changes: list[dict] = []
    for event_id in sorted(set(current_idx) | set(previous_idx)):
        new = current_idx.get(event_id)
        old = previous_idx.get(event_id)
        if old is None:
            status = "added"
        elif new is None:
            status = "removed"
        elif (old.get("closure_scope"), old.get("closure_type")) != (
            new.get("closure_scope"), new.get("closure_type")
        ):
            status = "reclassified"
        elif old.get("closed_date_iso") != new.get("closed_date_iso"):
            status = "date_changed"
        else:
            continue
        row = new or old or {}
        changes.append({
            "change_status": status,
            "closure_event_id": event_id,
            "opeid8": row.get("opeid8", ""),
            "school_name": row.get("school_name", ""),
            "old_closure_scope": (old or {}).get("closure_scope", ""),
            "new_closure_scope": (new or {}).get("closure_scope", ""),
            "old_closure_type": (old or {}).get("closure_type", ""),
            "new_closure_type": (new or {}).get("closure_type", ""),
            "old_closed_date_iso": (old or {}).get("closed_date_iso", ""),
            "new_closed_date_iso": (new or {}).get("closed_date_iso", ""),
        })
    return changes


CHANGE_FIELDS = [
    "change_status", "closure_event_id", "opeid8", "school_name",
    "old_closure_scope", "new_closure_scope",
    "old_closure_type", "new_closure_type",
    "old_closed_date_iso", "new_closed_date_iso",
]


def build_bundle(
    events_path: Path,
    out_dir: Path,
    release_date: str,
    overrides_path: Path | None = MANUAL_OVERRIDES,
    previous_published: Path | None = None,
) -> dict:
    with events_path.open(encoding="utf-8", newline="") as fh:
        events = list(csv.DictReader(fh))

    published = _build_published_dicts(events_path)
    by_year = _build_by_year_data(published)
    by_state = _build_by_state_data(published)
    _validate_publish_bundle(published, by_year, by_state)

    existing_overrides = _read_dicts(overrides_path)
    overrides = _build_overrides_queue(events, existing_overrides)
    closure_list = _load_rows(events_path, _EDITORIAL_COLS)

    published_path = out_dir / f"published_{release_date}.csv"
    closure_list_path = out_dir / f"closure_list_{release_date}.csv"
    overrides_out_path = out_dir / f"overrides_{release_date}.csv"
    by_year_path = out_dir / f"by_year_{release_date}.csv"
    by_state_path = out_dir / f"by_state_{release_date}.csv"
    readme_path = out_dir / f"readme_{release_date}.csv"
    changes_path = out_dir / f"publication_changes_{release_date}.csv"
    validation_path = out_dir / f"validation_{release_date}.json"

    _write_table(published_path, _pub_dicts_to_rows(published))
    _write_table(closure_list_path, closure_list)
    _write_table(overrides_out_path, overrides)
    _write_table(by_year_path, by_year)
    _write_table(by_state_path, by_state)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _write_table(readme_path, _build_readme_data(generated_at))
    _write_table(out_dir / f"mergers_{release_date}.csv", _build_mergers_data(events_path))

    previous = _read_dicts(previous_published)
    changes = _build_change_report(published, previous)
    _write_dicts(changes_path, changes, CHANGE_FIELDS)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_date": release_date,
        "events_file": str(events_path),
        "events_count": len(events),
        "published_count": len(published),
        "institution_closures": sum(1 for r in published if r["closure_scope"] == "institution"),
        "campus_closures": sum(1 for r in published if r["closure_scope"] == "campus"),
        "review_queue_count": len(overrides) - 1,
        "change_counts": {
            status: sum(1 for row in changes if row["change_status"] == status)
            for status in ("added", "removed", "reclassified", "date_changed")
        },
        "validation": "passed",
        "sheet_write_performed": False,
    }
    validation_path.parent.mkdir(parents=True, exist_ok=True)
    validation_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events-csv", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--date", default=datetime.now().date().isoformat())
    parser.add_argument("--overrides-csv", type=Path, default=MANUAL_OVERRIDES)
    parser.add_argument("--previous-published", type=Path, default=None)
    args = parser.parse_args()

    events_path = args.events_csv or _latest_events_csv()
    summary = build_bundle(
        events_path=events_path,
        out_dir=args.out_dir,
        release_date=args.date,
        overrides_path=args.overrides_csv,
        previous_published=args.previous_published,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
