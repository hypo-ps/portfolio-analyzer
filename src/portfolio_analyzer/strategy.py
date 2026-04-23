"""Phase 0 decision state machine.

Used by both the live analyzer and the backtest to guarantee parity (D-BT11).
The raw score decision from `scoring.score_stock` is retained as a diagnostic
signal (`raw_signal`); the actionable `decision` is produced here, as a
function of the previous-day decision plus today's metrics.

Rules are defined in decisions.md D-BT13 (hard EXIT gate) and D-BT14 (state
machine with hysteresis and REDUCE->HOLD upgrade).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from portfolio_analyzer import config as cfg
from portfolio_analyzer.stock_analysis import StockMetrics

STATE_HOLD = "HOLD"
STATE_REDUCE = "REDUCE"
STATE_EXIT = "EXIT"

VALID_STATES = {STATE_HOLD, STATE_REDUCE, STATE_EXIT}


@dataclass
class StrategyResult:
    decision: str            # HOLD | REDUCE | EXIT
    raw_signal: str          # score-based signal before state-machine filter
    reason: str              # short human-readable cause


def hard_gate_forces_exit(metrics: StockMetrics) -> bool:
    """EXIT is only permitted if at least one hard-gate condition holds."""
    if not math.isnan(metrics.ma_200) and metrics.price < metrics.ma_200:
        return True
    dd = metrics.drawdown_from_high
    if not math.isnan(dd) and dd < cfg.EXIT_GATE_DRAWDOWN:
        return True
    return False


def _filtered_raw_signal(raw: str, metrics: StockMetrics) -> tuple[str, str]:
    """Apply D-BT13 gate: raw EXIT downgraded to REDUCE if gate not met."""
    if raw == STATE_EXIT and not hard_gate_forces_exit(metrics):
        return STATE_REDUCE, "EXIT suppressed by hard gate (price>=200DMA and dd>=-15%)"
    return raw, ""


def _reentry_primary(metrics: StockMetrics) -> bool:
    """D-BT19/D-BT24 primary gate: price > 200DMA AND > 50DMA AND RS > 0 AND
    drawdown >= EXIT_GATE_DRAWDOWN.
    """
    if math.isnan(metrics.ma_200) or math.isnan(metrics.ma_50):
        return False
    dd = metrics.drawdown_from_high
    if not math.isnan(dd) and dd < cfg.EXIT_GATE_DRAWDOWN:
        return False
    return (
        metrics.price > metrics.ma_200
        and metrics.price > metrics.ma_50
        and metrics.relative_strength > 0
    )


def _reentry_fast(metrics: StockMetrics) -> bool:
    """D-BT27 secondary gate: price > 50DMA AND RS > 0 AND rebound from recent
    low >= REFILL_REBOUND_THRESHOLD. Bypasses the 200DMA / drawdown gates so a
    V-shape recovery can re-engage before the long moving average catches up.
    """
    if math.isnan(metrics.ma_50):
        return False
    rebound = metrics.rebound_from_low
    if math.isnan(rebound) or rebound < cfg.REFILL_REBOUND_THRESHOLD:
        return False
    return metrics.price > metrics.ma_50 and metrics.relative_strength > 0


def _reentry_qualifies(metrics: StockMetrics) -> bool:
    """Re-entry allowed when either the primary (D-BT19/D-BT24) or the
    secondary fast-rebound (D-BT27) gate passes.
    """
    if math.isnan(metrics.relative_strength):
        return False
    return _reentry_primary(metrics) or _reentry_fast(metrics)


def decide(prev_state: str, metrics: StockMetrics, raw_signal: str) -> StrategyResult:
    """State-machine transition. `raw_signal` is the output of score_stock().

    - EXIT  -> REDUCE iff re-entry gate (D-BT19); otherwise stay EXIT.
    - HOLD  -> EXIT  iff hard gate.
    - HOLD  -> REDUCE iff trend=WEAK AND RS<0.
    - REDUCE -> EXIT iff hard gate.
    - REDUCE -> HOLD iff trend=STRONG AND RS>0.
    - otherwise stay put.
    """
    if prev_state not in VALID_STATES:
        prev_state = STATE_HOLD

    if prev_state == STATE_EXIT:
        if _reentry_primary(metrics) and not math.isnan(metrics.relative_strength):
            return StrategyResult(
                STATE_REDUCE, raw_signal,
                "re-entry from EXIT: price>50DMA>200DMA and RS>0",
            )
        if _reentry_fast(metrics) and not math.isnan(metrics.relative_strength):
            return StrategyResult(
                STATE_REDUCE, raw_signal,
                "re-entry from EXIT: fast rebound (D-BT27)",
            )
        return StrategyResult(STATE_EXIT, raw_signal, "EXIT maintained (re-entry gate not met)")

    filtered, gate_note = _filtered_raw_signal(raw_signal, metrics)
    rs = metrics.relative_strength
    rs_known = not math.isnan(rs)

    if prev_state == STATE_HOLD:
        if hard_gate_forces_exit(metrics):
            return StrategyResult(STATE_EXIT, raw_signal, "hard gate: price<200DMA or dd<-15%")
        # Only downgrade on a genuine weak-trend + negative-RS confirmation.
        if metrics.trend == "WEAK" and rs_known and rs < 0:
            note = "trend flipped WEAK and RS<0"
            if filtered == STATE_HOLD:
                note += " (raw score HOLD but hysteresis allows REDUCE)"
            return StrategyResult(STATE_REDUCE, raw_signal, note)
        return StrategyResult(STATE_HOLD, raw_signal, gate_note or "hysteresis holds HOLD")

    # prev_state == REDUCE
    if hard_gate_forces_exit(metrics):
        return StrategyResult(STATE_EXIT, raw_signal, "hard gate from REDUCE")
    if metrics.trend == "STRONG" and rs_known and rs > 0:
        return StrategyResult(STATE_HOLD, raw_signal, "recovery upgrade: STRONG + RS>0")
    return StrategyResult(STATE_REDUCE, raw_signal, "hysteresis holds REDUCE")
