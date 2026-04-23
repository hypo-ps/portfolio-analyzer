from __future__ import annotations

import math

import pandas as pd


def sma(close: pd.Series, window: int) -> float:
    if len(close) < window:
        return math.nan
    return float(close.iloc[-window:].mean())


def return_over(close: pd.Series, n: int) -> float:
    if len(close) <= n:
        return math.nan
    prev = float(close.iloc[-n - 1])
    last = float(close.iloc[-1])
    if prev == 0:
        return math.nan
    return last / prev - 1.0


def rolling_high(close: pd.Series, window: int) -> float:
    if len(close) == 0:
        return math.nan
    return float(close.iloc[-window:].max())


def pct_from_high(price: float, high: float) -> float:
    if high == 0 or math.isnan(high):
        return math.nan
    return price / high - 1.0
