from __future__ import annotations

import math
from dataclasses import dataclass

from portfolio_analyzer import config as cfg
from portfolio_analyzer.stock_analysis import StockMetrics


@dataclass
class Scored:
    score: float
    decision: str  # "HOLD" | "REDUCE" | "EXIT"
    reasons: list[str]


def score_stock(m: StockMetrics) -> Scored:
    score = 0
    reasons: list[str] = []

    if m.trend == "STRONG":
        score += 2
        reasons.append("Above 50DMA and 200DMA")
    else:
        reasons.append("Below key MAs")

    if not math.isnan(m.relative_strength) and m.relative_strength > 0:
        score += 2
        reasons.append("Outperforming market")
    else:
        reasons.append("Underperforming market")

    if not math.isnan(m.drawdown_from_high) and m.drawdown_from_high > cfg.NEAR_HIGH_DRAWDOWN:
        score += 1
        reasons.append("Near 52-week highs")

    if not math.isnan(m.drawdown_from_high) and m.drawdown_from_high < cfg.LARGE_DRAWDOWN:
        score -= 2
        reasons.append("Large drawdown from highs")

    if m.insufficient_history:
        reasons.append("Insufficient price history")

    decision = (
        "HOLD" if score >= cfg.HOLD_SCORE_MIN
        else "REDUCE" if score >= cfg.REDUCE_SCORE_MIN
        else "EXIT"
    )
    return Scored(score=float(score), decision=decision, reasons=reasons)
