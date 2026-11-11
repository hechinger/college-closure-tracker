"""Shared download utilities for FSA federal-report downloaders.

Provides ZIP validation, checksum helpers, idempotency logic, and the
shared HTTP POST helper so neither downloader duplicates these concerns.
"""

from __future__ import annotations

import enum
import gzip
import hashlib
import json
import re
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# ZIP validation
# ---------------------------------------------------------------------------

class ZipValidationError(ValueError):
    """Raised when a downloaded body fails ZIP validation."""


def _looks_like_html(data: bytes) -> bool:
    snippet = data[:2048].lstrip()
    return snippet.startswith(b"<!") or snippet.startswith(b"<html") or b"<title>" in snippet.lower()


def validate_zip(
    data: bytes,
    *,
    min_bytes: int = 22,
    expected_member_prefix: str | None = None,
) -> dict[str, str]:
    """Validate that *data* is a well-formed ZIP and return member SHA-256s.

    Raises ZipValidationError for:
    - Empty or too-small body
    - Body that looks like an HTML login/error page
    - Body that is not a valid ZIP
    - ZIP members whose paths contain '..' or start with '/' (path traversal)

    Returns a dict mapping member name → SHA-256 hex string.
    """
    if len(data) < min_bytes:
        raise ZipValidationError(
            f"Response body is {len(data)} bytes — too small to be a ZIP."
        )
    if _looks_like_html(data):
        raise ZipValidationError(
            "Response body looks like an HTML page, not a ZIP. "
            "The endpoint may require authentication or the session expired."
        )
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ZipValidationError(f"Response body is not a valid ZIP: {exc}") from exc

    members: dict[str, str] = {}
    with zf:
        for info in zf.infolist():
            name = info.filename
            if ".." in name or name.startswith("/") or name.startswith("\\"):
                raise ZipValidationError(
                    f"ZIP member '{name}' contains an unsafe path — aborting."
                )
            if expected_member_prefix and not name.startswith(expected_member_prefix):
                pass  # informational only; callers can enforce if needed
            members[name] = sha256_bytes(zf.read(name))
    return members


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class IdempotencyOutcome(enum.Enum):
    NEW = "new"
    NOOP = "noop"               # identical outer ZIP already on disk
    EQUIVALENT = "equivalent"   # different outer ZIP, identical members
    CONFLICT = "conflict"       # same doc_id, different member content


def find_existing_by_doc_id(search_root: Path, doc_id: str) -> tuple[Path, Path] | None:
    """Scan *search_root* recursively for a metadata JSON claiming *doc_id*.

    Returns (zip_path, metadata_path) if found, else None.
    """
    for meta_path in search_root.rglob("*_metadata.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(meta.get("doc_storage_id", "")) == doc_id:
            zip_path = meta_path.parent / meta.get("filename", "")
            if zip_path.exists():
                return zip_path, meta_path
    return None


def check_idempotency(
    output_path: Path,
    new_outer_hash: str,
    new_member_hashes: dict[str, str],
    doc_id: str,
    *,
    metadata_path: Path | None = None,
) -> IdempotencyOutcome:
    """Compare a pending download against what is already on disk.

    Uses the sidecar metadata JSON (if it exists) to detect conflicts
    without re-hashing the stored ZIP.

    Raises RuntimeError on CONFLICT — the caller must not silently overwrite.
    """
    if not output_path.exists():
        return IdempotencyOutcome.NEW

    # Try the sidecar metadata first (fast path).
    meta: dict = {}
    if metadata_path and metadata_path.exists():
        try:
            meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    stored_outer = meta.get("outer_sha256") or sha256_file(output_path)
    if stored_outer == new_outer_hash:
        return IdempotencyOutcome.NOOP

    stored_members: dict[str, str] = meta.get("member_sha256", {})
    if not stored_members:
        # Re-derive from stored file.
        try:
            stored_members = validate_zip(output_path.read_bytes())
        except ZipValidationError:
            stored_members = {}

    if stored_members == new_member_hashes:
        return IdempotencyOutcome.EQUIVALENT

    # Same doc_id, different member content → hard failure.
    raise RuntimeError(
        f"CONFLICT: doc_id '{doc_id}' already exists at {output_path} "
        f"but its member checksums differ from the new download. "
        "Manual review required before overwriting."
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "identity",
    "Content-Type": "application/json",
    "Origin": "https://fsapartners.ed.gov",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
}


def post_json(
    url: str,
    payload: dict,
    *,
    referer: str = "https://fsapartners.ed.gov/",
    extra_headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> tuple[bytes, dict[str, str]]:
    """POST *payload* as JSON to *url* and return (body_bytes, response_headers)."""
    headers = {**_DEFAULT_HEADERS, "Referer": referer}
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        hdr = {k.lower(): v for k, v in resp.headers.items()}
    if hdr.get("content-encoding") == "gzip":
        body = gzip.decompress(body)
    return body, hdr


# ---------------------------------------------------------------------------
# Filename extraction
# ---------------------------------------------------------------------------

def filename_from_headers(headers: dict[str, str], fallback: str) -> str:
    disposition = headers.get("content-disposition", "")
    m = re.search(r'filename[^;=\n]*=\s*["\']?([^"\';\n]+)["\']?', disposition, re.I)
    if m:
        return m.group(1).strip()
    return fallback


# ---------------------------------------------------------------------------
# Metadata I/O
# ---------------------------------------------------------------------------

def write_metadata(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
