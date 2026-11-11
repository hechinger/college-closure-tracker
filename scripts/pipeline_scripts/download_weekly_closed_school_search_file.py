#!/usr/bin/env python3
"""Download the current weekly closed school search file from FSA Partner Connect.

The site exposes a small search endpoint that returns the latest document
storage ID. We then hand that ID to the download endpoint and save the zip to a
stable local path so later steps can read it without guessing dates.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


SEARCH_URL = "https://user-api.fsapartners.ed.gov/reports/search/weekly-closed-school"
DOWNLOAD_URL = "https://user-api.fsapartners.ed.gov/reports/download"
DOC_TYPE = "WEEKLY_CLOSED_REPORT"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=Path("raw") / "weekly_closed_school_search" / "latest.zip",
        help="Path where the downloaded zip should be written.",
    )
    parser.add_argument(
        "--metadata",
        default=Path("raw") / "weekly_closed_school_search" / "latest.json",
        help="Path where the download metadata JSON should be written.",
    )
    parser.add_argument(
        "--search-url",
        default=SEARCH_URL,
        help="Override the FSA weekly search endpoint.",
    )
    parser.add_argument(
        "--download-url",
        default=DOWNLOAD_URL,
        help="Override the FSA download endpoint.",
    )
    return parser.parse_args()


def post_json(url: str, payload: dict, *, extra_headers: dict[str, str] | None = None) -> tuple[bytes, dict[str, str]]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "identity",
        "Content-Type": "application/json",
        "Origin": "https://fsapartners.ed.gov",
        "Referer": "https://fsapartners.ed.gov/additional-resources/reports/weekly-closed-school-search-file",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    }
    if extra_headers:
        headers.update(extra_headers)

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read()
        header_map = dict(response.headers.items())
        if header_map.get("Content-Encoding", "").lower() == "gzip" or header_map.get("content-encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
        return body, header_map


def search_latest_report(search_url: str) -> dict:
    payload = {}
    body, _headers = post_json(search_url, payload)
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, list) or not data:
        raise RuntimeError("The weekly closed school search endpoint returned no results.")

    def sort_key(item: dict) -> tuple[str, str]:
        return (str(item.get("procDate", "")), str(item.get("docStorageId", "")))

    return max(data, key=sort_key)


def download_report(download_url: str, doc_storage_id: str) -> tuple[bytes, dict[str, str]]:
    payload = {
        "docType": DOC_TYPE,
        "docStorageId": doc_storage_id,
    }
    return post_json(download_url, payload)


def safe_filename_from_headers(headers: dict[str, str], fallback: str) -> str:
    disposition = headers.get("content-disposition", "") or headers.get("Content-Disposition", "")
    match = re.search(r'filename="?([^"]+)"?', disposition)
    if match:
        return match.group(1)
    return fallback


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    args = parse_args()

    latest = search_latest_report(args.search_url)
    doc_storage_id = str(latest.get("docStorageId", "")).strip()
    proc_date = str(latest.get("procDate", "")).strip()
    doc_type = str(latest.get("docType", DOC_TYPE)).strip() or DOC_TYPE

    if not doc_storage_id:
        raise RuntimeError("The weekly closed school search response did not include a docStorageId.")

    body, headers = download_report(args.download_url, doc_storage_id)
    fallback_name = f"WKCL.{proc_date or dt.date.today().isoformat().replace('-', '')}.{doc_storage_id}.zip"
    filename = safe_filename_from_headers(headers, fallback_name)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(body)

    metadata = {
        "downloaded_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "docType": doc_type,
        "procDate": proc_date,
        "docStorageId": doc_storage_id,
        "search_url": args.search_url,
        "download_url": args.download_url,
        "filename": filename,
        "output_path": str(output_path).replace("\\", "/"),
    }
    write_json(Path(args.metadata), metadata)

    print(json.dumps(metadata, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        print(
            "Failed to download the weekly closed school search file.\n"
            f"Network error: {exc}\n"
            "If you are testing locally without internet access, skip this step and place the latest weekly ZIP in raw/weekly_closed_school_search/ yourself.",
            file=sys.stderr,
        )
        raise
