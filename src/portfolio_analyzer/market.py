from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from portfolio_analyzer import config as cfg
from portfolio_analyzer.util import ohlc

_TREND_SCORE = {"UPTREND": 1, "SIDEWAYS": 0, "DOWNTREND": -1}


def classify_index_trend(close: pd.Series) -> str:
    if len(close) == 0:
        return "SIDEWAYS"
    price = float(close.iloc[-1])
    ma50 = ohlc.sma(close, cfg.MA_SHORT)
    ma200 = ohlc.sma(close, cfg.MA_LONG)
    if math.isnan(ma50) or math.isnan(ma200):
        return "SIDEWAYS"
    if price > ma50 > ma200:
        return "UPTREND"
    if price < ma50 < ma200:
        return "DOWNTREND"
    return "SIDEWAYS"


def blend_trend(nifty500_trend: str, nifty50_trend: str) -> str:
    score = (
        _TREND_SCORE[nifty500_trend] * cfg.NIFTY500_WEIGHT
        + _TREND_SCORE[nifty50_trend] * cfg.NIFTY50_WEIGHT
    )
    if score >= cfg.BLEND_UP_THRESHOLD:
        return "UPTREND"
    if score <= cfg.BLEND_DOWN_THRESHOLD:
        return "DOWNTREND"
    return "SIDEWAYS"


def breadth_regime(pct: float) -> str:
    if math.isnan(pct):
        return "unknown"
    if pct >= cfg.BREADTH_STRONG:
        return "strong"
    if pct < cfg.BREADTH_WEAK:
        return "weak"
    return "mixed"


def apply_breadth_override(blended: str, breadth_pct: float) -> str:
    if math.isnan(breadth_pct):
        return blended
    if blended == "UPTREND" and breadth_pct < cfg.BREADTH_WEAK:
        return "SIDEWAYS"
    if blended == "DOWNTREND" and breadth_pct >= cfg.BREADTH_STRONG:
        return "SIDEWAYS"
    return blended


@dataclass
class MarketState:
    trend: str
    return_50d: float
    breadth_pct: float
    breadth_regime: str
    nifty500_trend: str
    nifty50_trend: str


def compute_market_state(
    nifty500_close: pd.Series,
    nifty50_close: pd.Series,
    breadth_pct: float,
) -> MarketState:
    n500_trend = classify_index_trend(nifty500_close)
    n50_trend = classify_index_trend(nifty50_close)
    blended = blend_trend(n500_trend, n50_trend)
    final = apply_breadth_override(blended, breadth_pct)
    return MarketState(
        trend=final,
        return_50d=ohlc.return_over(nifty500_close, cfg.RETURN_WINDOW),
        breadth_pct=breadth_pct,
        breadth_regime=breadth_regime(breadth_pct),
        nifty500_trend=n500_trend,
        nifty50_trend=n50_trend,
    )


def compute_breadth_pct(close_by_symbol: dict[str, pd.Series]) -> float:
    """% of supplied symbols whose last close > their 50DMA. NaNs excluded from denominator."""
    total = 0
    above = 0
    for _symbol, series in close_by_symbol.items():
        ma50 = ohlc.sma(series, cfg.MA_SHORT)
        if math.isnan(ma50) or len(series) == 0:
            continue
        total += 1
        if float(series.iloc[-1]) > ma50:
            above += 1
    if total == 0:
        return math.nan
    return above / total
