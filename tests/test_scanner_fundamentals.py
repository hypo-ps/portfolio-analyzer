from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import requests
from click.testing import CliRunner

from portfolio_analyzer import cli
from portfolio_analyzer.scanner import db as sdb
from portfolio_analyzer.scanner.bhavcopy import BhavRow
from portfolio_analyzer.scanner.fundamentals import ingest as fi
from portfolio_analyzer.scanner.fundamentals import screener

FIXTURES = Path(__file__).parent / "fixtures" / "screener"


def _bar(isin: str, symbol: str) -> BhavRow:
    return BhavRow(
        trade_date=dt.date(2024, 1, 1), isin=isin, symbol=symbol,
        name=f"{symbol} Ltd", series="EQ",
        open=100.0, high=100.0, low=100.0, close=100.0,
        prev_close=100.0, volume=1000, turnover=100000.0, trades=10,
    )


def _seed_universe(db_path: Path, pairs: list[tuple[str, str]]) -> None:
    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [_bar(isin, sym) for isin, sym in pairs])


def test_clean_number_handles_currency_and_percent():
    assert screener._clean_number("₹ 5,03,200 Cr.") == 503200.0
    assert screener._clean_number("28%") == pytest.approx(0.28)
    assert screener._clean_number("(12.5)") == -12.5
    assert screener._clean_number("-") is None
    assert screener._clean_number("") is None


def test_fiscal_year_from_header():
    assert screener._fiscal_year_from_header("Mar 2024") == 2024
    assert screener._fiscal_year_from_header("TTM") is None
    assert screener._fiscal_year_from_header("Mar 20189m") is None
    assert screener._fiscal_year_from_header("") is None


def test_parse_infy_fixture():
    html = (FIXTURES / "INFY.html").read_text()
    c = screener.parse_company("INFY", html)
    assert "Infosys" in c.name
    assert c.variant == "consolidated"
    assert c.meta["sector"] == "Information Technology"
    assert c.meta["industry"] == "IT - Software"
    assert c.meta["market_cap_cr"] and c.meta["market_cap_cr"] > 100000
    assert c.meta["stock_pe"] and 5 < c.meta["stock_pe"] < 60
    assert 0.1 < c.meta["roe_latest"] < 0.6
    assert len(c.annual_financials) >= 8
    last = c.annual_financials[-1]
    assert "sales_cr" in last and last["sales_cr"] > 0
    assert "net_profit_cr" in last and last["net_profit_cr"] > 0
    assert len(c.annual_ratios) >= 8


def test_parse_kpittech_fixture():
    html = (FIXTURES / "KPITTECH.html").read_text()
    c = screener.parse_company("KPITTECH", html)
    assert "KPIT" in c.name
    assert c.meta["market_cap_cr"] and c.meta["market_cap_cr"] > 1000
    years = [r["fiscal_year"] for r in c.annual_financials]
    assert years == sorted(years)
    assert len(set(years)) == len(years)


def test_db_roundtrip_for_parsed_company(tmp_path: Path):
    db_path = tmp_path / "s.db"
    _seed_universe(db_path, [("INE009A01021", "INFY")])
    html = (FIXTURES / "INFY.html").read_text()
    c = screener.parse_company("INFY", html)
    with sdb.open_db(db_path) as conn:
        sdb.upsert_fundamentals_meta(conn, "INE009A01021", screener.SOURCE, c.meta)
        n_fin = sdb.upsert_financials_annual(
            conn, "INE009A01021", screener.SOURCE, c.variant, c.annual_financials,
        )
        n_rat = sdb.upsert_ratios_annual(
            conn, "INE009A01021", screener.SOURCE, c.variant, c.annual_ratios,
        )
        sdb.record_fundamentals_ingestion(
            conn, "INE009A01021", screener.SOURCE, "ok", report_type=c.variant,
        )
    assert n_fin == len(c.annual_financials)
    assert n_rat == len(c.annual_ratios)
    with sdb.open_db(db_path) as conn:
        summary = sdb.fundamentals_summary(conn)
    assert summary["companies_covered"] == 1
    assert summary["annual_rows"] == n_fin
    assert summary["by_status"] == {"ok": 1}


def test_upserts_are_idempotent(tmp_path: Path):
    db_path = tmp_path / "s.db"
    _seed_universe(db_path, [("INE009A01021", "INFY")])
    html = (FIXTURES / "INFY.html").read_text()
    c = screener.parse_company("INFY", html)
    for _ in range(2):
        with sdb.open_db(db_path) as conn:
            sdb.upsert_fundamentals_meta(conn, "INE009A01021", screener.SOURCE, c.meta)
            sdb.upsert_financials_annual(
                conn, "INE009A01021", screener.SOURCE, c.variant, c.annual_financials,
            )
    with sdb.open_db(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM financials_annual").fetchone()[0]
        m = conn.execute("SELECT COUNT(*) FROM fundamentals_meta").fetchone()[0]
    assert n == len(c.annual_financials)
    assert m == 1


def _fake_fetch_infy(symbol, **kwargs):
    return ("consolidated", (FIXTURES / "INFY.html").read_text())


def test_ingest_fundamentals_happy_path(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_universe(db_path, [("INE009A01021", "INFY")])
    mocker.patch.object(fi.screener, "fetch_company", side_effect=_fake_fetch_infy)
    result = fi.ingest_fundamentals(db_path=db_path, only_symbols=["INFY"])
    assert result.processed == 1
    assert result.ok == 1
    assert result.not_found == 0
    with sdb.open_db(db_path) as conn:
        summary = sdb.fundamentals_summary(conn)
    assert summary["companies_covered"] == 1
    assert summary["annual_rows"] > 0


def test_ingest_fundamentals_skips_fresh(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_universe(db_path, [("INE009A01021", "INFY")])
    mocker.patch.object(fi.screener, "fetch_company", side_effect=_fake_fetch_infy)
    fi.ingest_fundamentals(db_path=db_path, only_symbols=["INFY"])
    spy = mocker.spy(fi.screener, "fetch_company")
    result = fi.ingest_fundamentals(db_path=db_path, only_symbols=["INFY"])
    assert result.skipped == 1
    assert spy.call_count == 0


def test_ingest_fundamentals_force_refetches(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_universe(db_path, [("INE009A01021", "INFY")])
    mocker.patch.object(fi.screener, "fetch_company", side_effect=_fake_fetch_infy)
    fi.ingest_fundamentals(db_path=db_path, only_symbols=["INFY"])
    result = fi.ingest_fundamentals(db_path=db_path, only_symbols=["INFY"], force=True)
    assert result.ok == 1
    assert result.skipped == 0


def test_ingest_fundamentals_records_not_found(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_universe(db_path, [("INE0000XXXXX", "NOPE")])
    mocker.patch.object(
        fi.screener, "fetch_company",
        side_effect=screener.ScreenerNotFoundError("gone"),
    )
    result = fi.ingest_fundamentals(db_path=db_path, only_symbols=["NOPE"])
    assert result.not_found == 1
    with sdb.open_db(db_path) as conn:
        row = conn.execute(
            "SELECT status, detail FROM fundamentals_ingestion_log WHERE isin=?",
            ("INE0000XXXXX",),
        ).fetchone()
    assert row[0] == "not_found"
    assert "gone" in row[1]


def test_ingest_fundamentals_records_network_error(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_universe(db_path, [("INE009A01021", "INFY")])
    mocker.patch.object(
        fi.screener, "fetch_company",
        side_effect=requests.ConnectionError("boom"),
    )
    result = fi.ingest_fundamentals(db_path=db_path, only_symbols=["INFY"])
    assert result.error == 1


def test_cli_fundamentals_ingest_smoke(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_universe(db_path, [("INE009A01021", "INFY")])
    mocker.patch.object(fi.screener, "fetch_company", side_effect=_fake_fetch_infy)
    runner = CliRunner()
    res = runner.invoke(cli.main, [
        "scanner", "fundamentals-ingest",
        "--symbol", "INFY", "--db", str(db_path),
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["ok"] == 1
    assert payload["processed"] == 1


def test_cli_status_includes_fundamentals_section(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_universe(db_path, [("INE009A01021", "INFY")])
    mocker.patch.object(fi.screener, "fetch_company", side_effect=_fake_fetch_infy)
    fi.ingest_fundamentals(db_path=db_path, only_symbols=["INFY"])
    runner = CliRunner()
    res = runner.invoke(cli.main, ["scanner", "status", "--db", str(db_path)])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["fundamentals"]["companies_covered"] == 1
    assert payload["fundamentals"]["by_status"] == {"ok": 1}
