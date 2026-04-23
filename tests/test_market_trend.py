from __future__ import annotations

import math

import pandas as pd
import pytest

from portfolio_analyzer.market import (
    apply_breadth_override,
    blend_trend,
    breadth_regime,
    classify_index_trend,
    compute_breadth_pct,
    compute_market_state,
)


def _series(values: list[float]) -> pd.Series:
    idx = pd.date_range(start="2020-01-01", periods=len(values), freq="B")
    return pd.Series(values, index=idx, dtype="float64")


def test_classify_uptrend_linear_up():
    s = _series([100 + i * 0.5 for i in range(260)])
    assert classify_index_trend(s) == "UPTREND"


def test_classify_downtrend_linear_down():
    s = _series([200 - i * 0.5 for i in range(260)])
    assert classify_index_trend(s) == "DOWNTREND"


def test_classify_sideways_short_history():
    s = _series([100.0] * 30)
    assert classify_index_trend(s) == "SIDEWAYS"


def test_blend_both_up():
    assert blend_trend("UPTREND", "UPTREND") == "UPTREND"


def test_blend_both_down():
    assert blend_trend("DOWNTREND", "DOWNTREND") == "DOWNTREND"


def test_blend_500_up_50_down_gives_up():
    # 0.7*1 + 0.3*-1 = 0.4 -> SIDEWAYS (below 0.5 threshold)
    assert blend_trend("UPTREND", "DOWNTREND") == "SIDEWAYS"


def test_blend_500_up_50_sideways_gives_up():
    # 0.7*1 + 0.3*0 = 0.7 -> UPTREND
    assert blend_trend("UPTREND", "SIDEWAYS") == "UPTREND"


def test_blend_500_sideways_50_up_gives_sideways():
    # 0.7*0 + 0.3*1 = 0.3 -> SIDEWAYS
    assert blend_trend("SIDEWAYS", "UPTREND") == "SIDEWAYS"


def test_blend_500_down_50_up_gives_sideways():
    # 0.7*-1 + 0.3*1 = -0.4 -> SIDEWAYS
    assert blend_trend("DOWNTREND", "UPTREND") == "SIDEWAYS"


@pytest.mark.parametrize(
    "pct,expected",
    [(0.70, "strong"), (0.65, "strong"), (0.50, "mixed"), (0.40, "mixed"), (0.39, "weak"), (0.10, "weak")],
)
def test_breadth_regime(pct, expected):
    assert breadth_regime(pct) == expected


def test_breadth_override_uptrend_weak_breadth_downgrades():
    assert apply_breadth_override("UPTREND", 0.30) == "SIDEWAYS"


def test_breadth_override_uptrend_mixed_breadth_unchanged():
    assert apply_breadth_override("UPTREND", 0.50) == "UPTREND"


def test_breadth_override_downtrend_strong_breadth_upgrades():
    assert apply_breadth_override("DOWNTREND", 0.70) == "SIDEWAYS"


def test_breadth_override_downtrend_weak_breadth_unchanged():
    assert apply_breadth_override("DOWNTREND", 0.30) == "DOWNTREND"


def test_breadth_override_nan_unchanged():
    assert apply_breadth_override("UPTREND", math.nan) == "UPTREND"


def test_compute_breadth_pct_basic():
    # 3 stocks: two above 50DMA, one below
    above = _series([100.0] * 60 + [200.0])   # last > MA
    above2 = _series([100.0] * 60 + [150.0])
    below = _series([100.0] * 60 + [50.0])    # last < MA
    pct = compute_breadth_pct({"A": above, "B": above2, "C": below})
    assert pct == pytest.approx(2 / 3)


def test_compute_breadth_pct_ignores_short_history():
    short = _series([100.0] * 10)
    long_ok = _series([100.0] * 60 + [200.0])
    pct = compute_breadth_pct({"S": short, "L": long_ok})
    assert pct == pytest.approx(1.0)  # only "L" counted


def test_compute_breadth_pct_all_short_is_nan():
    short = _series([100.0] * 10)
    assert math.isnan(compute_breadth_pct({"S": short}))


def test_market_state_includes_all_fields():
    up = _series([100 + i * 0.5 for i in range(260)])
    state = compute_market_state(up, up, breadth_pct=0.70)
    assert state.trend == "UPTREND"
    assert state.nifty500_trend == "UPTREND"
    assert state.nifty50_trend == "UPTREND"
    assert state.breadth_regime == "strong"
    assert state.return_50d > 0


def test_market_state_breadth_override_to_sideways():
    up = _series([100 + i * 0.5 for i in range(260)])
    state = compute_market_state(up, up, breadth_pct=0.30)
    assert state.trend == "SIDEWAYS"
    assert state.nifty500_trend == "UPTREND"
