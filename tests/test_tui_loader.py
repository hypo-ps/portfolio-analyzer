from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from portfolio_analyzer.tui import loader


def _write_artifacts(tmp: Path) -> Path:
    json_path = tmp / "bt.json"
    stem = "bt"
    report = {
        "window": {"start": "2021-01-04", "end": "2022-01-04"},
        "initial_capital": 1_000_000.0,
        "universe": {"initial_portfolio": "NIFTY50", "breadth": "NIFTY500"},
        "performance": {"cagr": 0.1, "max_drawdown": -0.08, "sharpe": 1.2,
                        "volatility_annual": 0.1, "total_return": 0.1, "days": 250},
        "benchmark_nifty500": {"cagr": 0.05, "max_drawdown": -0.1, "sharpe": 0.4,
                               "volatility_annual": 0.12, "total_return": 0.05, "days": 250},
        "avg_exposure": 0.5, "ending_equity": 1_100_000.0,
        "num_fills": 4, "num_holdings_at_end": 2,
        "num_blocked_by_exposure_floor": 0,
        "market_regime_days": {"UPTREND": 200, "SIDEWAYS": 40, "DOWNTREND": 10},
        "exits": {"num_exits": 1, "num_reduces": 0},
        "rearms": {"num_rearms": 0}, "refills": {"num_refills": 0},
    }
    json_path.write_text(json.dumps(report))

    idx = pd.bdate_range("2021-01-04", periods=260)
    eq = pd.DataFrame({
        "equity": [1_000_000.0 + 400.0 * i for i in range(260)],
        "benchmark": [1_000_000.0 + 200.0 * i for i in range(260)],
        "exposure": [0.5] * 260,
    }, index=idx)
    eq.index.name = "date"
    eq.to_csv(tmp / f"{stem}_equity.csv")

    fills = pd.DataFrame([
        {"date": "2021-01-04", "symbol": "A", "side": "BUY", "shares": 100.0, "price": 10.0, "reason": "INIT"},
        {"date": "2021-06-01", "symbol": "A", "side": "SELL", "shares": 40.0, "price": 12.0, "reason": "REDUCE"},
        {"date": "2021-03-15", "symbol": "B", "side": "BUY", "shares": 50.0, "price": 20.0, "reason": "REFILL"},
        {"date": "2021-09-01", "symbol": "B", "side": "SELL", "shares": 50.0, "price": 22.0, "reason": "EXIT"},
    ])
    fills.to_csv(tmp / f"{stem}_fills.csv", index=False)

    pd.DataFrame([{"date": "2021-03-15", "symbol": "B", "rupees": 1000.0}]) \
        .to_csv(tmp / f"{stem}_refills.csv", index=False)
    return json_path


def test_load_reads_report_and_csvs(tmp_path):
    p = _write_artifacts(tmp_path)
    art = loader.load(p)
    assert art.report["initial_capital"] == 1_000_000.0
    assert len(art.equity) == 260
    assert list(art.equity.columns) == ["equity", "benchmark", "exposure"]
    assert len(art.fills) == 4
    assert len(art.refills) == 1
    assert art.rearms.empty and art.blocked.empty and art.decisions.empty


def test_per_year_computes_alpha_and_exposure(tmp_path):
    p = _write_artifacts(tmp_path)
    art = loader.load(p)
    df = loader.per_year(art.equity)
    assert not df.empty
    assert {"year", "port_ret", "bench_ret", "alpha", "sharpe",
            "vol_ann", "maxdd", "avg_expo"}.issubset(df.columns)
    y2021 = df[df["year"] == 2021].iloc[0]
    assert y2021["port_ret"] > y2021["bench_ret"]
    assert abs(y2021["alpha"] - (y2021["port_ret"] - y2021["bench_ret"])) < 1e-12
    assert 0.49 < y2021["avg_expo"] < 0.51


def test_per_quarter_has_q1_through_q4(tmp_path):
    p = _write_artifacts(tmp_path)
    art = loader.load(p)
    df = loader.per_quarter(art.equity)
    quarters = set(df["quarter"])
    assert any(q.endswith("Q1") for q in quarters)
    assert any(q.endswith("Q4") for q in quarters)


def test_holdings_reconstructs_shares_and_avg_cost(tmp_path):
    p = _write_artifacts(tmp_path)
    art = loader.load(p)
    h = loader.holdings_from_fills(art.fills)
    # A: 100 BUY @10 then 40 SELL -> 60 shares left; avg_cost from BUY leg only = 10.
    # B: 50 BUY @20 then 50 SELL -> closed (excluded).
    assert list(h["symbol"]) == ["A"]
    row = h.iloc[0]
    assert abs(row["shares"] - 60.0) < 1e-9
    assert abs(row["avg_cost"] - 10.0) < 1e-9
    assert abs(row["invested"] - 1000.0) < 1e-9
    assert abs(row["realized"] - 480.0) < 1e-9  # 40 * 12


def test_holdings_empty_when_no_fills():
    empty = loader.holdings_from_fills(pd.DataFrame())
    assert empty.empty
    assert list(empty.columns) == ["symbol", "shares", "avg_cost", "invested", "realized"]


def test_window_label_renders_year_span():
    r = {"window": {"start": "2021-04-22", "end": "2026-04-22"}}
    assert loader.window_label(r) == "2021-2026"
    assert loader.window_label({}) == "?"


def test_normalize_equity_rebases_to_one_and_uses_offset_index(tmp_path):
    p = _write_artifacts(tmp_path)
    art = loader.load(p)
    norm = loader.normalize_equity(art)
    assert len(norm) == 260
    assert list(norm.index)[:3] == [0, 1, 2]
    assert abs(norm.iloc[0] - 1.0) < 1e-12
    # equity series: 1_000_000 -> 1_000_000 + 400*259; rebased ratio matches.
    expected_last = (1_000_000.0 + 400.0 * 259) / 1_000_000.0
    assert abs(norm.iloc[-1] - expected_last) < 1e-9


def test_drawdown_series_is_non_positive_and_offset_indexed(tmp_path):
    p = _write_artifacts(tmp_path)
    art = loader.load(p)
    dd = loader.drawdown_series(art)
    assert len(dd) == 260
    assert list(dd.index)[:3] == [0, 1, 2]
    assert (dd <= 1e-12).all()
    # Monotonically rising equity fixture -> drawdown is flat 0.
    assert abs(dd.min()) < 1e-12


def test_compare_metrics_returns_row_per_run(tmp_path):
    d1 = tmp_path / "a"; d1.mkdir()
    d2 = tmp_path / "b"; d2.mkdir()
    p1 = _write_artifacts(d1)
    p2 = _write_artifacts(d2)
    arts = [loader.load(p1), loader.load(p2)]
    df = loader.compare_metrics(arts)
    assert len(df) == 2
    assert {"label", "cagr", "bench_cagr", "alpha", "sharpe", "maxdd",
            "avg_expo", "end_eq", "fills", "refills"}.issubset(df.columns)
    # Fixture alpha = 0.10 - 0.05 = 0.05
    assert abs(df["alpha"].iloc[0] - 0.05) < 1e-12
