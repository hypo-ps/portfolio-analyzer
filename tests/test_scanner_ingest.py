from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import requests
from click.testing import CliRunner

from portfolio_analyzer import cli
from portfolio_analyzer.scanner import ingest
from portfolio_analyzer.scanner.bhavcopy import BhavRow
from portfolio_analyzer.scanner.db import open_db


def _sample_rows(trade_date: dt.date) -> list[BhavRow]:
    return [
        BhavRow(
            trade_date=trade_date, isin="INE009A01021", symbol="INFY",
            name="INFOSYS LIMITED", series="EQ",
            open=1295.0, high=1297.7, low=1255.9, close=1268.6,
            prev_close=1313.2, volume=20088378, turnover=25498272367.7, trades=477139,
        ),
        BhavRow(
            trade_date=trade_date, isin="INE528G01035", symbol="YESBANK",
            name="YES BANK LIMITED", series="BE",
            open=20.1, high=20.9, low=19.8, close=20.3,
            prev_close=20.0, volume=100000, turnover=2030000.0, trades=500,
        ),
    ]


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=resp)


def test_ingest_date_happy_path(tmp_path: Path, mocker):
    d = dt.date(2026, 4, 22)
    db_path = tmp_path / "s.db"
    mocker.patch.object(ingest.bc, "fetch_and_parse", return_value=_sample_rows(d))
    result = ingest.ingest_date(d, db_path=db_path)

    assert result.status == "ingested"
    assert result.rows == 2
    with open_db(db_path) as conn:
        stocks = conn.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0]
        bars = conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    assert stocks == 2 and bars == 2


def test_ingest_date_skips_when_already_ingested(tmp_path: Path, mocker):
    d = dt.date(2026, 4, 22)
    db_path = tmp_path / "s.db"
    fake = mocker.patch.object(ingest.bc, "fetch_and_parse", return_value=_sample_rows(d))
    ingest.ingest_date(d, db_path=db_path)
    assert fake.call_count == 1

    result = ingest.ingest_date(d, db_path=db_path)
    assert result.status == "skipped"
    assert fake.call_count == 1  # second call must not re-fetch


def test_ingest_date_force_reingest(tmp_path: Path, mocker):
    d = dt.date(2026, 4, 22)
    db_path = tmp_path / "s.db"
    fake = mocker.patch.object(ingest.bc, "fetch_and_parse", return_value=_sample_rows(d))
    ingest.ingest_date(d, db_path=db_path)
    result = ingest.ingest_date(d, db_path=db_path, force=True)
    assert result.status == "ingested"
    assert fake.call_count == 2


def test_ingest_date_returns_no_data_on_404(tmp_path: Path, mocker):
    d = dt.date(2026, 4, 18)  # saturday
    mocker.patch.object(ingest.bc, "fetch_and_parse", side_effect=_http_error(404))
    result = ingest.ingest_date(d, db_path=tmp_path / "s.db")
    assert result.status == "no_data"
    assert "404" in result.detail


def test_ingest_date_returns_error_on_other_http(tmp_path: Path, mocker):
    d = dt.date(2026, 4, 22)
    mocker.patch.object(ingest.bc, "fetch_and_parse", side_effect=_http_error(503))
    result = ingest.ingest_date(d, db_path=tmp_path / "s.db")
    assert result.status == "error"


def test_ingest_date_returns_no_data_on_empty_after_filter(tmp_path: Path, mocker):
    d = dt.date(2026, 4, 22)
    mocker.patch.object(ingest.bc, "fetch_and_parse", return_value=[])
    result = ingest.ingest_date(d, db_path=tmp_path / "s.db")
    assert result.status == "no_data"


def test_ingest_range_skips_weekends(tmp_path: Path, mocker):
    # 2026-04-17 Fri, 18 Sat, 19 Sun, 20 Mon, 21 Tue
    calls: list[dt.date] = []

    def fake_fetch(date: dt.date) -> list[BhavRow]:
        calls.append(date)
        return _sample_rows(date)

    mocker.patch.object(ingest.bc, "fetch_and_parse", side_effect=fake_fetch)
    results = ingest.ingest_range(
        dt.date(2026, 4, 17), dt.date(2026, 4, 21), db_path=tmp_path / "s.db",
    )
    assert [r.trade_date for r in results] == [
        dt.date(2026, 4, 17), dt.date(2026, 4, 20), dt.date(2026, 4, 21),
    ]
    assert calls == [r.trade_date for r in results]


def test_ingest_range_rejects_reversed_window(tmp_path: Path):
    import pytest
    with pytest.raises(ValueError):
        ingest.ingest_range(
            dt.date(2026, 4, 22), dt.date(2026, 4, 20), db_path=tmp_path / "s.db",
        )


def test_cli_scanner_ingest_smoke(tmp_path: Path, mocker):
    d = dt.date(2026, 4, 22)
    db_path = tmp_path / "s.db"
    mocker.patch.object(ingest.bc, "fetch_and_parse", return_value=_sample_rows(d))
    runner = CliRunner()
    res = runner.invoke(cli.main, [
        "scanner", "ingest", "--date", d.isoformat(), "--db", str(db_path),
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["status"] == "ingested"
    assert payload["rows"] == 2


def test_cli_scanner_status_on_missing_db(tmp_path: Path):
    runner = CliRunner()
    res = runner.invoke(cli.main, ["scanner", "status", "--db", str(tmp_path / "nope.db")])
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["exists"] is False


def test_cli_scanner_status_after_ingest(tmp_path: Path, mocker):
    d = dt.date(2026, 4, 22)
    db_path = tmp_path / "s.db"
    mocker.patch.object(ingest.bc, "fetch_and_parse", return_value=_sample_rows(d))
    ingest.ingest_date(d, db_path=db_path)
    runner = CliRunner()
    res = runner.invoke(cli.main, ["scanner", "status", "--db", str(db_path)])
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["exists"] is True
    assert payload["stocks"] == 2
    assert payload["bars"] == 2
    assert payload["latest"]["trade_date"] == d.isoformat()
