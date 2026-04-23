from __future__ import annotations

import math

import numpy as np
import pandas as pd

from portfolio_analyzer.backtest import metrics as bt_metrics


def _bdates(n: int, start: str = "2024-01-02") -> pd.DatetimeIndex:
    return pd.date_range(start=start, periods=n, freq="B")


def test_perf_stats_flat_equity_has_zero_cagr_and_dd():
    eq = pd.Series([100.0] * 252, index=_bdates(252))
    p = bt_metrics.perf_stats(eq)
    assert p.days == 252
    assert abs(p.total_return) < 1e-12
    assert abs(p.cagr) < 1e-9
    assert abs(p.max_drawdown) < 1e-12
    assert abs(p.sharpe) < 1e-12


def test_perf_stats_doubles_over_one_year():
    eq_values = np.linspace(100.0, 200.0, 252)
    eq = pd.Series(eq_values, index=_bdates(252))
    p = bt_metrics.perf_stats(eq)
    assert abs(p.total_return - 1.0) < 1e-9
    # 252 days ~= 1 year -> CAGR ~ 100%
    assert 0.98 < p.cagr < 1.02
    assert p.max_drawdown == 0.0  # monotonic up


def test_max_drawdown_detected():
    eq = pd.Series([100, 120, 90, 110, 80], index=_bdates(5), dtype="float64")
    p = bt_metrics.perf_stats(eq)
    # Peak at 120, trough at 80 -> -33.33%
    assert abs(p.max_drawdown - (80 / 120 - 1.0)) < 1e-9


def test_exit_diagnostics_hit_rate_and_avg_return():
    # Craft a stock that falls 10% over 21 days after an EXIT signal.
    dates = _bdates(30)
    opens = pd.DataFrame({"X": [100.0] * 30}, index=dates)
    closes = pd.DataFrame({"X": [100.0] * 30}, index=dates)
    # EXIT at dates[0]; entry at dates[1] open (100), measure at dates[22] close.
    closes.loc[dates[22], "X"] = 90.0
    decisions = pd.DataFrame([
        {"date": dates[0], "symbol": "X", "decision": "EXIT"},
    ])
    diag = bt_metrics.exit_diagnostics(decisions, opens, closes, lookahead_days=21)
    assert diag.num_exits == 1
    assert abs(diag.avg_forward_return_21d - (-0.10)) < 1e-9
    assert diag.exit_quality_rate == 1.0


def test_exit_diagnostics_ignores_signals_too_close_to_end():
    dates = _bdates(10)
    opens = pd.DataFrame({"X": [100.0] * 10}, index=dates)
    closes = pd.DataFrame({"X": [100.0] * 10}, index=dates)
    decisions = pd.DataFrame([
        {"date": dates[0], "symbol": "X", "decision": "EXIT"},
    ])
    diag = bt_metrics.exit_diagnostics(decisions, opens, closes, lookahead_days=21)
    # No valid 21-day window -> no fwd returns, avg=0
    assert diag.num_exits == 1
    assert diag.avg_forward_return_21d == 0.0


def test_benchmark_equity_starts_at_initial_capital():
    dates = _bdates(5)
    px = pd.Series([100.0, 110.0, 120.0, 130.0, 140.0], index=dates)
    bench = bt_metrics.benchmark_equity(px, initial_capital=10_000.0,
                                        start_date=dates[0], end_date=dates[-1])
    assert not bench.empty
    assert abs(bench.iloc[0] - 10_000.0) < 1e-6
    assert abs(bench.iloc[-1] - 14_000.0) < 1e-6


def test_perf_stats_empty_series_returns_zeros():
    p = bt_metrics.perf_stats(pd.Series([], dtype=float))
    assert p.cagr == 0.0
    assert p.max_drawdown == 0.0
