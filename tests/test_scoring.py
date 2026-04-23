from __future__ import annotations

import pytest

from portfolio_analyzer.scoring import score_stock
from portfolio_analyzer.stock_analysis import StockMetrics


def _metrics(
    *,
    trend: str = "STRONG",
    rs: float = 0.05,
    drawdown: float = -0.05,
    insufficient: bool = False,
) -> StockMetrics:
    return StockMetrics(
        symbol="X",
        price=100.0,
        ma_50=95.0,
        ma_200=90.0,
        high_52w=105.0,
        return_50d=0.05,
        relative_strength=rs,
        drawdown_from_high=drawdown,
        trend=trend,
        insufficient_history=insufficient,
    )


def test_perfect_leader_scores_hold():
    # STRONG + positive RS + near highs = 2+2+1 = 5 -> HOLD
    s = score_stock(_metrics(trend="STRONG", rs=0.05, drawdown=-0.05))
    assert s.score == 5
    assert s.decision == "HOLD"
    assert "Above 50DMA and 200DMA" in s.reasons
    assert "Outperforming market" in s.reasons
    assert "Near 52-week highs" in s.reasons


def test_weak_trend_negative_rs_large_dd_exits():
    # WEAK + negative RS + drawdown < -25% = 0 + 0 + 0 - 2 = -2 -> EXIT
    s = score_stock(_metrics(trend="WEAK", rs=-0.05, drawdown=-0.30))
    assert s.score == -2
    assert s.decision == "EXIT"
    assert "Below key MAs" in s.reasons
    assert "Underperforming market" in s.reasons
    assert "Large drawdown from highs" in s.reasons


def test_reduce_band_lower_at_2():
    # D-BT12: REDUCE_SCORE_MIN=2. STRONG + negative RS + dd middle = 2 -> REDUCE.
    s = score_stock(_metrics(trend="STRONG", rs=-0.01, drawdown=-0.15))
    assert s.score == 2
    assert s.decision == "REDUCE"


def test_score_one_is_exit():
    # D-BT12: score < 2 -> EXIT. WEAK + negative RS + near highs = 1.
    s = score_stock(_metrics(trend="WEAK", rs=-0.01, drawdown=-0.05))
    assert s.score == 1
    assert s.decision == "EXIT"


def test_hold_boundary_at_4():
    # D-BT12: HOLD_SCORE_MIN=4. STRONG + positive RS + dd middle = 4 -> HOLD.
    s = score_stock(_metrics(trend="STRONG", rs=0.01, drawdown=-0.15))
    assert s.score == 4
    assert s.decision == "HOLD"


def test_score_three_is_reduce():
    # D-BT12: score=3 now REDUCE (was HOLD). STRONG + negative RS + near highs = 3.
    s = score_stock(_metrics(trend="STRONG", rs=-0.01, drawdown=-0.05))
    assert s.score == 3
    assert s.decision == "REDUCE"


def test_exit_boundary_at_0():
    # WEAK + negative RS + middle dd = 0 -> EXIT
    s = score_stock(_metrics(trend="WEAK", rs=-0.01, drawdown=-0.15))
    assert s.score == 0
    assert s.decision == "EXIT"


def test_large_drawdown_subtracts_even_if_strong():
    # STRONG + positive RS + dd<-25% = 2 + 2 + 0 - 2 = 2 -> REDUCE
    s = score_stock(_metrics(trend="STRONG", rs=0.05, drawdown=-0.30))
    assert s.score == 2
    assert s.decision == "REDUCE"


def test_drawdown_between_thresholds_no_change():
    # -10% exactly is NOT > -10%, so +1 does not apply
    s = score_stock(_metrics(trend="STRONG", rs=0.05, drawdown=-0.10))
    assert s.score == 4
    assert "Near 52-week highs" not in s.reasons


def test_insufficient_history_flagged_in_reasons():
    s = score_stock(_metrics(trend="WEAK", rs=float("nan"), drawdown=float("nan"), insufficient=True))
    assert "Insufficient price history" in s.reasons


@pytest.mark.parametrize(
    "trend,rs,dd,expected_score,expected_decision",
    [
        ("STRONG", 0.01, -0.05, 5, "HOLD"),
        ("STRONG", 0.01, -0.15, 4, "HOLD"),
        ("STRONG", -0.01, -0.15, 2, "REDUCE"),
        ("WEAK", 0.01, -0.05, 3, "REDUCE"),
        ("WEAK", -0.01, -0.30, -2, "EXIT"),
    ],
)
def test_table_driven(trend, rs, dd, expected_score, expected_decision):
    s = score_stock(_metrics(trend=trend, rs=rs, drawdown=dd))
    assert s.score == expected_score
    assert s.decision == expected_decision
