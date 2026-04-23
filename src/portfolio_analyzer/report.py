from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic import BaseModel, Field

from portfolio_analyzer import config as cfg
from portfolio_analyzer.market import MarketState
from portfolio_analyzer.scoring import Scored
from portfolio_analyzer.stock_analysis import StockMetrics


class MarketOut(BaseModel):
    trend: str
    return_50d: float
    breadth_pct: float
    breadth_regime: str
    nifty500_trend: str
    nifty50_trend: str


class PortfolioSummaryOut(BaseModel):
    total_stocks: int
    hold_count: int
    reduce_count: int
    exit_count: int


class StockOut(BaseModel):
    symbol: str
    sector: str
    price: float
    trend: str
    relative_strength: float
    drawdown_from_high: float
    score: float
    decision: str           # state-machine output (D-BT14)
    raw_signal: str         # pre-hysteresis score-based call (D-BT18)
    prev_state: str         # state fed into the machine for this run
    reasons: list[str]


class ReportOut(BaseModel):
    date: str
    market: MarketOut
    portfolio_summary: PortfolioSummaryOut
    stocks: list[StockOut]
    top_performers: list[str] = Field(default_factory=list)
    weakest_stocks: list[str] = Field(default_factory=list)


@dataclass
class ScoredStock:
    metrics: StockMetrics
    scored: Scored           # raw score + score-based decision (diagnostic)
    sector: str
    decision: str = ""       # state-machine output (D-BT14); defaults to scored.decision
    prev_state: str = "HOLD"

    def __post_init__(self) -> None:
        if not self.decision:
            self.decision = self.scored.decision


def _nan_to_zero(x: float) -> float:
    return 0.0 if math.isnan(x) else float(x)


def _rank_by_score(stocks: list[ScoredStock], reverse: bool) -> list[str]:
    ordered = sorted(
        stocks,
        key=lambda s: (s.scored.score, _nan_to_zero(s.metrics.relative_strength)),
        reverse=reverse,
    )
    return [s.metrics.symbol for s in ordered[: cfg.TOP_N_PERFORMERS]]


def build_report(
    date_str: str,
    market: MarketState,
    scored_stocks: list[ScoredStock],
) -> ReportOut:
    stocks_out = [
        StockOut(
            symbol=s.metrics.symbol,
            sector=s.sector or "UNKNOWN",
            price=_nan_to_zero(s.metrics.price),
            trend=s.metrics.trend,
            relative_strength=_nan_to_zero(s.metrics.relative_strength),
            drawdown_from_high=_nan_to_zero(s.metrics.drawdown_from_high),
            score=s.scored.score,
            decision=s.decision,
            raw_signal=s.scored.decision,
            prev_state=s.prev_state,
            reasons=s.scored.reasons,
        )
        for s in scored_stocks
    ]
    summary = PortfolioSummaryOut(
        total_stocks=len(scored_stocks),
        hold_count=sum(1 for s in scored_stocks if s.decision == "HOLD"),
        reduce_count=sum(1 for s in scored_stocks if s.decision == "REDUCE"),
        exit_count=sum(1 for s in scored_stocks if s.decision == "EXIT"),
    )
    return ReportOut(
        date=date_str,
        market=MarketOut(
            trend=market.trend,
            return_50d=_nan_to_zero(market.return_50d),
            breadth_pct=_nan_to_zero(market.breadth_pct),
            breadth_regime=market.breadth_regime,
            nifty500_trend=market.nifty500_trend,
            nifty50_trend=market.nifty50_trend,
        ),
        portfolio_summary=summary,
        stocks=stocks_out,
        top_performers=_rank_by_score(scored_stocks, reverse=True),
        weakest_stocks=_rank_by_score(scored_stocks, reverse=False),
    )
