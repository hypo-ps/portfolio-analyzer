from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

import pytest

from portfolio_analyzer import refresh
from portfolio_analyzer.instruments import load_sector_map

NSE_500_CSV = (
    "Company Name,Industry,Symbol,Series,ISIN Code\n"
    "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
    "Tata Consultancy Services Ltd.,Information Technology,TCS,EQ,INE467B01029\n"
    "Infosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n"
)
NSE_50_CSV = (
    "Company Name,Industry,Symbol,Series,ISIN Code\n"
    "Reliance Industries Ltd.,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018\n"
    "HDFC Bank Ltd.,Financial Services,HDFCBANK,EQ,INE040A01034\n"
)


def test_parse_nse_csv_extracts_symbols_and_industries():
    parsed = refresh._parse_nse_csv(NSE_500_CSV)
    assert parsed.symbols == ["RELIANCE", "TCS", "INFY"]
    assert parsed.industry_by_symbol["RELIANCE"] == "Oil Gas & Consumable Fuels"
    assert parsed.industry_by_symbol["INFY"] == "Information Technology"


def test_parse_nse_csv_skips_blank_symbols():
    csv_text = NSE_500_CSV + ",Misc,,EQ,\n"
    parsed = refresh._parse_nse_csv(csv_text)
    assert "" not in parsed.symbols
    assert len(parsed.symbols) == 3


def _patch_downloads(mocker, n500_text: str = NSE_500_CSV, n50_text: str = NSE_50_CSV):
    def fake_download(url: str) -> str:
        if "nifty500" in url:
            return n500_text
        if "nifty50" in url:
            return n50_text
        raise AssertionError(f"Unexpected URL: {url}")

    return mocker.patch.object(refresh, "_download", side_effect=fake_download)


def test_refresh_writes_all_three_files(tmp_path: Path, mocker):
    # Use >=100 symbols for NIFTY 500 to satisfy sanity check.
    big_500 = "Company Name,Industry,Symbol,Series,ISIN Code\n" + "\n".join(
        f"Co {i},Industry {i % 3},SYM{i},EQ,INE{i:08d}" for i in range(150)
    )
    big_50 = "Company Name,Industry,Symbol,Series,ISIN Code\n" + "\n".join(
        f"Co {i},Industry {i % 2},N50_{i},EQ,INE{i:08d}" for i in range(20)
    )
    _patch_downloads(mocker, n500_text=big_500, n50_text=big_50)

    changed = refresh.refresh_constituents(data_dir=tmp_path, force=True)
    assert changed is True

    n500 = (tmp_path / "nifty500.csv").read_text()
    assert "SYM0" in n500 and "SYM149" in n500
    assert n500.splitlines()[1] == "symbol"

    n50 = (tmp_path / "nifty50.csv").read_text()
    assert "N50_0" in n50 and "N50_19" in n50

    auto = (tmp_path / "sector_map.auto.csv").read_text()
    assert "SYM0,Industry 0" in auto
    assert "N50_0,Industry 0" in auto


def test_refresh_skips_when_fresh_today(tmp_path: Path, mocker):
    for name in ("nifty500.csv", "nifty50.csv", "sector_map.auto.csv"):
        (tmp_path / name).write_text("stub\n")
    download = mocker.patch.object(refresh, "_download")
    changed = refresh.refresh_constituents(data_dir=tmp_path, force=False)
    assert changed is False
    download.assert_not_called()


def _big_csv(symbol_prefix: str, count: int) -> str:
    return "Company Name,Industry,Symbol,Series,ISIN Code\n" + "\n".join(
        f"Co {i},Industry {i % 3},{symbol_prefix}{i},EQ,INE{i:08d}" for i in range(count)
    )


def test_refresh_reruns_when_file_mtime_stale(tmp_path: Path, mocker):
    for name in ("nifty500.csv", "nifty50.csv", "sector_map.auto.csv"):
        p = tmp_path / name
        p.write_text("stub\n")
        stale = time.time() - 2 * 86400
        os.utime(p, (stale, stale))
    _patch_downloads(mocker, n500_text=_big_csv("SYM", 150), n50_text=_big_csv("N50_", 20))
    changed = refresh.refresh_constituents(data_dir=tmp_path, force=False)
    assert changed is True


def test_refresh_graceful_on_network_error(tmp_path: Path, mocker):
    import requests

    mocker.patch.object(refresh, "_download", side_effect=requests.ConnectionError("boom"))
    changed = refresh.refresh_constituents(data_dir=tmp_path, force=True)
    assert changed is False
    assert not (tmp_path / "nifty500.csv").exists()


def test_refresh_rejects_suspiciously_small_payload(tmp_path: Path, mocker):
    tiny_500 = "Company Name,Industry,Symbol,Series,ISIN Code\nCo,Ind,ONE,EQ,INE\n"
    _patch_downloads(mocker, n500_text=tiny_500)
    changed = refresh.refresh_constituents(data_dir=tmp_path, force=True)
    assert changed is False
    assert not (tmp_path / "nifty500.csv").exists()


def test_sector_map_user_overrides_auto(tmp_path: Path, mocker):
    big_500 = _big_csv("SYM", 150) + "\nReliance,Oil Gas & Consumable Fuels,RELIANCE,EQ,INE002A01018"
    _patch_downloads(mocker, n500_text=big_500, n50_text=_big_csv("N50_", 20))
    assert refresh.refresh_constituents(data_dir=tmp_path, force=True) is True

    (tmp_path / "sector_map.csv").write_text("symbol,sector\nRELIANCE,ENERGY\n")

    auto = load_sector_map(tmp_path / "sector_map.auto.csv")
    user = load_sector_map(tmp_path / "sector_map.csv")
    merged = {**auto, **user}
    assert merged["RELIANCE"] == "ENERGY"
    assert merged["SYM0"] == "Industry 0"
