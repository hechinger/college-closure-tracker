#!/usr/bin/env python3
"""Download the FSA Weekly School File in Access or database-readable format.

The Weekly School File is broader than the closed-school search file: it covers
all Title IV-participating schools and includes closure and reinstatement fields
needed to verify closed-school records.

IMPORTANT — endpoint discovery
-------------------------------
The FSA Partner Connect API endpoint for this file is NOT the same as the
closed-school search endpoint and must not be assumed from that pattern.
Inspect real browser requests against:
  https://fsapartners.ed.gov/additional-resources/reports/weekly-school-file
and supply the confirmed search URL via --search-url.

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
    post_json,
    sha256_bytes,
    validate_zip,
    write_metadata,
)

DOWNLOAD_URL = "https://user-api.fsapartners.ed.gov/reports/download"
REFERER = "https://fsapartners.ed.gov/additional-resources/reports/weekly-school-file"

# Keywords used to identify each format in docType or filename fields.
FORMAT_KEYWORDS = {
    "access": ("access", "schfile_access"),
    "database": ("extract", "database", "schfile_extract"),
}

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_OUTPUT_FOLDER = PROJECT_ROOT / "data" / "raw" / "weekly_school_file"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--search-url",
        required=True,
        help=(
            "FSA Partner Connect search endpoint for the Weekly School File. "
            "Must be confirmed from real browser traffic — do not guess from "
            "the closed-school endpoint."
        ),
    )
    p.add_argument(
        "--format",
        choices=["access", "database"],
        required=True,
        help="'access' = fixed-width Access-format ZIP; 'database' = database-readable ZIP.",
    )
    p.add_argument(
        "--download-url",
        default=DOWNLOAD_URL,
        help="Override the FSA download endpoint.",
    )
    p.add_argument(
        "--output-folder",
        type=Path,
        default=DEFAULT_OUTPUT_FOLDER,
        help="Root folder; file is saved under {output-folder}/{proc_date}/{format}/.",
    )
    p.add_argument(
        "--doc-type",
        default=None,
        help=(
            "Override the docType sent to the download endpoint. "
            "If omitted, the value from the search response is used."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch metadata only; do not write files.",
    )
    return p.parse_args()


def search_reports(search_url: str) -> list[dict]:
    body, _ = post_json(search_url, {}, referer=REFERER)
    results = json.loads(body.decode("utf-8"))
    if not isinstance(results, list) or not results:
        raise RuntimeError(
            "Weekly school file search returned an empty or unexpected response.\n"
            f"  URL: {search_url}\n"
            "Check that --search-url points to the correct FSA endpoint."
        )
    return results


def pick_format(results: list[dict], fmt: str) -> dict:
    """Select the report entry matching *fmt* from the search results."""
    keywords = FORMAT_KEYWORDS[fmt]

    def matches(item: dict) -> bool:
        haystack = " ".join([
            str(item.get("docType", "")),
            str(item.get("fileName", "")),
            str(item.get("title", "")),
        ]).lower()
        return any(kw in haystack for kw in keywords)

    candidates = [r for r in results if matches(r)]
    if not candidates:
        available = [
            {k: r.get(k) for k in ("docType", "fileName", "procDate", "docStorageId")}
            for r in results
        ]
        raise RuntimeError(
            f"No search result matched format '{fmt}' (keywords: {keywords}).\n"
            f"Available entries:\n{json.dumps(available, indent=2)}\n"
            "Adjust --format or supply --doc-type to override."
        )
    # Prefer the most recent by procDate then docStorageId.
    return max(candidates, key=lambda r: (str(r.get("procDate", "")), str(r.get("docStorageId", ""))))


def download_report(download_url: str, doc_type: str, doc_storage_id: str) -> tuple[bytes, dict]:
    payload = {"docType": doc_type, "docStorageId": doc_storage_id}
    return post_json(download_url, payload, referer=REFERER)


def main() -> int:
    args = parse_args()

    print(f"Searching Weekly School File ({args.format} format)…")
    results = search_reports(args.search_url)
    print(f"  {len(results)} result(s) returned.")

    try:
        entry = pick_format(results, args.format)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    doc_id = str(entry.get("docStorageId", "")).strip()
    proc_date = str(entry.get("procDate", "")).strip()
    doc_type = args.doc_type or str(entry.get("docType", "")).strip()

    if not doc_id:
        print("ERROR: search response did not include a docStorageId.", file=sys.stderr)
        return 1
    if not doc_type:
        print("ERROR: could not determine docType; supply --doc-type.", file=sys.stderr)
        return 1

    print(f"  docStorageId : {doc_id}")
    print(f"  docType      : {doc_type}")
    print(f"  procDate     : {proc_date}")

    if args.dry_run:
        print("Dry run — stopping before download.")
        return 0

    print("Downloading…")
    body, headers = download_report(args.download_url, doc_type, doc_id)

    try:
        member_hashes = validate_zip(body)
    except ZipValidationError as exc:
        print(f"ERROR: downloaded body failed validation: {exc}", file=sys.stderr)
        return 1

    outer_hash = sha256_bytes(body)
    fallback_name = f"schfile_{args.format}_{proc_date.replace('-', '') if proc_date else 'unknown'}.dat.zip"
    filename = filename_from_headers(headers, fallback_name)

    out_dir = args.output_folder / (proc_date or dt.date.today().isoformat()) / args.format
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / filename
    metadata_path = out_dir / (Path(filename).stem + "_metadata.json")

    try:
        outcome = check_idempotency(output_path, outer_hash, member_hashes, doc_id, metadata_path=metadata_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if outcome == IdempotencyOutcome.NOOP:
        print(f"No-op: identical file already at {output_path}")
        return 0
    if outcome == IdempotencyOutcome.EQUIVALENT:
        print("Equivalent: different ZIP packaging but identical members; existing file kept.")
        return 0

    output_path.write_bytes(body)

    metadata = {
        "downloaded_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "idempotency_outcome": outcome.value,
        "source_type": "weekly_school_file",
        "format": args.format,
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
        "zip_members": [
            {"name": k, "member_sha256": v}
            for k, v in member_hashes.items()
        ],
    }
    write_metadata(metadata_path, metadata)

    print(f"Saved  : {output_path}")
    print(f"Members: {list(member_hashes)}")
    print(f"SHA-256: {outer_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
