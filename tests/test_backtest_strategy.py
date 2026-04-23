from __future__ import annotations

import numpy as np
import pandas as pd

from portfolio_analyzer.backtest import phase0_strategy
from portfolio_analyzer.scoring import score_stock
from portfolio_analyzer.stock_analysis import compute_metrics


def _bdates(n: int, start: str = "2022-01-03") -> pd.DatetimeIndex:
    return pd.date_range(start=start, periods=n, freq="B")


def test_vectorized_decisions_match_scalar_scorer():
    n = 300
    dates = _bdates(n)
    rng = np.random.default_rng(1)
    # Stock A: steady uptrend outperforming the index
    a = pd.Series(100.0 * (1 + np.linspace(0, 0.5, n)) + rng.normal(0, 0.3, n), index=dates)
    # Stock B: flat then big drawdown in last 30 bars
    b_vals = np.concatenate([np.linspace(100, 120, n - 30), np.linspace(120, 80, 30)])
    b = pd.Series(b_vals + rng.normal(0, 0.2, n), index=dates)
    # Index: mild uptrend
    idx = pd.Series(1000.0 * (1 + np.linspace(0, 0.2, n)), index=dates)

    holdings = pd.concat({"A": a, "B": b}, axis=1)
    _, raw, _ = phase0_strategy.compute_decisions(holdings, idx)

    # Raw signals (pre-hysteresis, D-BT18) must match the scalar scorer.
    from portfolio_analyzer import config as cfg
    for t in [dates[250], dates[270], dates[-1]]:
        a_slice = a.loc[:t]
        b_slice = b.loc[:t]
        idx_slice = idx.loc[:t]
        mkt_ret50 = (idx_slice.iloc[-1] / idx_slice.iloc[-1 - cfg.RETURN_WINDOW] - 1.0)
        for sym, s in (("A", a_slice), ("B", b_slice)):
            metrics = compute_metrics(sym, s, float(mkt_ret50))
            scored = score_stock(metrics)
            assert raw.loc[t, sym] == scored.decision, (
                f"parity failure on {t.date()} {sym}: vec={raw.loc[t, sym]} scalar={scored.decision}"
            )


def test_decisions_default_to_hold_during_warmup():
    n = 60  # less than 200-day MA window
    dates = _bdates(n)
    holdings = pd.DataFrame({"X": np.linspace(100, 110, n)}, index=dates)
    idx = pd.Series(np.linspace(1000, 1050, n), index=dates)
    dec, _, _ = phase0_strategy.compute_decisions(holdings, idx)
    assert (dec["X"] == "HOLD").all()


def test_breadth_pct_series_vectorization():
    n = 100
    dates = _bdates(n)
    # Three stocks, all flat then one spikes above its 50DMA late
    flat = pd.Series([100.0] * n, index=dates)
    spike = pd.Series([100.0] * 80 + [200.0] * 20, index=dates)
    df = pd.DataFrame({"A": flat, "B": flat, "C": spike})
    breadth = phase0_strategy._breadth_pct_series(df)
    assert breadth.iloc[-1] == 1 / 3  # only C is above its own 50DMA at end
    # During the flat period (post-warmup), breadth is 0
    assert breadth.iloc[70] == 0.0


def test_classify_trend_series_matches_scalar():
    from portfolio_analyzer.market import classify_index_trend

    n = 260
    dates = _bdates(n)
    close = pd.Series(100.0 * (1 + np.linspace(0, 0.3, n)), index=dates)
    series = phase0_strategy._classify_trend_series(close)
    # Scalar classifier on the last bar should match
    assert series.iloc[-1] == classify_index_trend(close)
    # Early bars (before ma200) should be SIDEWAYS
    assert series.iloc[100] == "SIDEWAYS"



def test_refill_eligibility_fast_path_admits_rebound_below_200dma():
    # D-BT27: build a stock that spends 70 bars high, tanks to a deep drawdown
    # (below 200DMA), then rebounds >=10% off the local low while still below
    # 200DMA. Primary gate must reject; secondary must admit.
    from portfolio_analyzer import config as cfg

    n = 260
    dates = _bdates(n)
    # 200 bars at ~100 to warm up the 200DMA, 30-bar decline to 60 (dd ~-40%),
    # then 20-bar recovery to 75 (rebound ~+25% off low of 60).
    up = np.linspace(100.0, 105.0, 200)
    down = np.linspace(105.0, 60.0, 40)
    rebound = np.linspace(60.0, 75.0, 20)
    close = np.concatenate([up, down, rebound])
    df = pd.DataFrame({"X": close}, index=dates)
    elig = phase0_strategy.compute_refill_eligibility(df)
    last = elig.iloc[-1]["X"]
    ma200 = df["X"].rolling(cfg.MA_LONG).mean().iloc[-1]
    assert df["X"].iloc[-1] < ma200  # confirm primary 200DMA gate fails
    assert bool(last) is True  # secondary fast path admits
