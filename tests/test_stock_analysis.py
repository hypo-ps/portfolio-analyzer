from __future__ import annotations

import math

import pandas as pd
import pytest

from portfolio_analyzer.stock_analysis import classify_trend, compute_metrics
from portfolio_analyzer.util import ohlc


def test_classify_trend_strong():
    assert classify_trend(price=110, ma50=105, ma200=100) == "STRONG"


def test_classify_trend_weak_when_price_below_ma50():
    assert classify_trend(price=104, ma50=105, ma200=100) == "WEAK"


def test_classify_trend_weak_when_mas_inverted():
    assert classify_trend(price=110, ma50=95, ma200=100) == "WEAK"


def test_classify_trend_weak_when_mas_nan():
    assert classify_trend(price=110, ma50=math.nan, ma200=100) == "WEAK"


def test_sma_insufficient_history_is_nan(series_factory):
    s = series_factory([1.0, 2.0, 3.0])
    assert math.isnan(ohlc.sma(s, 50))


def test_return_over_basic(series_factory):
    s = series_factory([float(i) for i in range(1, 52)])  # 51 values
    # price[-1] = 51, price[-51] = 1 -> return_over(50) = 50.0
    assert ohlc.return_over(s, 50) == pytest.approx(50.0)


def test_pct_from_high_at_peak():
    assert ohlc.pct_from_high(100, 100) == pytest.approx(0.0)


def test_pct_from_high_below_peak():
    assert ohlc.pct_from_high(90, 100) == pytest.approx(-0.10)


def test_compute_metrics_uptrend(linear_uptrend):
    m = compute_metrics("TEST", linear_uptrend, market_return_50d=0.01)
    assert m.trend == "STRONG"
    assert m.price > m.ma_50 > m.ma_200
    assert m.return_50d > 0
    assert m.relative_strength == pytest.approx(m.return_50d - 0.01)
    # linear uptrend -> current price is the 52w high
    assert m.drawdown_from_high == pytest.approx(0.0)
    assert not m.insufficient_history


def test_compute_metrics_downtrend(linear_downtrend):
    m = compute_metrics("TEST", linear_downtrend, market_return_50d=0.0)
    assert m.trend == "WEAK"
    assert m.return_50d < 0
    assert m.drawdown_from_high < 0


def test_compute_metrics_insufficient_history(series_factory):
    s = series_factory([100.0 + i for i in range(30)])
    m = compute_metrics("TEST", s, market_return_50d=0.0)
    assert m.insufficient_history is True
    assert m.trend == "WEAK"
