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


@dataclass
class DeferResolution:
    """Outcome of applying the D-BT25/D-BT26 defer mechanics to a single symbol
    for one run. Intended for the live analyzer; the backtest simulator runs
    the same rules inline at T+1 open.
    """
    decision: str                        # HOLD | REDUCE | EXIT emitted today
    prev_state: str                      # state fed into the machine this run
    pending_days_remaining: int | None   # None if no pending; else >=1 to persist
    event: str | None                    # enqueue|decrement|fire_acute|fire_expired|cancel_upgrade
    reason: str


def is_acute_breakdown(
    px: float, prev_close: float, ma200: float, dd: float, rs: float,
    dd_threshold: float = cfg.EXIT_DEFER_DD_THRESHOLD,
    gap_pct: float = cfg.EXIT_DEFER_GAP_DOWN_PCT,
) -> bool:
    """D-BT26: EXIT fires immediately (no defer) in two situations.

    1. Gap-down bypass: `(prev_close - px) / prev_close > gap_pct`.
    2. Strong breakdown: price < 200DMA AND dd < dd_threshold AND rs < 0.

    NaN inputs fail their individual leg (so missing history -> defer, the
    protective default). Mild breakdowns (dd >= dd_threshold) or strong stocks
    (rs >= 0) fall through to the defer timer.
    """
    if (not math.isnan(prev_close) and prev_close > 0 and not math.isnan(px)
            and (prev_close - px) / prev_close > gap_pct):
        return True
    below_ma = not math.isnan(ma200) and ma200 > 0 and not math.isnan(px) and px < ma200
    deep_dd = not math.isnan(dd) and dd < dd_threshold
    weak_rs = not math.isnan(rs) and rs < 0
    return below_ma and deep_dd and weak_rs


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


def _reentry_qualifies(metrics: StockMetrics) -> bool:
    """D-BT19 re-entry gate: price > 200DMA AND > 50DMA AND RS > 0 AND drawdown >= EXIT_GATE_DRAWDOWN.

    D-BT24: drawdown check added so REENTRY cannot pick names that the hard-gate
    is still forcing to EXIT, which previously produced a REENTRY-then-immediate-EXIT
    loop (noise, costless churn in backtests).
    """
    if math.isnan(metrics.ma_200) or math.isnan(metrics.ma_50):
        return False
    if math.isnan(metrics.relative_strength):
        return False
    dd = metrics.drawdown_from_high
    if not math.isnan(dd) and dd < cfg.EXIT_GATE_DRAWDOWN:
        return False
    return (
        metrics.price > metrics.ma_200
        and metrics.price > metrics.ma_50
        and metrics.relative_strength > 0
    )


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
        if _reentry_qualifies(metrics):
            return StrategyResult(
                STATE_REDUCE, raw_signal,
                "re-entry from EXIT: price>50DMA>200DMA and RS>0",
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


def resolve_with_defer(
    prev_state: str,
    metrics: StockMetrics,
    raw_signal: str,
    prev_close: float,
    pending_days: int | None = None,
    defer_days: int | None = None,
    dd_threshold: float | None = None,
    gap_pct: float | None = None,
) -> DeferResolution:
    """Live-parity wrapper around ``decide`` with D-BT25/D-BT26 defer mechanics.

    ``pending_days`` is the remaining counter carried from yesterday's report
    (None if the symbol is not currently deferred). ``prev_close`` is the
    previous session's close used for the gap-down leg. Pass ``defer_days=0``
    to bypass the timer entirely (legacy immediate-EXIT behaviour).
    """
    defer_days = cfg.EXIT_DEFER_DAYS if defer_days is None else defer_days
    dd_threshold = cfg.EXIT_DEFER_DD_THRESHOLD if dd_threshold is None else dd_threshold
    gap_pct = cfg.EXIT_DEFER_GAP_DOWN_PCT if gap_pct is None else gap_pct
    if prev_state not in VALID_STATES:
        prev_state = STATE_HOLD

    if pending_days is not None and defer_days > 0:
        today = decide(prev_state, metrics, raw_signal)
        if today.decision != STATE_EXIT:
            return DeferResolution(
                today.decision, prev_state, None, "cancel_upgrade",
                f"deferred EXIT cancelled: today's signal is {today.decision}",
            )
        acute = is_acute_breakdown(
            metrics.price, prev_close, metrics.ma_200,
            metrics.drawdown_from_high, metrics.relative_strength,
            dd_threshold, gap_pct,
        )
        remaining = pending_days - 1
        if not acute and remaining > 0:
            return DeferResolution(
                prev_state, prev_state, remaining, "decrement",
                f"deferred EXIT pending ({remaining} day(s) left)",
            )
        return DeferResolution(
            STATE_EXIT, prev_state, None,
            "fire_acute" if acute else "fire_expired",
            "acute breakdown: firing EXIT now" if acute
            else "defer timer expired: firing EXIT",
        )

    result = decide(prev_state, metrics, raw_signal)
    if result.decision != STATE_EXIT or defer_days <= 0:
        return DeferResolution(
            result.decision, prev_state, None, None, result.reason,
        )
    acute = is_acute_breakdown(
        metrics.price, prev_close, metrics.ma_200,
        metrics.drawdown_from_high, metrics.relative_strength,
        dd_threshold, gap_pct,
    )
    if acute:
        return DeferResolution(
            STATE_EXIT, prev_state, None, "fire_acute",
            "acute breakdown: EXIT fires immediately",
        )
    return DeferResolution(
        prev_state, prev_state, defer_days, "enqueue",
        f"EXIT deferred {defer_days} day(s): mild breakdown, awaiting confirmation",
    )
