"""Vectorized Phase 0 signal engine for backtesting.

Stage 1: vectorized rolling metrics + raw score signal (fast).
Stage 2: per-stock sequential pass applying strategy.decide() to enforce the
state machine (D-BT14) and hard-gate (D-BT13). Required because the state
machine is path-dependent (today's decision depends on yesterday's).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from portfolio_analyzer import config as cfg
from portfolio_analyzer import strategy
from portfolio_analyzer.stock_analysis import StockMetrics


@dataclass
class MarketDaily:
    trend: pd.Series           # str per date: UPTREND|SIDEWAYS|DOWNTREND
    breadth_pct: pd.Series     # float per date, NaN if undefined
    nifty500_trend: pd.Series
    nifty50_trend: pd.Series


def _classify_trend_series(close: pd.Series) -> pd.Series:
    ma50 = close.rolling(cfg.MA_SHORT).mean()
    ma200 = close.rolling(cfg.MA_LONG).mean()
    up = (close > ma50) & (ma50 > ma200)
    down = (close < ma50) & (ma50 < ma200)
    out = pd.Series("SIDEWAYS", index=close.index, dtype=object)
    out[up] = "UPTREND"
    out[down & ~up] = "DOWNTREND"
    out[ma50.isna() | ma200.isna()] = "SIDEWAYS"
    return out


_TREND_SCORE = {"UPTREND": 1, "SIDEWAYS": 0, "DOWNTREND": -1}


def _blend_trend_series(n500: pd.Series, n50: pd.Series) -> pd.Series:
    s500 = n500.map(_TREND_SCORE).astype(float) * cfg.NIFTY500_WEIGHT
    s50 = n50.map(_TREND_SCORE).astype(float) * cfg.NIFTY50_WEIGHT
    blended = s500 + s50
    out = pd.Series("SIDEWAYS", index=blended.index, dtype=object)
    out[blended >= cfg.BLEND_UP_THRESHOLD] = "UPTREND"
    out[blended <= cfg.BLEND_DOWN_THRESHOLD] = "DOWNTREND"
    return out


def _breadth_pct_series(universe_close: pd.DataFrame) -> pd.Series:
    ma50 = universe_close.rolling(cfg.MA_SHORT).mean()
    above = (universe_close > ma50) & ma50.notna()
    valid = ma50.notna() & universe_close.notna()
    num = above.sum(axis=1)
    den = valid.sum(axis=1)
    pct = num / den.where(den > 0)
    return pct


def _apply_breadth_override(blended: pd.Series, breadth: pd.Series) -> pd.Series:
    out = blended.copy()
    weak = breadth < cfg.BREADTH_WEAK
    strong = breadth >= cfg.BREADTH_STRONG
    out[(blended == "UPTREND") & weak] = "SIDEWAYS"
    out[(blended == "DOWNTREND") & strong] = "SIDEWAYS"
    return out


def compute_market_daily(
    nifty500_close: pd.Series,
    nifty50_close: pd.Series,
    universe_close: pd.DataFrame,
) -> MarketDaily:
    n500_trend = _classify_trend_series(nifty500_close)
    n50_trend = _classify_trend_series(nifty50_close).reindex(n500_trend.index, method="ffill")
    blended = _blend_trend_series(n500_trend, n50_trend)
    breadth = _breadth_pct_series(universe_close).reindex(n500_trend.index, method="ffill")
    final = _apply_breadth_override(blended, breadth)
    return MarketDaily(trend=final, breadth_pct=breadth,
                       nifty500_trend=n500_trend, nifty50_trend=n50_trend)


def _raw_signal_frame(
    close: pd.DataFrame, ma50: pd.DataFrame, ma200: pd.DataFrame,
    drawdown: pd.DataFrame, rs: pd.DataFrame, ret50: pd.DataFrame,
) -> pd.DataFrame:
    """Score-based signal per (date, symbol) using D-BT12 thresholds."""
    strong = ((close > ma50) & (ma50 > ma200)).fillna(False)
    outperf = (rs > 0).fillna(False)
    near_high = (drawdown > cfg.NEAR_HIGH_DRAWDOWN).fillna(False)
    large_dd = (drawdown < cfg.LARGE_DRAWDOWN).fillna(False)
    score = (
        2 * strong.astype(int) + 2 * outperf.astype(int)
        + 1 * near_high.astype(int) - 2 * large_dd.astype(int)
    )
    out = pd.DataFrame(strategy.STATE_EXIT, index=score.index, columns=score.columns, dtype=object)
    out[score >= cfg.REDUCE_SCORE_MIN] = strategy.STATE_REDUCE
    out[score >= cfg.HOLD_SCORE_MIN] = strategy.STATE_HOLD
    insufficient = ma200.isna() | ret50.isna()
    out[insufficient] = strategy.STATE_HOLD
    return out


def compute_decisions(
    holding_close: pd.DataFrame,
    nifty500_close: pd.Series,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Returns (decisions, raw_signals, rs), all (date x symbol) frames.

    `decisions` is the state-machine output; `raw_signals` is the pre-hysteresis
    score-based call (diagnostic, D-BT18); `rs` is the per-stock relative
    strength (ret50 - market_ret50), used downstream for ranked re-arm (D-BT21).
    """
    idx = holding_close.index
    ma50 = holding_close.rolling(cfg.MA_SHORT).mean()
    ma200 = holding_close.rolling(cfg.MA_LONG).mean()
    high_52w = holding_close.rolling(cfg.HIGH_52W_WINDOW).max()
    ret50 = holding_close / holding_close.shift(cfg.RETURN_WINDOW) - 1.0
    drawdown = holding_close / high_52w - 1.0
    market_ret50 = (nifty500_close / nifty500_close.shift(cfg.RETURN_WINDOW) - 1.0).reindex(idx, method="ffill")
    rs = ret50.sub(market_ret50, axis=0)
    raw = _raw_signal_frame(holding_close, ma50, ma200, drawdown, rs, ret50)

    close_np = holding_close.to_numpy(dtype=float, na_value=np.nan)
    ma50_np = ma50.to_numpy(dtype=float, na_value=np.nan)
    ma200_np = ma200.to_numpy(dtype=float, na_value=np.nan)
    high_np = high_52w.to_numpy(dtype=float, na_value=np.nan)
    ret_np = ret50.to_numpy(dtype=float, na_value=np.nan)
    dd_np = drawdown.to_numpy(dtype=float, na_value=np.nan)
    rs_np = rs.to_numpy(dtype=float, na_value=np.nan)
    raw_np = raw.to_numpy(dtype=object)
    out_np = np.empty_like(raw_np, dtype=object)

    symbols = list(holding_close.columns)
    for j, sym in enumerate(symbols):
        prev = strategy.STATE_HOLD
        for i in range(len(idx)):
            price = close_np[i, j]; m50 = ma50_np[i, j]; m200 = ma200_np[i, j]
            trend = "STRONG" if (
                not math.isnan(price) and not math.isnan(m50) and not math.isnan(m200)
                and price > m50 > m200
            ) else "WEAK"
            metrics = StockMetrics(
                symbol=sym, price=price, ma_50=m50, ma_200=m200,
                high_52w=high_np[i, j], return_50d=ret_np[i, j],
                relative_strength=rs_np[i, j], drawdown_from_high=dd_np[i, j],
                trend=trend,
                insufficient_history=math.isnan(m200) or math.isnan(ret_np[i, j]),
            )
            result = strategy.decide(prev, metrics, raw_np[i, j])
            out_np[i, j] = result.decision
            prev = result.decision

    decisions = pd.DataFrame(out_np, index=idx, columns=symbols, dtype=object)
    return decisions, raw, rs


def compute_refill_eligibility(holding_close: pd.DataFrame) -> pd.DataFrame:
    """Boolean (date x symbol) frame where refill entries are permitted (D-BT22).

    Requires the negation of the EXIT hard-gate (price >= 200DMA AND drawdown
    >= EXIT_GATE_DRAWDOWN) plus price > 50DMA. This mirrors `strategy.hard_
    gate_forces_exit()` so refill cannot pick names the decision matrix is
    still emitting EXIT for. RS > 0 is enforced at selection time.
    """
    ma50 = holding_close.rolling(cfg.MA_SHORT).mean()
    ma200 = holding_close.rolling(cfg.MA_LONG).mean()
    high_52w = holding_close.rolling(cfg.HIGH_52W_WINDOW).max()
    drawdown = holding_close / high_52w - 1.0
    above_mas = (holding_close > ma50) & (holding_close > ma200)
    not_hard_gated = drawdown >= cfg.EXIT_GATE_DRAWDOWN
    return (above_mas & not_hard_gated).fillna(False)
