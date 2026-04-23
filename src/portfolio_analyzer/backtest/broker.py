from __future__ import annotations

import math
from dataclasses import dataclass

from portfolio_analyzer.backtest.portfolio import (
    Portfolio,
    Position,
    STATE_EXITED,
    STATE_FULL,
    STATE_REDUCED,
)


@dataclass
class Fill:
    date: str
    symbol: str
    side: str  # "BUY" | "SELL"
    shares: float
    price: float
    reason: str


def buy(portfolio: Portfolio, date: str, symbol: str, price: float,
        rupees: float, reason: str = "INIT") -> Fill | None:
    """Buy as many (fractional) shares as `rupees` can afford at `price`."""
    if price <= 0 or rupees <= 0 or math.isnan(price):
        return None
    shares = rupees / price
    if shares <= 0:
        return None
    if rupees > portfolio.cash + 1e-6:
        rupees = portfolio.cash
        shares = rupees / price
        if shares <= 0:
            return None
    portfolio.cash -= shares * price
    existing = portfolio.positions.get(symbol)
    if existing is None:
        portfolio.positions[symbol] = Position(
            symbol=symbol, shares=shares, avg_cost=price, state=STATE_FULL
        )
    else:
        new_shares = existing.shares + shares
        existing.avg_cost = (
            (existing.avg_cost * existing.shares + price * shares) / new_shares
        )
        existing.shares = new_shares
    return Fill(date=date, symbol=symbol, side="BUY", shares=shares, price=price, reason=reason)


def _sell_fraction(portfolio: Portfolio, date: str, symbol: str, price: float,
                   fraction: float, new_state: str, reason: str) -> Fill | None:
    if price <= 0 or math.isnan(price):
        return None
    pos = portfolio.positions.get(symbol)
    if pos is None or pos.shares <= 0 or pos.state == STATE_EXITED:
        return None
    shares = pos.shares * fraction
    if shares <= 0:
        return None
    portfolio.cash += shares * price
    pos.shares -= shares
    pos.state = new_state
    if new_state == STATE_EXITED:
        pos.shares = 0.0
    return Fill(date=date, symbol=symbol, side="SELL", shares=shares, price=price, reason=reason)


def reduce_half(portfolio: Portfolio, date: str, symbol: str, price: float,
                reason: str = "REDUCE") -> Fill | None:
    """Sell 50% of a FULL position; no-op if already REDUCED or EXITED."""
    pos = portfolio.positions.get(symbol)
    if pos is None or pos.state != STATE_FULL:
        return None
    return _sell_fraction(portfolio, date, symbol, price, 0.5, STATE_REDUCED, reason)


def exit_position(portfolio: Portfolio, date: str, symbol: str, price: float,
                  reason: str = "EXIT") -> Fill | None:
    """Sell entire remaining position; no-op if already EXITED."""
    pos = portfolio.positions.get(symbol)
    if pos is None or pos.state == STATE_EXITED or pos.shares <= 0:
        return None
    return _sell_fraction(portfolio, date, symbol, price, 1.0, STATE_EXITED, reason)


def rearm_full(portfolio: Portfolio, symbol: str) -> bool:
    """Reset a REDUCED position back to FULL without buying (D-BT14 upgrade).

    Used when the strategy upgrades REDUCE -> HOLD on recovery. The position's
    share count is unchanged; only the broker state flips so a subsequent REDUCE
    transition can halve the remaining shares again. No-op for FULL/EXITED.
    """
    pos = portfolio.positions.get(symbol)
    if pos is None or pos.state != STATE_REDUCED or pos.shares <= 0:
        return False
    pos.state = STATE_FULL
    return True


def reenter(portfolio: Portfolio, date: str, symbol: str, price: float,
            rupees: float, reason: str = "REENTRY") -> Fill | None:
    """Re-enter a previously EXITED position at REDUCED state (D-BT19).

    Buys shares worth up to `rupees` (cash-clipped) and leaves the position
    in REDUCED state so a subsequent HOLD upgrade re-arms it to FULL normally.
    No-op unless the symbol currently exists and is EXITED.
    """
    if price <= 0 or rupees <= 0 or math.isnan(price):
        return None
    pos = portfolio.positions.get(symbol)
    if pos is None or pos.state != STATE_EXITED:
        return None
    spend = min(rupees, portfolio.cash)
    if spend <= 0:
        return None
    shares = spend / price
    if shares <= 0:
        return None
    portfolio.cash -= shares * price
    pos.shares = shares
    pos.avg_cost = price
    pos.state = STATE_REDUCED
    return Fill(date=date, symbol=symbol, side="BUY", shares=shares, price=price, reason=reason)
