from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from portfolio_analyzer import config as cfg
from portfolio_analyzer.util import ohlc


@dataclass
class StockMetrics:
    symbol: str
    price: float
    ma_50: float
    ma_200: float
    high_52w: float
    return_50d: float
    relative_strength: float
    drawdown_from_high: float
    trend: str  # "STRONG" | "WEAK"
    insufficient_history: bool


def classify_trend(price: float, ma50: float, ma200: float) -> str:
    if math.isnan(ma50) or math.isnan(ma200):
        return "WEAK"
    if price > ma50 > ma200:
        return "STRONG"
    return "WEAK"


def compute_metrics(symbol: str, close: pd.Series, market_return_50d: float) -> StockMetrics:
    price = float(close.iloc[-1]) if len(close) else math.nan
    ma50 = ohlc.sma(close, cfg.MA_SHORT)
    ma200 = ohlc.sma(close, cfg.MA_LONG)
    high52 = ohlc.rolling_high(close, cfg.HIGH_52W_WINDOW)
    ret50 = ohlc.return_over(close, cfg.RETURN_WINDOW)
    drawdown = ohlc.pct_from_high(price, high52)
    rs = math.nan if math.isnan(ret50) or math.isnan(market_return_50d) else ret50 - market_return_50d
    insufficient = math.isnan(ma200) or math.isnan(ret50)
    return StockMetrics(
        symbol=symbol,
        price=price,
        ma_50=ma50,
        ma_200=ma200,
        high_52w=high52,
        return_50d=ret50,
        relative_strength=rs,
        drawdown_from_high=drawdown,
        trend=classify_trend(price, ma50, ma200),
        insufficient_history=insufficient,
    )
