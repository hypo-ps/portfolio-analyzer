from __future__ import annotations

from portfolio_analyzer.backtest import broker
from portfolio_analyzer.backtest.portfolio import (
    Portfolio,
    STATE_EXITED,
    STATE_FULL,
    STATE_REDUCED,
)


def _fresh_portfolio(cash: float = 100_000.0) -> Portfolio:
    return Portfolio(cash=cash)


def test_buy_increases_position_and_decreases_cash():
    p = _fresh_portfolio(100_000)
    fill = broker.buy(p, "2024-01-02", "INFY", price=1500.0, rupees=30_000.0)
    assert fill is not None and fill.side == "BUY"
    pos = p.positions["INFY"]
    assert abs(pos.shares - 20.0) < 1e-9
    assert abs(p.cash - 70_000.0) < 1e-9
    assert pos.state == STATE_FULL
    assert pos.avg_cost == 1500.0


def test_buy_clips_to_available_cash():
    p = _fresh_portfolio(1000.0)
    fill = broker.buy(p, "2024-01-02", "INFY", price=100.0, rupees=5000.0)
    assert fill is not None
    assert abs(p.cash) < 1e-6
    assert abs(p.positions["INFY"].shares - 10.0) < 1e-9


def test_reduce_half_sells_half_and_flips_state():
    p = _fresh_portfolio(100_000)
    broker.buy(p, "2024-01-02", "INFY", 1000.0, 50_000.0)
    fill = broker.reduce_half(p, "2024-02-01", "INFY", price=1100.0)
    assert fill is not None and fill.side == "SELL"
    pos = p.positions["INFY"]
    assert abs(pos.shares - 25.0) < 1e-9
    assert pos.state == STATE_REDUCED
    # cash == 50,000 (initial leftover) + 25 * 1100
    assert abs(p.cash - (50_000.0 + 25 * 1100.0)) < 1e-6


def test_reduce_is_noop_if_already_reduced():
    p = _fresh_portfolio(100_000)
    broker.buy(p, "2024-01-02", "INFY", 1000.0, 50_000.0)
    broker.reduce_half(p, "2024-02-01", "INFY", 1100.0)
    shares_after_first = p.positions["INFY"].shares
    cash_after_first = p.cash
    fill = broker.reduce_half(p, "2024-03-01", "INFY", 1200.0)
    assert fill is None
    assert p.positions["INFY"].shares == shares_after_first
    assert p.cash == cash_after_first


def test_exit_sells_remainder_and_terminates():
    p = _fresh_portfolio(100_000)
    broker.buy(p, "2024-01-02", "INFY", 1000.0, 50_000.0)
    broker.reduce_half(p, "2024-02-01", "INFY", 1100.0)
    fill = broker.exit_position(p, "2024-03-01", "INFY", 900.0)
    assert fill is not None
    pos = p.positions["INFY"]
    assert pos.state == STATE_EXITED
    assert pos.shares == 0.0
    # Second exit is a no-op
    assert broker.exit_position(p, "2024-04-01", "INFY", 800.0) is None


def test_exit_from_full_skips_reduce_step():
    p = _fresh_portfolio(100_000)
    broker.buy(p, "2024-01-02", "INFY", 1000.0, 50_000.0)
    fill = broker.exit_position(p, "2024-02-01", "INFY", 1100.0)
    assert fill is not None
    pos = p.positions["INFY"]
    assert pos.state == STATE_EXITED
    assert pos.shares == 0.0


def test_market_value_only_counts_active_positions():
    p = _fresh_portfolio(100_000)
    broker.buy(p, "2024-01-02", "INFY", 1000.0, 50_000.0)
    broker.exit_position(p, "2024-02-01", "INFY", 1100.0)
    assert p.market_value({"INFY": 1200.0}) == 0.0
    assert abs(p.equity({"INFY": 1200.0}) - p.cash) < 1e-6


def test_cash_conservation_on_buy_then_full_exit_at_same_price():
    p = _fresh_portfolio(100_000)
    broker.buy(p, "2024-01-02", "INFY", 1000.0, 40_000.0)
    broker.exit_position(p, "2024-02-01", "INFY", 1000.0)
    assert abs(p.cash - 100_000.0) < 1e-6


def test_rearm_full_flips_state_without_buying():
    p = _fresh_portfolio(100_000)
    broker.buy(p, "2024-01-02", "INFY", 1000.0, 50_000.0)
    broker.reduce_half(p, "2024-02-01", "INFY", 1100.0)
    cash_before = p.cash
    shares_before = p.positions["INFY"].shares
    assert broker.rearm_full(p, "INFY") is True
    assert p.positions["INFY"].state == STATE_FULL
    assert p.positions["INFY"].shares == shares_before
    assert p.cash == cash_before


def test_rearm_full_is_noop_for_full_or_exited():
    p = _fresh_portfolio(100_000)
    broker.buy(p, "2024-01-02", "INFY", 1000.0, 50_000.0)
    assert broker.rearm_full(p, "INFY") is False  # already FULL
    broker.exit_position(p, "2024-02-01", "INFY", 1100.0)
    assert broker.rearm_full(p, "INFY") is False  # EXITED is terminal
    assert p.positions["INFY"].state == STATE_EXITED


def test_rearm_then_reduce_again_halves_further():
    p = _fresh_portfolio(100_000)
    broker.buy(p, "2024-01-02", "INFY", 1000.0, 40_000.0)  # 40 shares
    broker.reduce_half(p, "2024-02-01", "INFY", 1000.0)    # -> 20 shares, REDUCED
    broker.rearm_full(p, "INFY")                           # state back to FULL
    broker.reduce_half(p, "2024-03-01", "INFY", 1000.0)    # -> 10 shares, REDUCED
    assert abs(p.positions["INFY"].shares - 10.0) < 1e-9
    assert p.positions["INFY"].state == STATE_REDUCED


def test_reenter_rebuilds_exited_position_at_reduced_state():
    p = _fresh_portfolio(100_000)
    broker.buy(p, "2024-01-02", "INFY", 1000.0, 40_000.0)   # 40 sh @ 1000
    broker.exit_position(p, "2024-02-01", "INFY", 1100.0)   # cash=100k+4k=104k
    cash_before = p.cash
    fill = broker.reenter(p, "2024-03-01", "INFY", 1050.0, 20_000.0)
    assert fill is not None and fill.side == "BUY" and fill.reason == "REENTRY"
    pos = p.positions["INFY"]
    assert pos.state == STATE_REDUCED
    assert abs(pos.shares - (20_000.0 / 1050.0)) < 1e-9
    assert abs(p.cash - (cash_before - 20_000.0)) < 1e-6
    assert pos.avg_cost == 1050.0


def test_reenter_is_noop_when_position_not_exited():
    p = _fresh_portfolio(100_000)
    broker.buy(p, "2024-01-02", "INFY", 1000.0, 40_000.0)
    assert broker.reenter(p, "2024-03-01", "INFY", 1050.0, 20_000.0) is None
    broker.reduce_half(p, "2024-02-01", "INFY", 1100.0)
    assert broker.reenter(p, "2024-03-01", "INFY", 1050.0, 20_000.0) is None


def test_reenter_is_noop_for_unknown_symbol():
    p = _fresh_portfolio(100_000)
    assert broker.reenter(p, "2024-03-01", "NEWCO", 1050.0, 20_000.0) is None


def test_reenter_clips_to_available_cash():
    p = _fresh_portfolio(50_000)
    broker.buy(p, "2024-01-02", "INFY", 1000.0, 40_000.0)   # cash=10k
    broker.exit_position(p, "2024-02-01", "INFY", 1100.0)   # cash=10k+44k=54k
    fill = broker.reenter(p, "2024-03-01", "INFY", 1000.0, 100_000.0)  # ask > cash
    assert fill is not None
    assert abs(p.cash) < 1e-6  # all cash consumed
    assert abs(fill.shares - 54.0) < 1e-9
