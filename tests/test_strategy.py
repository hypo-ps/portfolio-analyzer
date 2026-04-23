from __future__ import annotations

import math

from portfolio_analyzer import strategy
from portfolio_analyzer.stock_analysis import StockMetrics


def _m(*, price: float = 100.0, ma200: float = 90.0, dd: float = -0.05,
       trend: str = "STRONG", rs: float = 0.02) -> StockMetrics:
    return StockMetrics(
        symbol="X", price=price, ma_50=95.0, ma_200=ma200, high_52w=120.0,
        return_50d=0.05, relative_strength=rs, drawdown_from_high=dd,
        trend=trend, insufficient_history=False,
    )


def test_hard_gate_requires_below_200dma_or_large_drawdown():
    assert strategy.hard_gate_forces_exit(_m(price=80.0, ma200=90.0)) is True
    assert strategy.hard_gate_forces_exit(_m(dd=-0.20)) is True
    assert strategy.hard_gate_forces_exit(_m(price=120.0, ma200=90.0, dd=-0.05)) is False


def test_raw_exit_without_hard_gate_never_becomes_exit():
    # Raw score says EXIT but price above 200DMA and dd shallow -> state machine
    # never emits EXIT; it falls through to HOLD/REDUCE based on hysteresis.
    r = strategy.decide(strategy.STATE_HOLD,
                        _m(price=120.0, ma200=90.0, dd=-0.05, trend="WEAK", rs=-0.01),
                        strategy.STATE_EXIT)
    assert r.decision == strategy.STATE_REDUCE  # WEAK + rs<0 triggers HOLD->REDUCE
    # STRONG + positive RS with a raw EXIT (contrived) -> hysteresis keeps HOLD.
    r2 = strategy.decide(strategy.STATE_HOLD,
                         _m(price=120.0, ma200=90.0, dd=-0.05, trend="STRONG", rs=0.02),
                         strategy.STATE_EXIT)
    assert r2.decision == strategy.STATE_HOLD


def test_hold_to_reduce_requires_weak_trend_and_negative_rs():
    # Weak trend alone not enough; positive RS keeps HOLD.
    r = strategy.decide(strategy.STATE_HOLD,
                        _m(price=120.0, ma200=90.0, trend="WEAK", rs=0.01),
                        strategy.STATE_HOLD)
    assert r.decision == strategy.STATE_HOLD
    # Weak + negative RS -> REDUCE.
    r2 = strategy.decide(strategy.STATE_HOLD,
                         _m(price=120.0, ma200=90.0, trend="WEAK", rs=-0.01),
                         strategy.STATE_HOLD)
    assert r2.decision == strategy.STATE_REDUCE


def test_hold_to_exit_via_hard_gate():
    r = strategy.decide(strategy.STATE_HOLD,
                        _m(price=80.0, ma200=90.0, trend="WEAK", rs=-0.01),
                        strategy.STATE_EXIT)
    assert r.decision == strategy.STATE_EXIT


def test_reduce_upgrades_to_hold_on_recovery():
    r = strategy.decide(strategy.STATE_REDUCE,
                        _m(price=120.0, ma200=90.0, trend="STRONG", rs=0.05),
                        strategy.STATE_HOLD)
    assert r.decision == strategy.STATE_HOLD


def test_reduce_stays_reduce_without_full_recovery():
    # Strong trend but RS still negative -> stay REDUCE.
    r = strategy.decide(strategy.STATE_REDUCE,
                        _m(price=120.0, ma200=90.0, trend="STRONG", rs=-0.01),
                        strategy.STATE_HOLD)
    assert r.decision == strategy.STATE_REDUCE


def test_reduce_to_exit_via_hard_gate():
    r = strategy.decide(strategy.STATE_REDUCE,
                        _m(price=80.0, ma200=90.0, trend="WEAK", rs=-0.05),
                        strategy.STATE_EXIT)
    assert r.decision == strategy.STATE_EXIT


def test_exit_reenters_when_all_conditions_met():
    # price>200DMA AND price>50DMA AND RS>0 -> EXIT -> REDUCE
    r = strategy.decide(strategy.STATE_EXIT,
                        _m(price=200.0, ma200=90.0, trend="STRONG", rs=0.10),
                        strategy.STATE_HOLD)
    assert r.decision == strategy.STATE_REDUCE


def test_exit_stays_when_reentry_gate_unmet():
    # price below 50DMA -> re-entry blocked (ma_50 defaults to 95.0 in _m)
    r1 = strategy.decide(strategy.STATE_EXIT,
                         _m(price=94.0, ma200=90.0, rs=0.10),
                         strategy.STATE_HOLD)
    assert r1.decision == strategy.STATE_EXIT
    # RS <= 0 -> blocked
    r2 = strategy.decide(strategy.STATE_EXIT,
                         _m(price=200.0, ma200=90.0, rs=-0.01),
                         strategy.STATE_HOLD)
    assert r2.decision == strategy.STATE_EXIT
    # price below 200DMA -> blocked (hard gate ground)
    r3 = strategy.decide(strategy.STATE_EXIT,
                         _m(price=80.0, ma200=90.0, rs=0.10),
                         strategy.STATE_HOLD)
    assert r3.decision == strategy.STATE_EXIT


def test_exit_reentry_requires_known_rs_and_mas():
    # NaN RS -> no re-entry
    r = strategy.decide(strategy.STATE_EXIT,
                        _m(price=200.0, ma200=90.0, rs=float("nan")),
                        strategy.STATE_HOLD)
    assert r.decision == strategy.STATE_EXIT
    # NaN MA200 -> no re-entry
    r2 = strategy.decide(strategy.STATE_EXIT,
                        _m(price=200.0, ma200=float("nan"), rs=0.10),
                        strategy.STATE_HOLD)
    assert r2.decision == strategy.STATE_EXIT


def test_reentry_blocked_when_drawdown_below_exit_gate():
    # D-BT24: even with price > 200DMA, > 50DMA, RS > 0, a drawdown worse than
    # EXIT_GATE_DRAWDOWN keeps the position in EXIT to avoid REENTRY-then-EXIT loops.
    r = strategy.decide(strategy.STATE_EXIT,
                        _m(price=200.0, ma200=90.0, rs=0.10, dd=-0.20),
                        strategy.STATE_HOLD)
    assert r.decision == strategy.STATE_EXIT
    # Shallow drawdown (better than -15%) still allows reentry.
    r2 = strategy.decide(strategy.STATE_EXIT,
                         _m(price=200.0, ma200=90.0, rs=0.10, dd=-0.10),
                         strategy.STATE_HOLD)
    assert r2.decision == strategy.STATE_REDUCE



def test_unknown_prev_state_defaults_to_hold():
    r = strategy.decide("GARBAGE",
                        _m(price=120.0, ma200=90.0, trend="STRONG", rs=0.05),
                        strategy.STATE_HOLD)
    assert r.decision == strategy.STATE_HOLD


def test_nan_ma200_does_not_trigger_hard_gate():
    # Insufficient history: MA200 NaN must not force EXIT.
    m = _m(price=100.0, ma200=float("nan"), dd=float("nan"), trend="WEAK", rs=-0.01)
    assert strategy.hard_gate_forces_exit(m) is False
    r = strategy.decide(strategy.STATE_HOLD, m, strategy.STATE_HOLD)
    assert r.decision in (strategy.STATE_HOLD, strategy.STATE_REDUCE)
    assert r.decision != strategy.STATE_EXIT
    # Explicit: NaN RS keeps HOLD from transitioning to REDUCE.
    m2 = _m(price=100.0, ma200=float("nan"), trend="WEAK", rs=float("nan"))
    assert math.isnan(m2.relative_strength)
    r2 = strategy.decide(strategy.STATE_HOLD, m2, strategy.STATE_HOLD)
    assert r2.decision == strategy.STATE_HOLD


# --- D-BT28 live defer-resolver tests ---------------------------------------


def test_is_acute_breakdown_gap_down_bypass():
    # prev=100, px=96 -> 4% gap > 3% default threshold.
    assert strategy.is_acute_breakdown(96.0, 100.0, 110.0, -0.05, 0.05) is True
    # 2% gap < 3% and other legs negative -> not acute.
    assert strategy.is_acute_breakdown(98.0, 100.0, 110.0, -0.05, 0.05) is False


def test_is_acute_breakdown_triple_gate():
    # All three legs true -> acute.
    assert strategy.is_acute_breakdown(
        px=88.0, prev_close=89.0, ma200=95.0, dd=-0.12, rs=-0.05,
    ) is True
    # Any single leg false -> not acute (mild breakdown).
    assert strategy.is_acute_breakdown(88.0, 89.0, 95.0, -0.05, -0.05) is False  # dd mild
    assert strategy.is_acute_breakdown(88.0, 89.0, 95.0, -0.12, 0.05) is False   # rs strong
    assert strategy.is_acute_breakdown(96.0, 97.0, 95.0, -0.12, -0.05) is False  # above ma200


def test_is_acute_breakdown_nan_legs_fail_protectively():
    # With all triple-gate legs benign, each NaN collapses the only remaining
    # path (gap-down) to False -> fall through to defer.
    nan = float("nan")
    assert strategy.is_acute_breakdown(96.0, nan, 110.0, -0.05, 0.05) is False
    assert strategy.is_acute_breakdown(nan, 100.0, 110.0, -0.05, 0.05) is False
    # NaN ma200 kills the triple-gate leg even with deep dd and weak rs.
    assert strategy.is_acute_breakdown(96.0, 96.5, nan, -0.20, -0.10) is False


def test_resolve_enqueues_mild_exit():
    # EXIT signal with only a shallow dd -> enqueue, freeze state.
    m = _m(price=85.0, ma200=90.0, dd=-0.05, trend="WEAK", rs=-0.01)
    r = strategy.resolve_with_defer(
        strategy.STATE_HOLD, m, raw_signal=strategy.STATE_EXIT,
        prev_close=86.0, pending_days=None,
    )
    assert r.decision == strategy.STATE_HOLD  # frozen
    assert r.pending_days_remaining == 2
    assert r.event == "enqueue"


def test_resolve_fires_acute_on_enqueue_day():
    # Triple-gate trips on the very day EXIT is first emitted -> no defer.
    m = _m(price=85.0, ma200=95.0, dd=-0.20, trend="WEAK", rs=-0.05)
    r = strategy.resolve_with_defer(
        strategy.STATE_HOLD, m, raw_signal=strategy.STATE_EXIT,
        prev_close=86.0, pending_days=None,
    )
    assert r.decision == strategy.STATE_EXIT
    assert r.pending_days_remaining is None
    assert r.event == "fire_acute"


def test_resolve_decrements_and_then_fires_on_expiry():
    # 2-day timer: decrement first, fire_expired on the second resolution.
    m = _m(price=88.0, ma200=90.0, dd=-0.05, trend="WEAK", rs=-0.01)
    day1 = strategy.resolve_with_defer(
        strategy.STATE_HOLD, m, raw_signal=strategy.STATE_EXIT,
        prev_close=88.2, pending_days=2,
    )
    assert day1.event == "decrement" and day1.pending_days_remaining == 1
    assert day1.decision == strategy.STATE_HOLD

    day2 = strategy.resolve_with_defer(
        strategy.STATE_HOLD, m, raw_signal=strategy.STATE_EXIT,
        prev_close=88.2, pending_days=1,
    )
    assert day2.event == "fire_expired"
    assert day2.decision == strategy.STATE_EXIT
    assert day2.pending_days_remaining is None


def test_resolve_fires_acute_mid_defer():
    # Pending = 2; gap-down today > 3% -> fire immediately, don't wait out timer.
    m = _m(price=85.0, ma200=90.0, dd=-0.12, trend="WEAK", rs=-0.05)
    r = strategy.resolve_with_defer(
        strategy.STATE_HOLD, m, raw_signal=strategy.STATE_EXIT,
        prev_close=90.0, pending_days=2,   # 90 -> 85 = ~5.5% gap
    )
    assert r.event == "fire_acute"
    assert r.decision == strategy.STATE_EXIT


def test_resolve_cancels_when_signal_upgrades():
    # Pending EXIT, but today's strategy output is no longer EXIT -> cancel.
    m = _m(price=200.0, ma200=90.0, dd=-0.05, trend="STRONG", rs=0.10)
    r = strategy.resolve_with_defer(
        strategy.STATE_HOLD, m, raw_signal=strategy.STATE_HOLD,
        prev_close=198.0, pending_days=1,
    )
    assert r.event == "cancel_upgrade"
    assert r.pending_days_remaining is None
    assert r.decision == strategy.STATE_HOLD


def test_resolve_defer_days_zero_matches_plain_decide():
    # Bypass switch: defer_days=0 should behave identically to strategy.decide.
    m = _m(price=80.0, ma200=90.0, dd=-0.20, trend="WEAK", rs=-0.05)
    r = strategy.resolve_with_defer(
        strategy.STATE_HOLD, m, raw_signal=strategy.STATE_EXIT,
        prev_close=81.0, pending_days=None, defer_days=0,
    )
    direct = strategy.decide(strategy.STATE_HOLD, m, strategy.STATE_EXIT)
    assert r.decision == direct.decision == strategy.STATE_EXIT
    assert r.pending_days_remaining is None
    assert r.event is None


def test_resolve_exit_prev_state_not_re_enqueued():
    # prev=EXIT + re-entry gate unmet: decide returns EXIT, but the defer queue
    # must not latch onto a symbol that is already in the EXIT bucket.
    m = _m(price=80.0, ma200=90.0, dd=-0.20, trend="WEAK", rs=-0.05)
    r = strategy.resolve_with_defer(
        strategy.STATE_EXIT, m, raw_signal=strategy.STATE_EXIT,
        prev_close=81.0, pending_days=None,
    )
    # Acute conditions met -> fire_acute (one-off from within EXIT->EXIT is
    # harmless); state stays EXIT either way, no stale pending created.
    assert r.decision == strategy.STATE_EXIT
    assert r.pending_days_remaining is None
