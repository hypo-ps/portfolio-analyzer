from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from portfolio_analyzer import config as cfg
from portfolio_analyzer.backtest import simulator


@pytest.fixture(autouse=True)
def _zero_costs(monkeypatch, request):
    """Isolate simulator tests from realistic costs/slippage."""
    if "with_costs" in request.keywords:
        return
    monkeypatch.setattr(cfg, "TRANSACTION_COST_BPS", 0.0)
    monkeypatch.setattr(cfg, "SLIPPAGE_BPS", 0.0)


def _bdates(n: int, start: str = "2024-01-02") -> pd.DatetimeIndex:
    return pd.date_range(start=start, periods=n, freq="B")


def test_init_allocates_equal_capital_and_buys_at_start_open():
    dates = _bdates(5)
    opens = pd.DataFrame({"A": [10.0] * 5, "B": [20.0] * 5}, index=dates)
    closes = pd.DataFrame({"A": [10.0] * 5, "B": [20.0] * 5}, index=dates)
    # All HOLD -> no trades after init
    decisions = pd.DataFrame("HOLD", index=dates, columns=["A", "B"])
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=10_000.0, holding_symbols=["A", "B"],
        open_df=opens, close_df=closes, decisions=decisions,
    )
    # 5000 / 10 = 500 shares of A; 5000 / 20 = 250 shares of B
    assert abs(res.final_positions["A"] - 500.0) < 1e-9
    assert abs(res.final_positions["B"] - 250.0) < 1e-9
    # Equity should be stable at 10000 throughout (prices flat).
    assert abs(res.equity_curve.iloc[-1] - 10_000.0) < 1e-6
    assert res.decisions_history.empty


def test_exit_signal_executes_next_day_at_open():
    dates = _bdates(5)
    opens = pd.DataFrame({"A": [10.0, 10.0, 10.0, 10.0, 10.0]}, index=dates)
    closes = pd.DataFrame({"A": [10.0, 10.0, 10.0, 10.0, 10.0]}, index=dates)
    # HOLD on day 0, EXIT signal emitted on day 1 close -> executed on day 2 open
    decisions = pd.DataFrame(
        {"A": ["HOLD", "EXIT", "EXIT", "EXIT", "EXIT"]}, index=dates,
    )
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=1000.0, holding_symbols=["A"],
        open_df=opens, close_df=closes, decisions=decisions,
    )
    # One fill: INIT (day 0), one EXIT (day 2). That's 2 fills.
    sides = [f.side for f in res.fills]
    assert sides.count("BUY") == 1
    assert sides.count("SELL") == 1
    # After exit at flat price, equity still ~1000
    assert abs(res.equity_curve.iloc[-1] - 1000.0) < 1e-6
    # Final position should be 0
    assert res.final_positions["A"] == 0.0


def test_reduce_then_exit_transitions_fire_once_each():
    dates = _bdates(10)
    opens = pd.DataFrame({"A": [10.0] * 10}, index=dates)
    closes = pd.DataFrame({"A": [10.0] * 10}, index=dates)
    # After day 2: REDUCE forever; after day 5: EXIT forever.
    sigs = ["HOLD", "HOLD", "REDUCE", "REDUCE", "REDUCE", "EXIT", "EXIT", "EXIT", "EXIT", "EXIT"]
    decisions = pd.DataFrame({"A": sigs}, index=dates)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=1000.0, holding_symbols=["A"],
        open_df=opens, close_df=closes, decisions=decisions,
    )
    # Expect: 1 BUY (init), 1 SELL (reduce), 1 SELL (exit) = 3 fills
    sides = [f.side for f in res.fills]
    assert sides.count("BUY") == 1
    assert sides.count("SELL") == 2
    reasons = [f.reason for f in res.fills]
    assert "REDUCE" in reasons
    assert "EXIT" in reasons


def test_market_value_drives_equity_curve():
    dates = _bdates(5)
    opens = pd.DataFrame({"A": [10.0] * 5}, index=dates)
    closes = pd.DataFrame({"A": [10.0, 11.0, 12.0, 13.0, 14.0]}, index=dates)
    decisions = pd.DataFrame("HOLD", index=dates, columns=["A"])
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=1000.0, holding_symbols=["A"],
        open_df=opens, close_df=closes, decisions=decisions,
    )
    # 100 shares bought at 10 each -> equity = 100 * close_t
    expected = [100.0 * c for c in [10.0, 11.0, 12.0, 13.0, 14.0]]
    np.testing.assert_allclose(res.equity_curve.values, expected, rtol=1e-9)


def test_missing_price_skips_init_safely():
    dates = _bdates(3)
    opens = pd.DataFrame({"A": [10.0, 10.0, 10.0], "B": [float("nan"), 20.0, 20.0]}, index=dates)
    closes = pd.DataFrame({"A": [10.0, 10.0, 10.0], "B": [float("nan"), 20.0, 20.0]}, index=dates)
    decisions = pd.DataFrame("HOLD", index=dates, columns=["A", "B"])
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=1000.0, holding_symbols=["A", "B"],
        open_df=opens, close_df=closes, decisions=decisions,
    )
    # B had no open price at start -> not bought
    assert "B" not in res.final_positions or res.final_positions["B"] == 0.0
    assert res.final_positions.get("A", 0.0) > 0.0


def test_exposure_floor_blocks_over_reduction_in_uptrend():
    dates = _bdates(6)
    syms = ["A", "B", "C"]
    opens = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    closes = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    # All three stocks EXIT simultaneously: first EXIT drops 1.0->0.667 (allowed),
    # second would drop 0.667->0.333 (<0.5) so blocked; third likewise blocked.
    sig = pd.DataFrame({s: ["HOLD"] + ["EXIT"] * 5 for s in syms}, index=dates)
    trend = pd.Series(["UPTREND"] * 6, index=dates)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=3000.0, holding_symbols=syms,
        open_df=opens, close_df=closes, decisions=sig, market_trend=trend,
    )
    # Exactly one SELL should fire; the other two EXIT signals are blocked.
    sell_fills = [f for f in res.fills if f.side == "SELL"]
    assert len(sell_fills) == 1
    assert not res.blocked_history.empty
    # Final exposure stays at 2/3 (two positions held).
    assert abs(res.exposure_curve.iloc[-1] - (2.0 / 3.0)) < 1e-6


def test_exposure_floor_active_in_sideways():
    # D-BT20: floor now active in SIDEWAYS too; single-name EXIT would breach.
    dates = _bdates(4)
    opens = pd.DataFrame({"A": [10.0] * 4}, index=dates)
    closes = pd.DataFrame({"A": [10.0] * 4}, index=dates)
    sig = pd.DataFrame({"A": ["HOLD", "EXIT", "EXIT", "EXIT"]}, index=dates)
    trend = pd.Series(["SIDEWAYS"] * 4, index=dates)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=1000.0, holding_symbols=["A"],
        open_df=opens, close_df=closes, decisions=sig, market_trend=trend,
    )
    # Floor blocks the single-name EXIT: no SELL fires, position still held.
    assert not any(f.side == "SELL" for f in res.fills)
    assert res.final_positions["A"] > 0
    assert not res.blocked_history.empty


def test_exposure_floor_inactive_in_downtrend():
    # D-BT20: floor disabled only in DOWNTREND; EXIT should fire.
    dates = _bdates(4)
    opens = pd.DataFrame({"A": [10.0] * 4}, index=dates)
    closes = pd.DataFrame({"A": [10.0] * 4}, index=dates)
    sig = pd.DataFrame({"A": ["HOLD", "EXIT", "EXIT", "EXIT"]}, index=dates)
    trend = pd.Series(["DOWNTREND"] * 4, index=dates)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=1000.0, holding_symbols=["A"],
        open_df=opens, close_df=closes, decisions=sig, market_trend=trend,
    )
    assert any(f.side == "SELL" for f in res.fills)
    assert res.final_positions["A"] == 0.0


def test_reentry_from_exit_buys_half_initial_allocation():
    # EXIT on day 2, then REDUCE (re-entry) on day 4: buy should be 50% of INIT rupees.
    dates = _bdates(6)
    opens = pd.DataFrame({"A": [10.0] * 6}, index=dates)
    closes = pd.DataFrame({"A": [10.0] * 6}, index=dates)
    sig = pd.DataFrame(
        {"A": ["HOLD", "EXIT", "EXIT", "REDUCE", "REDUCE", "REDUCE"]}, index=dates,
    )
    # No market_trend -> floor off, both transitions fire freely.
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=1000.0, holding_symbols=["A"],
        open_df=opens, close_df=closes, decisions=sig,
    )
    fills = res.fills
    # INIT buy (100 sh), EXIT sell (100 sh), REENTRY buy (50 sh at 50% of 1000 = 500)
    sides_reasons = [(f.side, f.reason) for f in fills]
    assert ("BUY", "INIT") in sides_reasons
    assert ("SELL", "EXIT") in sides_reasons
    reentry = [f for f in fills if f.reason == "REENTRY"]
    assert len(reentry) == 1
    assert abs(reentry[0].shares - 50.0) < 1e-9
    assert abs(res.final_positions["A"] - 50.0) < 1e-9


def test_reduce_then_hold_rearms_for_second_reduction():
    dates = _bdates(8)
    opens = pd.DataFrame({"A": [10.0] * 8}, index=dates)
    closes = pd.DataFrame({"A": [10.0] * 8}, index=dates)
    # HOLD (init) -> REDUCE (halve) -> HOLD (re-arm, no trade) -> REDUCE (halve again)
    sig = pd.DataFrame({"A": ["HOLD", "REDUCE", "REDUCE", "HOLD", "HOLD", "REDUCE", "REDUCE", "REDUCE"]},
                       index=dates)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=1000.0, holding_symbols=["A"],
        open_df=opens, close_df=closes, decisions=sig,
    )
    # Start 100 shares -> after first REDUCE 50 -> re-armed -> after second REDUCE 25.
    assert abs(res.final_positions["A"] - 25.0) < 1e-9
    sell_fills = [f for f in res.fills if f.side == "SELL"]
    assert len(sell_fills) == 2


def test_exposure_curve_reflects_exit():
    dates = _bdates(5)
    opens = pd.DataFrame({"A": [10.0] * 5}, index=dates)
    closes = pd.DataFrame({"A": [10.0] * 5}, index=dates)
    sig = pd.DataFrame({"A": ["HOLD", "EXIT", "EXIT", "EXIT", "EXIT"]}, index=dates)
    # No market_trend -> floor disabled.
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=1000.0, holding_symbols=["A"],
        open_df=opens, close_df=closes, decisions=sig,
    )
    # Day 0: fully invested. After EXIT executes on day 2: exposure drops to 0.
    assert res.exposure_curve.iloc[0] >= 0.99
    assert res.exposure_curve.iloc[-1] < 1e-9


def test_ranked_rearm_fires_in_rank_order_until_floor_met(monkeypatch):
    from portfolio_analyzer import config as cfg
    monkeypatch.setattr(cfg, "REARM_MAX_WEIGHT_PER_STOCK", 0.50)

    dates = _bdates(6)
    syms = ["A", "B", "C", "D"]
    opens = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    closes = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    # Keep C/D on REDUCE after day 1 so the state-machine HOLD->REDUCED rearm_full
    # path doesn't auto-flip them to FULL before the ranked-rearm check runs.
    sig = pd.DataFrame({
        "A": ["HOLD", "EXIT"] + ["HOLD"] * 4,
        "B": ["HOLD", "EXIT"] + ["HOLD"] * 4,
        "C": ["HOLD"] + ["REDUCE"] * 5,
        "D": ["HOLD"] + ["REDUCE"] * 5,
    }, index=dates)
    trend = pd.Series(["DOWNTREND", "DOWNTREND"] + ["UPTREND"] * 4, index=dates)
    ranks = pd.DataFrame({"A": 0.0, "B": 0.0, "C": 0.20, "D": 0.05}, index=dates)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=4000.0, holding_symbols=syms,
        open_df=opens, close_df=closes, decisions=sig,
        market_trend=trend, rank_df=ranks,
    )
    # Day 2 open: A=0 B=0 C=50 D=50, cash=3000, mv=1000, equity=4000 -> exposure 0.25.
    # Final exposure restored to floor (0.50) -> rearm stops after 2 names.
    assert list(res.rearm_history['symbol']) == ['C', 'D']
    assert abs(res.final_positions['C'] - 100.0) < 1e-9
    assert abs(res.final_positions['D'] - 100.0) < 1e-9
    rearm_fills = [f for f in res.fills if f.reason == 'REARM']
    assert [f.symbol for f in rearm_fills] == ['C', 'D']
    for rf in rearm_fills:
        assert abs(rf.shares - 50.0) < 1e-9
    assert res.exposure_curve.iloc[-1] + 1e-9 > 0.5


def test_ranked_rearm_respects_per_stock_weight_cap(monkeypatch):
    from portfolio_analyzer import config as cfg
    monkeypatch.setattr(cfg, 'REARM_MAX_WEIGHT_PER_STOCK', 0.20)

    dates = _bdates(6)
    syms = ['A', 'B', 'C', 'D']
    opens = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    closes = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    sig = pd.DataFrame({
        'A': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'B': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'C': ['HOLD'] + ['REDUCE'] * 5,
        'D': ['HOLD'] + ['REDUCE'] * 5,
    }, index=dates)
    trend = pd.Series(['DOWNTREND', 'DOWNTREND'] + ['UPTREND'] * 4, index=dates)
    ranks = pd.DataFrame({'A': 0.0, 'B': 0.0, 'C': 0.20, 'D': 0.05}, index=dates)

    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=4000.0, holding_symbols=syms,
        open_df=opens, close_df=closes, decisions=sig,
        market_trend=trend, rank_df=ranks,
    )
    # Cap = 20% of equity (Rs.800). Current REDUCED value per name = Rs.500.
    # Headroom per name = Rs.300 -> buy 30 sh each. Floor (0.50) never reached.
    assert list(res.rearm_history['symbol']) == ['C', 'D']
    assert abs(res.final_positions['C'] - 80.0) < 1e-9
    assert abs(res.final_positions['D'] - 80.0) < 1e-9
    rearm_fills = [f for f in res.fills if f.reason == 'REARM']
    for rf in rearm_fills:
        assert abs(rf.shares - 30.0) < 1e-9


def test_ranked_rearm_suppressed_in_downtrend(monkeypatch):
    from portfolio_analyzer import config as cfg
    monkeypatch.setattr(cfg, 'REARM_MAX_WEIGHT_PER_STOCK', 0.50)

    dates = _bdates(6)
    syms = ['A', 'B', 'C', 'D']
    opens = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    closes = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    sig = pd.DataFrame({
        'A': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'B': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'C': ['HOLD'] + ['REDUCE'] * 5,
        'D': ['HOLD'] + ['REDUCE'] * 5,
    }, index=dates)
    # Persistent DOWNTREND -> rearm must not fire even though exposure < floor.
    trend = pd.Series(['DOWNTREND'] * 6, index=dates)
    ranks = pd.DataFrame({'A': 0.0, 'B': 0.0, 'C': 0.20, 'D': 0.05}, index=dates)

    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=4000.0, holding_symbols=syms,
        open_df=opens, close_df=closes, decisions=sig,
        market_trend=trend, rank_df=ranks,
    )
    assert len(res.rearm_history) == 0
    assert abs(res.final_positions['C'] - 50.0) < 1e-9
    assert abs(res.final_positions['D'] - 50.0) < 1e-9



def test_refill_opens_fresh_entries_in_rs_order():
    # 4 NIFTY50 names; A, B, C are EXITed on day 1 (DOWNTREND), D stays held.
    # Day 2 switches to UPTREND so refill should fire. Ranks: C > B > A, all
    # eligible (price > 50/200 DMA).  REFILL_ALLOCATION_FRACTION=0.05 of
    # equity (~Rs.200) per entry; candidate pool is exhausted at 3 entries.
    dates = _bdates(6)
    syms = ['A', 'B', 'C', 'D']
    opens = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    closes = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    sig = pd.DataFrame({
        'A': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'B': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'C': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'D': ['HOLD'] * 6,
    }, index=dates)
    trend = pd.Series(['DOWNTREND', 'DOWNTREND'] + ['UPTREND'] * 4, index=dates)
    ranks = pd.DataFrame({'A': 0.10, 'B': 0.20, 'C': 0.30, 'D': 0.05}, index=dates)
    elig = pd.DataFrame(True, index=dates, columns=syms)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=4000.0, holding_symbols=syms,
        open_df=opens, close_df=closes, decisions=sig,
        market_trend=trend, rank_df=ranks, refill_eligible_df=elig,
    )
    # Day 2 post-EXIT: cash=3000, mv=1000, equity=4000, exposure 0.25.
    # Stop at exposure >= 0.55 (=Rs.2200 mv) but only 3 candidates available.
    # Per-entry = 0.05 * 4000 = Rs.200 -> 20 shares each.
    assert list(res.refill_history['symbol']) == ['C', 'B', 'A']
    assert all(abs(r - 200.0) < 1e-9 for r in res.refill_history['rupees'])
    refill_fills = [f for f in res.fills if f.reason == 'REFILL']
    assert [f.symbol for f in refill_fills] == ['C', 'B', 'A']
    for rf in refill_fills:
        assert abs(rf.shares - 20.0) < 1e-9
    # Final exposure: mv = 100*10 (D) + 20*10*3 = 1600 -> 0.40.
    assert abs(res.exposure_curve.iloc[-1] - 0.40) < 1e-6


def test_refill_excludes_held_and_low_rs():
    # A, B, C all EXIT on day 1; D stays held. A's RS is <= 0, so even though
    # it's a valid (non-held) candidate the RS > 0 gate rules it out -> the
    # refill plan is just [C, B] in RS order.
    dates = _bdates(6)
    syms = ['A', 'B', 'C', 'D']
    opens = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    closes = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    sig = pd.DataFrame({
        'A': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'B': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'C': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'D': ['HOLD'] * 6,
    }, index=dates)
    trend = pd.Series(['DOWNTREND', 'DOWNTREND'] + ['UPTREND'] * 4, index=dates)
    ranks = pd.DataFrame({'A': -0.05, 'B': 0.15, 'C': 0.25, 'D': 0.05}, index=dates)
    elig = pd.DataFrame(True, index=dates, columns=syms)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=4000.0, holding_symbols=syms,
        open_df=opens, close_df=closes, decisions=sig,
        market_trend=trend, rank_df=ranks, refill_eligible_df=elig,
    )
    # D is held; A is excluded by RS <= 0; only B and C refill.
    assert list(res.refill_history['symbol']) == ['C', 'B']


def test_refill_suppressed_in_downtrend():
    dates = _bdates(6)
    syms = ['A', 'B', 'C', 'D']
    opens = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    closes = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    sig = pd.DataFrame({
        'A': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'B': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'C': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'D': ['HOLD'] * 6,
    }, index=dates)
    trend = pd.Series(['DOWNTREND'] * 6, index=dates)
    ranks = pd.DataFrame({'A': 0.10, 'B': 0.20, 'C': 0.30, 'D': 0.05}, index=dates)
    elig = pd.DataFrame(True, index=dates, columns=syms)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=4000.0, holding_symbols=syms,
        open_df=opens, close_df=closes, decisions=sig,
        market_trend=trend, rank_df=ranks, refill_eligible_df=elig,
    )
    assert len(res.refill_history) == 0


def test_refill_honors_eligibility_gate():
    # Uptrend + idle cash, but only symbol B passes the 50/200 DMA gate.
    dates = _bdates(6)
    syms = ['A', 'B', 'C', 'D']
    opens = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    closes = pd.DataFrame({s: [10.0] * 6 for s in syms}, index=dates)
    sig = pd.DataFrame({
        'A': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'B': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'C': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'D': ['HOLD'] * 6,
    }, index=dates)
    trend = pd.Series(['DOWNTREND', 'DOWNTREND'] + ['UPTREND'] * 4, index=dates)
    ranks = pd.DataFrame({'A': 0.10, 'B': 0.20, 'C': 0.30, 'D': 0.05}, index=dates)
    elig = pd.DataFrame({'A': False, 'B': True, 'C': False, 'D': False}, index=dates)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=4000.0, holding_symbols=syms,
        open_df=opens, close_df=closes, decisions=sig,
        market_trend=trend, rank_df=ranks, refill_eligible_df=elig,
    )
    assert list(res.refill_history['symbol']) == ['B']



def _single_uptrend_trend(dates: pd.DatetimeIndex) -> pd.Series:
    """Trend series where refill fires on exactly one day (i=3, t_prev=dates[2])."""
    return pd.Series(
        ['DOWNTREND', 'DOWNTREND', 'UPTREND'] + ['DOWNTREND'] * (len(dates) - 3),
        index=dates,
    )


def test_refill_pulls_from_extended_candidate_pool(monkeypatch):
    # Core = [A]; candidates = [A, X, Y, Z] from NIFTY500. A is EXITed so cash
    # piles up; refill should pick top-RS non-held candidates (Y, Z, X in order).
    from portfolio_analyzer import config as cfg
    monkeypatch.setattr(cfg, 'REFILL_EXTERNAL_EXPOSURE_CAP', 1.0)

    dates = _bdates(6)
    core = ['A']
    pool = ['A', 'X', 'Y', 'Z']
    opens = pd.DataFrame({s: [10.0] * 6 for s in pool}, index=dates)
    closes = pd.DataFrame({s: [10.0] * 6 for s in pool}, index=dates)
    sig = pd.DataFrame({
        'A': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'X': ['HOLD'] * 6, 'Y': ['HOLD'] * 6, 'Z': ['HOLD'] * 6,
    }, index=dates)
    trend = _single_uptrend_trend(dates)
    ranks = pd.DataFrame({'A': 0.05, 'X': 0.10, 'Y': 0.30, 'Z': 0.20}, index=dates)
    # Exclude core A from refill eligibility to isolate non-core pathway.
    elig = pd.DataFrame({'A': False, 'X': True, 'Y': True, 'Z': True}, index=dates)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=10_000.0, holding_symbols=core,
        open_df=opens, close_df=closes, decisions=sig,
        market_trend=trend, rank_df=ranks, refill_eligible_df=elig,
        candidate_symbols=pool,
    )
    assert list(res.refill_history['symbol']) == ['Y', 'Z', 'X']


def test_refill_external_cap_truncates_plan_when_exceeded(monkeypatch):
    from portfolio_analyzer import config as cfg
    monkeypatch.setattr(cfg, 'REFILL_EXTERNAL_EXPOSURE_CAP', 0.10)
    monkeypatch.setattr(cfg, 'REFILL_ALLOCATION_FRACTION', 0.05)

    # Cap = 10% * Rs.10_000 = Rs.1_000. per_entry = 5% = Rs.500 -> only 2
    # non-core buys fit (Y, Z by RS); X gets shut out on the single uptrend day.
    dates = _bdates(6)
    core = ['A']
    pool = ['A', 'X', 'Y', 'Z']
    opens = pd.DataFrame({s: [10.0] * 6 for s in pool}, index=dates)
    closes = pd.DataFrame({s: [10.0] * 6 for s in pool}, index=dates)
    sig = pd.DataFrame({
        'A': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'X': ['HOLD'] * 6, 'Y': ['HOLD'] * 6, 'Z': ['HOLD'] * 6,
    }, index=dates)
    trend = _single_uptrend_trend(dates)
    ranks = pd.DataFrame({'A': 0.05, 'X': 0.10, 'Y': 0.30, 'Z': 0.20}, index=dates)
    elig = pd.DataFrame({'A': False, 'X': True, 'Y': True, 'Z': True}, index=dates)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=10_000.0, holding_symbols=core,
        open_df=opens, close_df=closes, decisions=sig,
        market_trend=trend, rank_df=ranks, refill_eligible_df=elig,
        candidate_symbols=pool,
    )
    assert list(res.refill_history['symbol']) == ['Y', 'Z']


def test_refill_external_cap_does_not_restrict_core_reentries(monkeypatch):
    # Core A is EXITed and eligible; external cap is zero -> non-core names
    # are locked out, but core A can still be refilled (cap applies only
    # to non-core deployment).
    from portfolio_analyzer import config as cfg
    monkeypatch.setattr(cfg, 'REFILL_EXTERNAL_EXPOSURE_CAP', 0.0)
    monkeypatch.setattr(cfg, 'REFILL_ALLOCATION_FRACTION', 0.05)

    dates = _bdates(6)
    core = ['A']
    pool = ['A', 'X', 'Y']
    opens = pd.DataFrame({s: [10.0] * 6 for s in pool}, index=dates)
    closes = pd.DataFrame({s: [10.0] * 6 for s in pool}, index=dates)
    sig = pd.DataFrame({
        'A': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'X': ['HOLD'] * 6, 'Y': ['HOLD'] * 6,
    }, index=dates)
    trend = _single_uptrend_trend(dates)
    ranks = pd.DataFrame({'A': 0.25, 'X': 0.10, 'Y': 0.30}, index=dates)
    elig = pd.DataFrame(True, index=dates, columns=pool)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=10_000.0, holding_symbols=core,
        open_df=opens, close_df=closes, decisions=sig,
        market_trend=trend, rank_df=ranks, refill_eligible_df=elig,
        candidate_symbols=pool,
    )
    assert list(res.refill_history['symbol']) == ['A']


def test_refill_top_k_truncates_candidate_list(monkeypatch):
    from portfolio_analyzer import config as cfg
    monkeypatch.setattr(cfg, 'REFILL_TOP_K', 2)
    monkeypatch.setattr(cfg, 'REFILL_ALLOCATION_FRACTION', 0.05)
    monkeypatch.setattr(cfg, 'REFILL_EXTERNAL_EXPOSURE_CAP', 1.0)

    # 4 eligible non-core candidates but top_k=2 -> only top 2 by RS picked.
    dates = _bdates(6)
    core = ['A']
    pool = ['A', 'W', 'X', 'Y', 'Z']
    opens = pd.DataFrame({s: [10.0] * 6 for s in pool}, index=dates)
    closes = pd.DataFrame({s: [10.0] * 6 for s in pool}, index=dates)
    sig = pd.DataFrame({
        'A': ['HOLD', 'EXIT'] + ['HOLD'] * 4,
        'W': ['HOLD'] * 6, 'X': ['HOLD'] * 6,
        'Y': ['HOLD'] * 6, 'Z': ['HOLD'] * 6,
    }, index=dates)
    trend = _single_uptrend_trend(dates)
    ranks = pd.DataFrame({
        'A': 0.05, 'W': 0.40, 'X': 0.10, 'Y': 0.30, 'Z': 0.20,
    }, index=dates)
    elig = pd.DataFrame({'A': False, 'W': True, 'X': True, 'Y': True, 'Z': True},
                        index=dates)
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=10_000.0, holding_symbols=core,
        open_df=opens, close_df=closes, decisions=sig,
        market_trend=trend, rank_df=ranks, refill_eligible_df=elig,
        candidate_symbols=pool,
    )
    assert list(res.refill_history['symbol']) == ['W', 'Y']



@pytest.mark.with_costs
def test_costs_and_slippage_are_wired_through_config(monkeypatch):
    """End-to-end: config BPS values flow into init BUY and produce a cost field."""
    monkeypatch.setattr(cfg, "TRANSACTION_COST_BPS", 0.001)
    monkeypatch.setattr(cfg, "SLIPPAGE_BPS", 0.00075)
    dates = _bdates(3)
    opens = pd.DataFrame({"A": [100.0] * 3}, index=dates)
    closes = pd.DataFrame({"A": [100.0] * 3}, index=dates)
    decisions = pd.DataFrame("HOLD", index=dates, columns=["A"])
    res = simulator.run_simulation(
        start_date=dates[0], end_date=dates[-1],
        initial_capital=10_000.0, holding_symbols=["A"],
        open_df=opens, close_df=closes, decisions=decisions,
    )
    init = next(f for f in res.fills if f.reason == "INIT")
    # Slippage widens exec price: 100 * (1 + 0.00075) = 100.075
    assert abs(init.price - 100.075) < 1e-9
    # Spend = 10000 / (1 + cost_bps); fee = spend * cost_bps
    expected_spend = 10_000.0 / 1.001
    assert abs(init.cost - expected_spend * 0.001) < 1e-6
    assert abs(init.shares - (expected_spend / 100.075)) < 1e-9
    # Flat prices + costs erode final equity below initial capital
    assert res.ending_equity < 10_000.0
