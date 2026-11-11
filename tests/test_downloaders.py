"""Narrow tests for _download_utils, download_weekly_closed_school_search_file,
and download_weekly_school_file.

All HTTP calls are mocked — no live network required.
Run with:  python -m pytest tests/test_downloaders.py -v
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts/ to sys.path so relative imports work.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import _download_utils as utils
from _download_utils import (
    IdempotencyOutcome,
    ZipValidationError,
    check_idempotency,
    filename_from_headers,
    sha256_bytes,
    validate_zip,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_zip(members: dict[str, bytes], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    """Build an in-memory ZIP with the given members."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


GOOD_ZIP = make_zip({"WKCLOSED.xlsx": b"fake xlsx content"})
GOOD_ZIP_HASH = sha256_bytes(GOOD_ZIP)
GOOD_MEMBERS = {"WKCLOSED.xlsx": sha256_bytes(b"fake xlsx content")}

SCHOOL_ZIP = make_zip({"schfile_access.dat": b"record" * 100})
SCHOOL_ZIP_HASH = sha256_bytes(SCHOOL_ZIP)


# ---------------------------------------------------------------------------
# validate_zip
# ---------------------------------------------------------------------------

class TestValidateZip:
    def test_valid_zip_returns_member_hashes(self):
        members = validate_zip(GOOD_ZIP)
        assert "WKCLOSED.xlsx" in members
        assert members["WKCLOSED.xlsx"] == sha256_bytes(b"fake xlsx content")

    def test_empty_body_raises(self):
        with pytest.raises(ZipValidationError, match="too small"):
            validate_zip(b"")

    def test_tiny_body_raises(self):
        with pytest.raises(ZipValidationError, match="too small"):
            validate_zip(b"\x00" * 10)

    def test_html_body_raises(self):
        html = b"<!DOCTYPE html><html><title>Login</title></html>"
        with pytest.raises(ZipValidationError, match="HTML"):
            validate_zip(html)

    def test_html_body_no_doctype_raises(self):
        html = b"<html><body>Session expired</body></html>"
        with pytest.raises(ZipValidationError, match="HTML"):
            validate_zip(html)

    def test_invalid_zip_magic_raises(self):
        with pytest.raises(ZipValidationError, match="not a valid ZIP"):
            validate_zip(b"this is not a zip file at all, definitely not" * 10)

    def test_path_traversal_dotdot_raises(self):
        bad = make_zip({"../evil.py": b"import os"})
        with pytest.raises(ZipValidationError, match="unsafe path"):
            validate_zip(bad)

    def test_path_traversal_abs_raises(self):
        bad = make_zip({"/etc/passwd": b"root:x:0:0"})
        with pytest.raises(ZipValidationError, match="unsafe path"):
            validate_zip(bad)

    def test_multi_member_zip(self):
        multi = make_zip({"a.dat": b"aaa", "b.dat": b"bbb"})
        members = validate_zip(multi)
        assert set(members) == {"a.dat", "b.dat"}


# ---------------------------------------------------------------------------
# check_idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_new_when_file_absent(self, tmp_path):
        out = tmp_path / "report.zip"
        result = check_idempotency(out, GOOD_ZIP_HASH, GOOD_MEMBERS, "DOC1")
        assert result == IdempotencyOutcome.NEW

    def test_noop_when_outer_hash_matches(self, tmp_path):
        out = tmp_path / "report.zip"
        out.write_bytes(GOOD_ZIP)
        meta = tmp_path / "report_metadata.json"
        meta.write_text(json.dumps({"outer_sha256": GOOD_ZIP_HASH}), encoding="utf-8")
        result = check_idempotency(out, GOOD_ZIP_HASH, GOOD_MEMBERS, "DOC1", metadata_path=meta)
        assert result == IdempotencyOutcome.NOOP

    def test_equivalent_when_members_match_but_outer_differs(self, tmp_path):
        # Use ZIP_STORED to guarantee a different outer hash than GOOD_ZIP (ZIP_DEFLATED).
        alt_zip = make_zip({"WKCLOSED.xlsx": b"fake xlsx content"}, compression=zipfile.ZIP_STORED)
        alt_hash = sha256_bytes(alt_zip)
        assert alt_hash != GOOD_ZIP_HASH  # sanity: different compression → different outer bytes

        out = tmp_path / "report.zip"
        out.write_bytes(GOOD_ZIP)
        meta = tmp_path / "report_metadata.json"
        meta.write_text(
            json.dumps({"outer_sha256": GOOD_ZIP_HASH, "member_sha256": GOOD_MEMBERS}),
            encoding="utf-8",
        )
        # Provide a different outer hash but same member hashes.
        result = check_idempotency(out, alt_hash, GOOD_MEMBERS, "DOC1", metadata_path=meta)
        assert result == IdempotencyOutcome.EQUIVALENT

    def test_conflict_raises(self, tmp_path):
        out = tmp_path / "report.zip"
        out.write_bytes(GOOD_ZIP)
        meta = tmp_path / "report_metadata.json"
        different_members = {"WKCLOSED.xlsx": sha256_bytes(b"DIFFERENT content")}
        meta.write_text(
            json.dumps({"outer_sha256": GOOD_ZIP_HASH, "member_sha256": GOOD_MEMBERS}),
            encoding="utf-8",
        )
        different_outer = sha256_bytes(b"different outer zip bytes")
        with pytest.raises(RuntimeError, match="CONFLICT"):
            check_idempotency(out, different_outer, different_members, "DOC1", metadata_path=meta)


# ---------------------------------------------------------------------------
# filename_from_headers
# ---------------------------------------------------------------------------

class TestFilenameFromHeaders:
    def test_extracts_quoted_filename(self):
        hdrs = {"content-disposition": 'attachment; filename="WEEKLY_REPORT_123.zip"'}
        assert filename_from_headers(hdrs, "fallback.zip") == "WEEKLY_REPORT_123.zip"

    def test_extracts_unquoted_filename(self):
        hdrs = {"content-disposition": "attachment; filename=REPORT.zip"}
        assert filename_from_headers(hdrs, "fallback.zip") == "REPORT.zip"

    def test_uses_fallback_when_header_absent(self):
        assert filename_from_headers({}, "fallback.zip") == "fallback.zip"

    def test_uses_fallback_when_no_filename_in_disposition(self):
        hdrs = {"content-disposition": "attachment"}
        assert filename_from_headers(hdrs, "fallback.zip") == "fallback.zip"


# ---------------------------------------------------------------------------
# Closed-school downloader (integration-style with mocked HTTP)
# ---------------------------------------------------------------------------

MOCK_SEARCH_RESPONSE = json.dumps([
    {"docType": "WEEKLY_CLOSED_REPORT", "docStorageId": "DOC999", "procDate": "2026-11-08"},
]).encode("utf-8")


def _make_mock_response(body: bytes, headers: dict[str, str] | None = None) -> MagicMock:
    mock = MagicMock()
    mock.read.return_value = body
    mock.headers = MagicMock()
    items = list((headers or {}).items())
    mock.headers.items.return_value = items
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


class TestClosedSchoolDownloader:
    def _run(self, tmp_path, extra_argv=None):
        import download_weekly_closed_school_search_file as mod
        argv = [
            "--output-folder", str(tmp_path),
            "--search-url", "https://mock.example/search",
            "--download-url", "https://mock.example/download",
        ]
        if extra_argv:
            argv += extra_argv
        with patch("sys.argv", ["dl"] + argv):
            args = mod.parse_args()
        return mod, args

    def test_new_download_saves_file(self, tmp_path):
        import download_weekly_closed_school_search_file as mod

        search_resp = _make_mock_response(MOCK_SEARCH_RESPONSE)
        download_resp = _make_mock_response(
            GOOD_ZIP,
            {"content-disposition": 'attachment; filename="WEEKLY_CLOSED_REPORT_DOC999.zip"'},
        )

        with patch("_download_utils.urllib.request.urlopen", side_effect=[search_resp, download_resp]):
            with patch("sys.argv", [
                "dl",
                "--output-folder", str(tmp_path),
                "--search-url", "https://mock.example/search",
                "--download-url", "https://mock.example/download",
            ]):
                rc = mod.main()

        assert rc == 0
        saved = list(tmp_path.rglob("*.zip"))
        assert len(saved) == 1
        assert saved[0].read_bytes() == GOOD_ZIP

    def test_html_response_fails(self, tmp_path):
        import download_weekly_closed_school_search_file as mod

        html_body = b"<!DOCTYPE html><html><title>Login</title></html>"
        search_resp = _make_mock_response(MOCK_SEARCH_RESPONSE)
        download_resp = _make_mock_response(html_body)

        with patch("_download_utils.urllib.request.urlopen", side_effect=[search_resp, download_resp]):
            with patch("sys.argv", [
                "dl",
                "--output-folder", str(tmp_path),
                "--search-url", "https://mock.example/search",
                "--download-url", "https://mock.example/download",
            ]):
                rc = mod.main()

        assert rc == 1
        assert not list(tmp_path.rglob("*.zip"))

    def test_empty_search_results_raises(self, tmp_path):
        import download_weekly_closed_school_search_file as mod

        empty_resp = _make_mock_response(b"[]")
        with patch("_download_utils.urllib.request.urlopen", return_value=empty_resp):
            with patch("sys.argv", [
                "dl",
                "--output-folder", str(tmp_path),
                "--search-url", "https://mock.example/search",
                "--download-url", "https://mock.example/download",
            ]):
                with pytest.raises(RuntimeError, match="empty"):
                    mod.main()

    def test_dry_run_does_not_write(self, tmp_path):
        import download_weekly_closed_school_search_file as mod

        search_resp = _make_mock_response(MOCK_SEARCH_RESPONSE)
        with patch("_download_utils.urllib.request.urlopen", return_value=search_resp):
            with patch("sys.argv", [
                "dl",
                "--output-folder", str(tmp_path),
                "--search-url", "https://mock.example/search",
                "--download-url", "https://mock.example/download",
                "--dry-run",
            ]):
                rc = mod.main()

        assert rc == 0
        assert not list(tmp_path.rglob("*.zip"))

    def test_idempotent_noop_on_repeat(self, tmp_path):
        import download_weekly_closed_school_search_file as mod

        def run_once():
            search_resp = _make_mock_response(MOCK_SEARCH_RESPONSE)
            download_resp = _make_mock_response(
                GOOD_ZIP,
                {"content-disposition": 'attachment; filename="WEEKLY_CLOSED_REPORT_DOC999.zip"'},
            )
            with patch("_download_utils.urllib.request.urlopen", side_effect=[search_resp, download_resp]):
                with patch("sys.argv", [
                    "dl",
                    "--output-folder", str(tmp_path),
                    "--search-url", "https://mock.example/search",
                    "--download-url", "https://mock.example/download",
                ]):
                    return mod.main()

        assert run_once() == 0
        # Second run: search is called again but download should not be needed.
        search_resp2 = _make_mock_response(MOCK_SEARCH_RESPONSE)
        with patch("_download_utils.urllib.request.urlopen", return_value=search_resp2):
            with patch("sys.argv", [
                "dl",
                "--output-folder", str(tmp_path),
                "--search-url", "https://mock.example/search",
                "--download-url", "https://mock.example/download",
            ]):
                rc2 = mod.main()
        assert rc2 == 0
        # Still only one ZIP on disk.
        assert len(list(tmp_path.rglob("*.zip"))) == 1


# ---------------------------------------------------------------------------
# Weekly School File downloader (integration-style with mocked HTTP)
# ---------------------------------------------------------------------------

MOCK_SCHOOL_SEARCH = json.dumps([
    {"docType": "SCHOOL_FILE_ACCESS", "fileName": "schfile_access_format_20261105.dat.zip",
     "docStorageId": "SCH001", "procDate": "2026-11-05"},
    {"docType": "SCHOOL_FILE_DATABASE", "fileName": "schfile_extract_20261105.dat.zip",
     "docStorageId": "SCH002", "procDate": "2026-11-05"},
]).encode("utf-8")


class TestWeeklySchoolDownloader:
    def test_access_format_download(self, tmp_path):
        import download_weekly_school_file as mod

        search_resp = _make_mock_response(MOCK_SCHOOL_SEARCH)
        dl_resp = _make_mock_response(
            SCHOOL_ZIP,
            {"content-disposition": 'attachment; filename="schfile_access_format_20261105.dat.zip"'},
        )
        with patch("_download_utils.urllib.request.urlopen", side_effect=[search_resp, dl_resp]):
            with patch("sys.argv", [
                "dl",
                "--search-url", "https://mock.example/school-search",
                "--format", "access",
                "--output-folder", str(tmp_path),
                "--download-url", "https://mock.example/download",
            ]):
                rc = mod.main()

        assert rc == 0
        saved = list(tmp_path.rglob("*.zip"))
        assert len(saved) == 1
        assert "access" in str(saved[0])

    def test_database_format_download(self, tmp_path):
        import download_weekly_school_file as mod

        search_resp = _make_mock_response(MOCK_SCHOOL_SEARCH)
        dl_resp = _make_mock_response(
            SCHOOL_ZIP,
            {"content-disposition": 'attachment; filename="schfile_extract_20261105.dat.zip"'},
        )
        with patch("_download_utils.urllib.request.urlopen", side_effect=[search_resp, dl_resp]):
            with patch("sys.argv", [
                "dl",
                "--search-url", "https://mock.example/school-search",
                "--format", "database",
                "--output-folder", str(tmp_path),
                "--download-url", "https://mock.example/download",
            ]):
                rc = mod.main()

        assert rc == 0
        saved = list(tmp_path.rglob("*.zip"))
        assert len(saved) == 1
        assert "database" in str(saved[0])

    def test_no_matching_format_fails(self, tmp_path):
        import download_weekly_school_file as mod

        # Search returns only an access entry; requesting database should fail.
        access_only = json.dumps([
            {"docType": "SCHOOL_FILE_ACCESS", "fileName": "schfile_access_20261105.dat.zip",
             "docStorageId": "SCH001", "procDate": "2026-11-05"},
        ]).encode("utf-8")
        search_resp = _make_mock_response(access_only)
        with patch("_download_utils.urllib.request.urlopen", return_value=search_resp):
            with patch("sys.argv", [
                "dl",
                "--search-url", "https://mock.example/school-search",
                "--format", "database",
                "--output-folder", str(tmp_path),
                "--download-url", "https://mock.example/download",
            ]):
                rc = mod.main()
        assert rc == 1

    def test_html_download_fails(self, tmp_path):
        import download_weekly_school_file as mod

        search_resp = _make_mock_response(MOCK_SCHOOL_SEARCH)
        html_resp = _make_mock_response(b"<html><title>Login</title></html>")
        with patch("_download_utils.urllib.request.urlopen", side_effect=[search_resp, html_resp]):
            with patch("sys.argv", [
                "dl",
                "--search-url", "https://mock.example/school-search",
                "--format", "access",
                "--output-folder", str(tmp_path),
                "--download-url", "https://mock.example/download",
            ]):
                rc = mod.main()
        assert rc == 1

    def test_dry_run_does_not_write(self, tmp_path):
        import download_weekly_school_file as mod

        search_resp = _make_mock_response(MOCK_SCHOOL_SEARCH)
        with patch("_download_utils.urllib.request.urlopen", return_value=search_resp):
            with patch("sys.argv", [
                "dl",
                "--search-url", "https://mock.example/school-search",
                "--format", "access",
                "--output-folder", str(tmp_path),
                "--download-url", "https://mock.example/download",
                "--dry-run",
            ]):
                rc = mod.main()
        assert rc == 0
        assert not list(tmp_path.rglob("*.zip"))
