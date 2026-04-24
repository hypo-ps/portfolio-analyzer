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
from portfolio_analyzer.scanner.vcp import fundamentals as vf

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


def test_period_end_from_header():
    assert screener._period_end_from_header("Mar 2023") == "2023-03-31"
    assert screener._period_end_from_header("Jun 2024") == "2024-06-30"
    assert screener._period_end_from_header("Sep 2025") == "2025-09-30"
    assert screener._period_end_from_header("Dec 2025") == "2025-12-31"
    assert screener._period_end_from_header("TTM") is None
    assert screener._period_end_from_header("Mar 20233m") is None
    assert screener._period_end_from_header("") is None


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
    assert len(c.quarterly_financials) >= 8
    periods = [r["period_end"] for r in c.quarterly_financials]
    assert periods == sorted(periods)
    assert len(set(periods)) == len(periods)
    q_last = c.quarterly_financials[-1]
    assert q_last["sales_cr"] and q_last["sales_cr"] > 0
    assert q_last["net_profit_cr"] and q_last["net_profit_cr"] > 0
    assert "dividend_payout_pct" not in q_last


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
        n_q = sdb.upsert_financials_quarterly(
            conn, "INE009A01021", screener.SOURCE, c.variant, c.quarterly_financials,
        )
        sdb.record_fundamentals_ingestion(
            conn, "INE009A01021", screener.SOURCE, "ok", report_type=c.variant,
        )
    assert n_fin == len(c.annual_financials)
    assert n_rat == len(c.annual_ratios)
    assert n_q == len(c.quarterly_financials)
    with sdb.open_db(db_path) as conn:
        summary = sdb.fundamentals_summary(conn)
        q_rows = conn.execute(
            "SELECT period_end, sales_cr, net_profit_cr FROM financials_quarterly "
            "WHERE isin=? ORDER BY period_end",
            ("INE009A01021",),
        ).fetchall()
    assert summary["companies_covered"] == 1
    assert summary["annual_rows"] == n_fin
    assert summary["quarterly_rows"] == n_q
    assert summary["by_status"] == {"ok": 1}
    assert [r[0] for r in q_rows] == sorted(r[0] for r in q_rows)
    assert all(r[1] is not None and r[1] > 0 for r in q_rows)


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
            sdb.upsert_financials_quarterly(
                conn, "INE009A01021", screener.SOURCE, c.variant, c.quarterly_financials,
            )
    with sdb.open_db(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM financials_annual").fetchone()[0]
        m = conn.execute("SELECT COUNT(*) FROM fundamentals_meta").fetchone()[0]
        q = conn.execute("SELECT COUNT(*) FROM financials_quarterly").fetchone()[0]
    assert n == len(c.annual_financials)
    assert m == 1
    assert q == len(c.quarterly_financials)


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
    assert summary["quarterly_rows"] > 0


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
    assert payload["fundamentals"]["quarterly_rows"] > 0



# ---------- quarterly feature math (D-S25) ----------

def _qrows(n: int, sales_growth: float = 0.0, profit_growth: float = 0.0,
           opm: float = 0.20, opm_trend: float = 0.0) -> list[tuple]:
    """Build n quarterly (sales, np, opm) tuples sorted ASC by period_end.

    Sales/profit compound quarter-over-quarter; OPM drifts linearly by
    ``opm_trend / (n-1)`` each step so the latest-vs-4Q-mean delta is
    controllable in tests.
    """
    rows = []
    s, p = 100.0, 20.0
    for i in range(n):
        om = opm + (opm_trend * i / max(n - 1, 1))
        rows.append((s, p, om))
        s *= (1.0 + sales_growth)
        p *= (1.0 + profit_growth)
    return rows


def test_quarterly_features_requires_eight_quarters_for_accel():
    # 7 quarters → TTM + OPM trend present, accel + TTM-YoY absent.
    feats = vf._quarterly_features(_qrows(7, sales_growth=0.05, profit_growth=0.05))
    assert feats["ttm_sales_cr"] is not None
    assert feats["opm_trend"] is not None
    assert feats["ttm_sales_growth_yoy"] is None
    assert feats["sales_accel_smoothed"] is None
    assert feats["profit_accel_smoothed"] is None


def test_quarterly_features_ttm_and_accel_on_eight_quarters():
    feats = vf._quarterly_features(_qrows(8, sales_growth=0.05, profit_growth=0.06))
    # TTM YoY should be positive given compounding growth.
    assert feats["ttm_sales_growth_yoy"] is not None
    assert feats["ttm_sales_growth_yoy"] > 0
    assert feats["ttm_profit_growth_yoy"] > 0
    # Accel is 0 for constant QoQ growth (geometric stability) ± rounding.
    assert abs(feats["sales_accel_smoothed"]) < 1e-9
    assert abs(feats["profit_accel_smoothed"]) < 1e-9


def test_quarterly_features_positive_opm_trend():
    feats = vf._quarterly_features(_qrows(8, opm=0.18, opm_trend=0.04))
    # Last quarter is 4pp above base; mean of last 4Q lies below latest.
    assert feats["opm_trend"] is not None and feats["opm_trend"] > 0


def test_quarterly_features_handles_missing_sales():
    rows = _qrows(8, sales_growth=0.05)
    # Knock out the oldest sales value → prior-TTM base incomplete → YoY None.
    rows[0] = (None, rows[0][1], rows[0][2])
    feats = vf._quarterly_features(rows)
    assert feats["ttm_sales_growth_yoy"] is None
    # Current TTM still intact (last 4 sales are populated).
    assert feats["ttm_sales_cr"] is not None


def test_quarter_yoy_guards_against_nonpositive_base():
    assert vf._quarter_yoy(100.0, 0.0) is None
    assert vf._quarter_yoy(100.0, -10.0) is None
    assert vf._quarter_yoy(None, 50.0) is None
    assert vf._quarter_yoy(120.0, 100.0) == pytest.approx(0.20)


def test_load_fundamental_features_includes_quarterly(tmp_path: Path):
    import sqlite3
    db_path = tmp_path / "q.db"
    _seed_universe(db_path, [("INE_Q_0001", "QUART")])
    isin = "INE_Q_0001"
    with sdb.open_db(db_path) as conn:
        # Minimal annual row so report_type resolves.
        sdb.upsert_financials_annual(
            conn, isin, screener.SOURCE, "consolidated",
            [{"fiscal_year": 2024, "sales_cr": 1000.0, "net_profit_cr": 200.0,
              "opm_pct": 0.20, "equity_capital_cr": 10.0, "reserves_cr": 500.0,
              "borrowings_cr": 50.0}],
        )
        # 8 quarters ascending period_end.
        q_rows = []
        for i, (s, p, om) in enumerate(
            _qrows(8, sales_growth=0.05, profit_growth=0.06, opm=0.18, opm_trend=0.04)
        ):
            q_rows.append({
                "period_end": f"202{2 + i // 4}-{'03-31' if i % 4 == 0 else '06-30' if i % 4 == 1 else '09-30' if i % 4 == 2 else '12-31'}",
                "sales_cr": s, "net_profit_cr": p, "opm_pct": om,
            })
        sdb.upsert_financials_quarterly(
            conn, isin, screener.SOURCE, "consolidated", q_rows,
        )
    with sqlite3.connect(db_path) as conn:
        feats = vf.load_fundamental_features(conn, isin)
    assert feats is not None
    assert feats.quarters_of_data == 8
    assert feats.ttm_sales_growth_yoy is not None and feats.ttm_sales_growth_yoy > 0
    assert feats.opm_trend is not None and feats.opm_trend > 0
