from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

from portfolio_analyzer import cli
from portfolio_analyzer.scanner import db as sdb
from portfolio_analyzer.scanner.bhavcopy import BhavRow
from portfolio_analyzer.scanner.vcp import features as vf
from portfolio_analyzer.scanner.vcp.fundamentals import FundamentalFeatures
from portfolio_analyzer.scanner.vcp.scan import scan_date
from portfolio_analyzer.scanner.vcp.scorer import score_candidate


# ---------- synthetic fixtures ----------

def _tight_vcp_series(n: int = 400, seed: int = 11) -> tuple[np.ndarray, ...]:
    """Uptrend + textbook 3-swing contraction, close pinned near pivot."""
    rng = np.random.default_rng(seed)
    base = np.linspace(100.0, 200.0, n)
    close = base + rng.normal(0, 2.0, n)
    phase = np.linspace(0, 6 * np.pi, 60)
    amps = np.interp(np.arange(60), [0, 20, 40, 59], [6.0, 3.0, 1.5, 0.8])
    close[-60:] = base[-60:] + amps * np.sin(phase) + 0.3
    close[-1] = close[-10:].max() * 0.995
    high = close + np.abs(rng.normal(0, 0.5, n))
    low = close - np.abs(rng.normal(0, 0.5, n))
    vol = rng.integers(80_000, 150_000, n).astype(float)
    vol[-30:] *= 0.5
    return close, high, low, vol


def _strong_fundamentals() -> FundamentalFeatures:
    return FundamentalFeatures(
        isin="INE_TEST", sector="IT", industry=None, market_cap_cr=5000.0,
        stock_pe=25.0, roe_latest=0.22, roce_latest=0.25, promoter_holding=0.5,
        latest_fy=2025, latest_sales_cr=1000.0, latest_net_profit_cr=200.0,
        latest_opm_pct=22.0, revenue_growth_yoy=0.18, revenue_cagr_3y=0.20,
        revenue_cagr_5y=0.18, profit_cagr_3y=0.25, debt_to_equity=0.2,
        years_of_data=10,
    )


# ---------- features unit tests ----------

def test_ema_seed_and_convergence():
    arr = np.full(100, 50.0)
    out = vf._ema(arr, 50)
    assert out is not None
    assert out[49] == pytest.approx(50.0)
    assert out[-1] == pytest.approx(50.0)


def test_ema_none_when_insufficient_history():
    assert vf._ema(np.arange(10, dtype=float), 50) is None


def test_wilder_smoothing_matches_mean_for_constant_input():
    arr = np.full(30, 3.0)
    assert vf._wilder(arr, 14) == pytest.approx(3.0)


def test_true_range_simple_case():
    high = np.array([10.0, 12.0])
    low = np.array([9.0, 11.0])
    close = np.array([9.5, 11.5])
    tr = vf._true_range(high, low, close)
    assert tr.shape == (2,)
    assert tr[0] == pytest.approx(1.0)           # first bar uses high-low
    assert tr[1] == pytest.approx(12.0 - 9.5)    # gap up across prev close


def test_find_swings_detects_peak_and_trough():
    highs = np.array([1, 2, 3, 4, 5, 4, 3, 2, 1, 2, 3], dtype=float)
    lows = np.array([1, 0.5, 0.2, 0.1, 0.2, 0.5, 1, 2, 3, 2, 1], dtype=float)
    sh, sl = vf._find_swings(highs, lows, n=2)
    assert (4, 5.0) in sh
    assert (3, 0.1) in sl


def test_compute_technical_features_rejects_short_history():
    arr = np.full(100, 100.0)
    assert vf.compute_technical_features(arr, arr, arr, arr, arr) is None


def test_compute_technical_features_on_vcp_series():
    close, high, low, vol = _tight_vcp_series()
    t = vf.compute_technical_features(close, high, low, close, vol)
    assert t is not None
    assert t.ema50 > t.ema200                    # uptrend stack
    assert t.close > t.ema50
    assert t.return_1y > 0.20
    assert abs(t.distance_to_pivot) < 0.01
    assert t.range_20d < 0.10
    # Swing structure: 3 rising lows and 3 rising highs
    assert len(t.swing_highs) == 3
    assert len(t.swing_lows) == 3
    lows_ = [p for _, p in t.swing_lows]
    assert lows_[2] > lows_[1] > lows_[0]


# ---------- scorer pipeline tests ----------

def test_score_reject_downtrend():
    rng = np.random.default_rng(3)
    n = 400
    close = np.linspace(200.0, 100.0, n) + rng.normal(0, 2, n)
    high = close + 0.5
    low = close - 0.5
    vol = np.full(n, 100_000.0)
    t = vf.compute_technical_features(close, high, low, close, vol)
    r = score_candidate(t, None)
    assert r.decision == "REJECT"
    assert r.stage == "STAGE1_FAIL"


def test_score_stage2_fail_on_missing_fundamentals():
    close, high, low, vol = _tight_vcp_series()
    t = vf.compute_technical_features(close, high, low, close, vol)
    r = score_candidate(t, None)
    assert r.decision == "REJECT"
    assert r.stage == "STAGE2_FAIL"


def test_score_stage2_fail_on_high_debt():
    close, high, low, vol = _tight_vcp_series()
    t = vf.compute_technical_features(close, high, low, close, vol)
    bad = _strong_fundamentals().__class__(
        **{**_strong_fundamentals().__dict__, "debt_to_equity": 3.0},
    )
    r = score_candidate(t, bad)
    assert r.decision == "REJECT"
    assert r.stage == "STAGE2_FAIL"
    assert any("debt_to_equity" in reason for reason in r.reasons)


def test_score_buy_alert_on_textbook_vcp():
    close, high, low, vol = _tight_vcp_series()
    t = vf.compute_technical_features(close, high, low, close, vol)
    r = score_candidate(t, _strong_fundamentals())
    assert r.decision in {"BUY_ALERT", "WATCHLIST"}
    assert r.vcp_score is not None and r.vcp_score > 0.5
    assert r.readiness_score is not None and r.readiness_score > 0.8


def test_score_watchlist_for_noisy_uptrend():
    rng = np.random.default_rng(7)
    n = 400
    close = np.linspace(100.0, 200.0, n) + rng.normal(0, 3.0, n)
    t = vf.compute_technical_features(
        close, close + 0.5, close - 0.5, close, np.full(n, 1_000_000.0),
    )
    r = score_candidate(t, _strong_fundamentals())
    assert r.decision in {"WATCHLIST", "REJECT"}
    if r.decision == "WATCHLIST":
        assert r.stage in {"BUILDING", "CONTRACTING"}


# ---------- orchestrator + CLI ----------

def _seed_scan_db(db_path: Path, symbol: str = "VCP") -> dt.date:
    """Seed a DB with one symbol carrying a tight-VCP adjusted price series."""
    isin = "INE_VCP_0001"
    close, high, low, vol = _tight_vcp_series()
    start = dt.date(2025, 1, 1)
    rows: list[BhavRow] = []
    td = start
    for i, c in enumerate(close):
        while td.weekday() >= 5:
            td += dt.timedelta(days=1)
        rows.append(BhavRow(
            trade_date=td, isin=isin, symbol=symbol, name=f"{symbol} Ltd",
            series="EQ", open=float(c), high=float(high[i]), low=float(low[i]),
            close=float(c), prev_close=float(c), volume=int(vol[i]),
            turnover=float(c) * float(vol[i]), trades=100,
        ))
        td += dt.timedelta(days=1)

    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [rows[0]])
        sdb.upsert_market_data(conn, rows)
    return rows[-1].trade_date


def test_scan_date_detects_vcp_candidate(tmp_path: Path):
    db_path = tmp_path / "s.db"
    last = _seed_scan_db(db_path)
    result = scan_date(last, db_path=db_path, store_rejects=True)
    assert result.universe == 1
    assert result.scored == 1
    assert result.stored == 1
    with sdb.open_db(db_path) as conn:
        row = conn.execute(
            "SELECT symbol, decision, stage, final_score FROM vcp_candidates"
        ).fetchone()
    assert row[0] == "VCP"
    # Fundamentals missing on synthetic symbol → STAGE2_FAIL is expected.
    assert row[1] == "REJECT"
    assert row[2] == "STAGE2_FAIL"


def test_cli_vcp_scan_smoke(tmp_path: Path):
    db_path = tmp_path / "s.db"
    last = _seed_scan_db(db_path)
    runner = CliRunner()
    res = runner.invoke(cli.main, [
        "scanner", "vcp-scan",
        "--date", last.isoformat(),
        "--store-rejects",
        "--db", str(db_path),
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["trade_date"] == last.isoformat()
    assert payload["universe"] == 1
    assert payload["stored"] == 1


def test_cli_status_includes_vcp_section(tmp_path: Path):
    db_path = tmp_path / "s.db"
    last = _seed_scan_db(db_path)
    scan_date(last, db_path=db_path, store_rejects=True)
    runner = CliRunner()
    res = runner.invoke(cli.main, ["scanner", "status", "--db", str(db_path)])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["vcp"]["total"] == 1
    assert payload["vcp"]["latest_scan_date"] == last.isoformat()
