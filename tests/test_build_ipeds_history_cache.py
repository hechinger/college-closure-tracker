"""Tests for the pre-2014 IPEDS history cache builder."""

from __future__ import annotations

import csv
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_ipeds_history_cache import (
    CACHE_FIELDS,
    _effy_totals,
    _hloffer_to_hdegofr1,
    _read_zip_csv,
    main,
)


def _zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return path


HD_OLD = (
    "UNITID,INSTNM,CITY,STABBR,OPEID,SECTOR,ICLEVEL,CONTROL,HLOFFER,DEGGRANT\n"
    "100,Alpha College,Boston,MA,00100200,2,1,2,7,1\n"
    "200,Beta Institute,Austin,TX,00200300,9,3,3,2,2\n"
)


def test_hloffer_mapping():
    assert _hloffer_to_hdegofr1("9", "1") == "14"
    assert _hloffer_to_hdegofr1("7", "1") == "20"
    assert _hloffer_to_hdegofr1("5", "1") == "30"
    assert _hloffer_to_hdegofr1("3", "1") == "40"
    assert _hloffer_to_hdegofr1("9", "2") == "0"   # not degree-granting
    assert _hloffer_to_hdegofr1("-3", "1") == "-3"


def test_effy_totals_uses_total_rows_only():
    rows = [
        {"UNITID": "100", "EFFYLEV": "2", "EFYTOTLT": "50"},
        {"UNITID": "100", "EFFYLEV": "1", "EFYTOTLT": "80"},
        {"UNITID": "200", "LSTUDY": "999", "EFYTOTLT": "30"},
    ]
    assert _effy_totals(rows) == {"100": "80", "200": "30"}


def test_read_zip_prefers_original_over_revised(tmp_path):
    z = _zip(tmp_path / "EFFY2010.zip", {
        "effy2010.csv": "UNITID,EFFYLEV,EFYTOTLT\n100,1,80\n",
        "effy2010_rv.csv": "UNITID,EFFYLEV,EFYTOTLT\n100,1,99\n",
    })
    assert _read_zip_csv(z)[0]["EFYTOTLT"] == "80"


def test_main_builds_year_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _zip(src / "HD2005.zip", {"hd2005.csv": HD_OLD})
    _zip(src / "EFFY2005.zip", {"effy2005.csv": "UNITID,EFFYLEV,EFYTOTLT\n100,1,1234\n"})
    out = tmp_path / "out"
    assert main(["--source-dir", str(src), "--out-dir", str(out),
                 "--min-year", "2005", "--max-year", "2005"]) == 0
    with (out / "ipeds_history_year_2005.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == CACHE_FIELDS
    alpha, beta = rows
    assert (alpha["opeid"], alpha["year"], alpha["level"], alpha["highest_degree"]) == (
        "00100200", "2005", "1", "20")
    assert alpha["enrollment_headcount_total"] == "1234"
    assert beta["highest_degree"] == "0"
    assert beta["enrollment_headcount_total"] == ""


def test_main_errors_without_archives(tmp_path):
    assert main(["--source-dir", str(tmp_path)]) == 1
