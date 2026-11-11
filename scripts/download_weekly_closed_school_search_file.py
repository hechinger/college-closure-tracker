#!/usr/bin/env python3
"""Download the current FSA Weekly Closed School Search File.

Saves the ZIP to a source-dated folder under data/raw/weekly_closed_school_search/
and writes a sidecar metadata JSON with checksums and provenance.

Idempotency rules
-----------------
- Same outer ZIP already on disk          → no-op, exit 0
- Different outer ZIP, identical members  → recorded as equivalent, exit 0
- Same doc_id, different member content   → hard failure, exit 1 (manual review)
- Not seen before                         → save and exit 0
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from _download_utils import (
    IdempotencyOutcome,
    ZipValidationError,
    check_idempotency,
    filename_from_headers,
    find_existing_by_doc_id,
    post_json,
    sha256_bytes,
    validate_zip,
    write_metadata,
)

SEARCH_URL = "https://user-api.fsapartners.ed.gov/reports/search/weekly-closed-school"
DOWNLOAD_URL = "https://user-api.fsapartners.ed.gov/reports/download"
DOC_TYPE = "WEEKLY_CLOSED_REPORT"
REFERER = "https://fsapartners.ed.gov/additional-resources/reports/weekly-closed-school-search-file"

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_FOLDER = PROJECT_ROOT / "data" / "raw" / "weekly_closed_school_search"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--output-folder",
        type=Path,
        default=DEFAULT_OUTPUT_FOLDER,
        help="Root folder; file is saved under {output-folder}/{proc_date}/.",
    )
    p.add_argument(
        "--search-url",
        default=SEARCH_URL,
        help="Override the FSA weekly closed-school search endpoint.",
    )
    p.add_argument(
        "--download-url",
        default=DOWNLOAD_URL,
        help="Override the FSA download endpoint.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch metadata only; do not write files.",
    )
    return p.parse_args()


def search_latest(search_url: str) -> dict:
    body, _ = post_json(search_url, {}, referer=REFERER)
    results = json.loads(body.decode("utf-8"))
    if not isinstance(results, list) or not results:
        raise RuntimeError(
            "Weekly closed-school search returned an empty or unexpected response."
        )
    return max(results, key=lambda r: (str(r.get("procDate", "")), str(r.get("docStorageId", ""))))


def download_report(download_url: str, doc_storage_id: str) -> tuple[bytes, dict]:
    payload = {"docType": DOC_TYPE, "docStorageId": doc_storage_id}
    return post_json(download_url, payload, referer=REFERER)


def main() -> int:
    args = parse_args()

    print("Searching for latest weekly closed-school report…")
    latest = search_latest(args.search_url)

    doc_id = str(latest.get("docStorageId", "")).strip()
    proc_date = str(latest.get("procDate", "")).strip()
    doc_type = str(latest.get("docType", DOC_TYPE)).strip() or DOC_TYPE

    if not doc_id:
        print("ERROR: search response did not include a docStorageId.", file=sys.stderr)
        return 1

    print(f"  docStorageId : {doc_id}")
    print(f"  procDate     : {proc_date}")

    if args.dry_run:
        print("Dry run — stopping before download.")
        return 0

    # Pre-download idempotency check: if we already have this doc_id, skip the fetch.
    existing = find_existing_by_doc_id(args.output_folder, doc_id)
    if existing:
        existing_zip, existing_meta = existing
        print(f"No-op: doc_id '{doc_id}' already on disk at {existing_zip}")
        return 0

    print("Downloading…")
    body, headers = download_report(args.download_url, doc_id)

    # Validate before touching the filesystem.
    try:
        member_hashes = validate_zip(body)
    except ZipValidationError as exc:
        print(f"ERROR: downloaded body failed validation: {exc}", file=sys.stderr)
        return 1

    outer_hash = sha256_bytes(body)
    fallback_name = f"{DOC_TYPE}_{doc_id}.zip"
    filename = filename_from_headers(headers, fallback_name)

    # Dated output path.
    date_slug = proc_date.replace("-", "") if proc_date else dt.date.today().strftime("%Y%m%d")
    out_dir = args.output_folder / proc_date if proc_date else args.output_folder / date_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / filename
    metadata_path = out_dir / (Path(filename).stem + "_metadata.json")

    # Idempotency check.
    try:
        outcome = check_idempotency(output_path, outer_hash, member_hashes, doc_id, metadata_path=metadata_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if outcome == IdempotencyOutcome.NOOP:
        print(f"No-op: identical file already at {output_path}")
        return 0
    if outcome == IdempotencyOutcome.EQUIVALENT:
        print(f"Equivalent: different ZIP packaging but identical members; existing file kept.")
        return 0

    # Write.
    output_path.write_bytes(body)

    metadata = {
        "downloaded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "idempotency_outcome": outcome.value,
        "source_type": "weekly_closed_school_search",
        "doc_type": doc_type,
        "doc_storage_id": doc_id,
        "proc_date": proc_date,
        "search_url": args.search_url,
        "download_url": args.download_url,
        "filename": filename,
        "output_path": str(output_path).replace("\\", "/"),
        "byte_size": len(body),
        "outer_sha256": outer_hash,
        "member_sha256": member_hashes,
    }
    write_metadata(metadata_path, metadata)

    print(f"Saved  : {output_path}")
    print(f"Members: {list(member_hashes)}")
    print(f"SHA-256: {outer_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
