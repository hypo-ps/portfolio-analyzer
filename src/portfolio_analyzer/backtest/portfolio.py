from __future__ import annotations

import math
from dataclasses import dataclass, field

STATE_FULL = "FULL"
STATE_REDUCED = "REDUCED"
STATE_EXITED = "EXITED"


@dataclass
class Position:
    symbol: str
    shares: float
    avg_cost: float
    state: str = STATE_FULL


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    def market_value(self, prices: dict[str, float]) -> float:
        mv = 0.0
        for sym, pos in self.positions.items():
            px = prices.get(sym)
            if px is None or (isinstance(px, float) and math.isnan(px)):
                continue
            mv += pos.shares * px
        return mv

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def active_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.shares > 0 and p.state != STATE_EXITED]
