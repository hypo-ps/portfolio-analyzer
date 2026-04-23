from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import pandas as pd

from portfolio_analyzer import config as cfg
from portfolio_analyzer.backtest import broker
from portfolio_analyzer.backtest.portfolio import Portfolio, STATE_EXITED, STATE_FULL, STATE_REDUCED

log = logging.getLogger(__name__)


@dataclass
class SimResult:
    equity_curve: pd.Series            # date -> equity
    exposure_curve: pd.Series          # date -> invested_mv / equity
    fills: list[broker.Fill]
    decisions_history: pd.DataFrame    # (date, symbol, decision) only for applied transitions
    blocked_history: pd.DataFrame      # (date, symbol, decision, reason) for floor-blocked trades
    rearm_history: pd.DataFrame        # (date, symbol) for ranked re-arm upgrades (D-BT21)
    refill_history: pd.DataFrame       # (date, symbol, rupees) for opportunistic refills (D-BT22)
    defer_history: pd.DataFrame        # (date, symbol, event) EXIT-defer lifecycle (D-BT25)
    final_positions: dict[str, float]  # symbol -> shares
    starting_capital: float
    ending_equity: float


@dataclass
class _State:
    portfolio: Portfolio
    fills: list[broker.Fill] = field(default_factory=list)
    last_decision: dict[str, str] = field(default_factory=dict)


def _is_acute_breakdown(
    px: float, prev_close: float, ma200: float, dd: float, rs: float,
    dd_threshold: float, gap_pct: float,
) -> bool:
    """D-BT26: EXIT fires immediately (no defer) in two situations.

    1. Gap-down bypass: today's open is `gap_pct` below yesterday's close.
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


def _would_breach_floor(portfolio: Portfolio, prices: dict[str, float],
                        sym: str, new_dec: str, floor: float) -> bool:
    """Return True if executing this sell would drop invested/equity below floor."""
    pos = portfolio.positions.get(sym)
    if pos is None or pos.shares <= 0:
        return False
    px = prices.get(sym)
    if px is None or math.isnan(px) or px <= 0:
        return False
    mv = portfolio.market_value(prices)
    equity = portfolio.cash + mv
    if equity <= 0:
        return False
    delta_mv = pos.shares * px * (0.5 if new_dec == "REDUCE" else 1.0)
    new_invested = mv - delta_mv
    return (new_invested / equity) < floor


def _select_rearm_candidates(
    portfolio: Portfolio,
    prices: dict[str, float],
    ranks: dict[str, float],
    floor: float,
    max_weight: float,
) -> list[tuple[str, float]]:
    """D-BT21: pick (symbol, additional_rupees) pairs, highest-ranked first,
    sized to restore REDUCE'd share counts until exposure hits `floor`.

    Does not mutate `portfolio`; caller executes via broker to log fills.
    Simulates the progressive mv/cash changes so the stop-at-floor and
    cash/weight constraints apply deterministically in rank order.
    """
    mv = portfolio.market_value(prices)
    cash = portfolio.cash
    equity = cash + mv
    if equity <= 0 or math.isnan(equity) or (mv / equity) >= floor:
        return []

    ranked: list[tuple[float, str]] = []
    for sym, pos in portfolio.positions.items():
        if pos.state != STATE_REDUCED or pos.shares <= 0:
            continue
        px = prices.get(sym)
        if px is None or math.isnan(px) or px <= 0:
            continue
        rank = ranks.get(sym)
        if rank is None or math.isnan(rank):
            continue
        ranked.append((rank, sym))
    ranked.sort(key=lambda x: x[0], reverse=True)

    plan: list[tuple[str, float]] = []
    for _, sym in ranked:
        if cash <= 0 or equity <= 0 or (mv / equity) >= floor:
            break
        pos = portfolio.positions[sym]
        px = prices[sym]
        current_value = pos.shares * px
        headroom = max_weight * equity - current_value
        if headroom <= 0:
            continue
        additional = min(current_value, headroom, cash)
        if additional <= 0:
            continue
        plan.append((sym, additional))
        # Update tracking totals for next iteration (equity invariant under buy).
        mv += additional
        cash -= additional
    return plan


def _select_refill_candidates(
    portfolio: Portfolio,
    prices: dict[str, float],
    eligible: dict[str, bool],
    ranks: dict[str, float],
    candidate_symbols: list[str],
    core_set: set[str],
    stop_exposure: float,
    allocation_fraction: float,
    external_cap: float,
    top_k: int,
) -> list[tuple[str, float]]:
    """D-BT22/23: pick (symbol, rupees) fresh-entry buys, highest-RS first, to
    lift exposure toward `stop_exposure`.

    Candidate filter: eligible (price > 50DMA & 200DMA & dd >= -15%, precomputed),
    RS > 0, and NOT currently held (no Position or Position in EXITED state).
    Candidates are ranked by RS and truncated at `top_k`.

    External cap (D-BT23): for symbols not in `core_set` (non-core-portfolio names
    sourced from the broader NIFTY 500 universe), the combined market value of
    such positions may not exceed `external_cap * equity` after the buy plan.
    The cap does not restrict refills that re-enter names still in `core_set`.

    Each entry sizes at `allocation_fraction * equity`, cash-clipped. Does not
    mutate `portfolio`; caller executes via broker to log fills.
    """
    mv = portfolio.market_value(prices)
    cash = portfolio.cash
    equity = cash + mv
    if equity <= 0 or math.isnan(equity) or (mv / equity) >= stop_exposure:
        return []

    external_mv = 0.0
    for sym, pos in portfolio.positions.items():
        if sym in core_set or pos.shares <= 0:
            continue
        px = prices.get(sym)
        if px is None or math.isnan(px) or px <= 0:
            continue
        external_mv += pos.shares * px
    external_budget = external_cap * equity - external_mv

    ranked: list[tuple[float, str]] = []
    for sym in candidate_symbols:
        if not eligible.get(sym, False):
            continue
        rank = ranks.get(sym)
        if rank is None or math.isnan(rank) or rank <= 0:
            continue
        pos = portfolio.positions.get(sym)
        if pos is not None and pos.state != STATE_EXITED and pos.shares > 0:
            continue  # already held in a non-exited state
        px = prices.get(sym)
        if px is None or math.isnan(px) or px <= 0:
            continue
        ranked.append((rank, sym))
    ranked.sort(key=lambda x: x[0], reverse=True)
    if top_k > 0:
        ranked = ranked[:top_k]

    plan: list[tuple[str, float]] = []
    per_entry = allocation_fraction * equity
    for _, sym in ranked:
        if cash <= 0 or equity <= 0 or (mv / equity) >= stop_exposure:
            break
        spend = min(per_entry, cash)
        if sym not in core_set:
            if external_budget <= 0:
                continue
            spend = min(spend, external_budget)
        if spend <= 0:
            continue
        plan.append((sym, spend))
        mv += spend
        cash -= spend
        if sym not in core_set:
            external_budget -= spend
    return plan


def run_simulation(
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    initial_capital: float,
    holding_symbols: list[str],
    open_df: pd.DataFrame,
    close_df: pd.DataFrame,
    decisions: pd.DataFrame,
    market_trend: pd.Series | None = None,
    rank_df: pd.DataFrame | None = None,
    refill_eligible_df: pd.DataFrame | None = None,
    candidate_symbols: list[str] | None = None,
) -> SimResult:
    """Event-driven daily loop with soft exposure floor, ranked re-arm, and refill.

    Semantics (D-BT3, D-BT8, D-BT14, D-BT15, D-BT20, D-BT21, D-BT22, D-BT23):
      Day T close  -> decision from `decisions` row at T (state-machine output)
      Day T+1 open -> execute decisions; then, if trend != DOWNTREND:
                      (i)  ranked re-arm upgrades top REDUCED names (rank_df);
                      (ii) opportunistic refill opens fresh entries from
                           `candidate_symbols` (defaults to `holding_symbols`)
                           at `REFILL_ALLOCATION_FRACTION` of equity each,
                           filtered by `refill_eligible_df` and RS > 0, until
                           exposure >= REFILL_STOP_EXPOSURE. Non-core names
                           (candidate_symbols - holding_symbols) are capped by
                           `REFILL_EXTERNAL_EXPOSURE_CAP` in aggregate (D-BT23).
      Day T+1 close -> mark-to-market for equity curve.

    `holding_symbols` is the core portfolio seeded at INIT (NIFTY 50 by default).
    `candidate_symbols` is the broader pool the decision state-machine and refill
    selector iterate over; when None it collapses to `holding_symbols` so the
    broader-universe paths stay dormant (backward-compatible with earlier tests).
    """
    open_df = open_df.sort_index()
    close_df = close_df.sort_index()
    decisions = decisions.sort_index()
    if market_trend is not None:
        market_trend = market_trend.sort_index()
    if rank_df is not None:
        rank_df = rank_df.sort_index()
    if refill_eligible_df is not None:
        refill_eligible_df = refill_eligible_df.sort_index()

    mask = (open_df.index >= start_date) & (open_df.index <= end_date)
    dates = open_df.index[mask]
    if len(dates) < 2:
        raise RuntimeError(f"Need >=2 trading days in [{start_date}, {end_date}]; got {len(dates)}")

    state = _State(portfolio=Portfolio(cash=float(initial_capital)))
    start_open_row = open_df.loc[dates[0]]
    per_stock_rupees = initial_capital / max(len(holding_symbols), 1)
    initial_allocation: dict[str, float] = {}
    for sym in holding_symbols:
        px = float(start_open_row.get(sym, float("nan")))
        if math.isnan(px) or px <= 0:
            log.warning("init: no open price for %s on %s; skipped", sym, dates[0].date())
            continue
        fill = broker.buy(state.portfolio, str(dates[0].date()), sym, px,
                          per_stock_rupees, reason="INIT",
                          slippage_bps=cfg.SLIPPAGE_BPS,
                          cost_bps=cfg.TRANSACTION_COST_BPS)
        if fill is not None:
            state.fills.append(fill)
            state.last_decision[sym] = "HOLD"
            initial_allocation[sym] = per_stock_rupees

    equity_curve: dict[pd.Timestamp, float] = {}
    exposure_curve: dict[pd.Timestamp, float] = {}
    close_row0 = close_df.loc[dates[0]].to_dict()
    eq0 = state.portfolio.equity(close_row0)
    equity_curve[dates[0]] = eq0
    exposure_curve[dates[0]] = (state.portfolio.market_value(close_row0) / eq0) if eq0 > 0 else 0.0

    decisions_log: list[tuple[pd.Timestamp, str, str]] = []
    blocked_log: list[tuple[pd.Timestamp, str, str, str]] = []
    rearm_log: list[tuple[pd.Timestamp, str]] = []
    refill_log: list[tuple[pd.Timestamp, str, float]] = []
    defer_log: list[tuple[pd.Timestamp, str, str]] = []
    floor = cfg.EXPOSURE_FLOOR
    reentry_fraction = cfg.REENTRY_ALLOCATION_FRACTION
    rearm_max_weight = cfg.REARM_MAX_WEIGHT_PER_STOCK
    refill_stop = cfg.REFILL_STOP_EXPOSURE
    refill_fraction = cfg.REFILL_ALLOCATION_FRACTION
    refill_external_cap = cfg.REFILL_EXTERNAL_EXPOSURE_CAP
    refill_top_k = cfg.REFILL_TOP_K
    defer_days = int(cfg.EXIT_DEFER_DAYS)
    defer_dd_threshold = float(cfg.EXIT_DEFER_DD_THRESHOLD)
    defer_gap_pct = float(cfg.EXIT_DEFER_GAP_DOWN_PCT)
    fee_kw = {"slippage_bps": cfg.SLIPPAGE_BPS, "cost_bps": cfg.TRANSACTION_COST_BPS}
    core_set = set(holding_symbols)
    if candidate_symbols is None:
        candidate_symbols = list(holding_symbols)
    decision_iter = candidate_symbols
    # D-BT26: ma200 + drawdown frames for triple-gate acute-breakdown test. Both
    # are derived from close_df to match phase0_strategy's rolling metrics.
    if defer_days > 0:
        ma200_df = close_df.rolling(cfg.MA_LONG).mean()
        high52_df = close_df.rolling(cfg.HIGH_52W_WINDOW).max()
        drawdown_df = close_df / high52_df - 1.0
    else:
        ma200_df = None
        drawdown_df = None
    pending_exits: dict[str, int] = {}

    for i in range(1, len(dates)):
        t_prev = dates[i - 1]
        t_now = dates[i]
        open_row = open_df.loc[t_now].to_dict()
        dec_row = decisions.loc[t_prev] if t_prev in decisions.index else None
        trend = (market_trend.loc[t_prev] if market_trend is not None
                 and t_prev in market_trend.index else None)
        floor_active = trend is not None and trend != "DOWNTREND"  # D-BT20: UPTREND + SIDEWAYS

        # D-BT26: resolve deferred EXITs BEFORE processing today's decisions.
        # Triple-gate acute (price<ma200 AND dd<-10% AND rs<0) OR gap-down>3%
        # fires immediately; otherwise the timer decrements. A non-EXIT matrix
        # signal cancels the deferral.
        cancelled_today: set[str] = set()
        if pending_exits and ma200_df is not None:
            ma200_row = ma200_df.loc[t_prev] if t_prev in ma200_df.index else None
            dd_row = drawdown_df.loc[t_prev] if drawdown_df is not None and t_prev in drawdown_df.index else None
            rs_row = rank_df.loc[t_prev] if rank_df is not None and t_prev in rank_df.index else None
            close_prev = close_df.loc[t_prev] if t_prev in close_df.index else None
            to_drop: list[str] = []
            for sym, remaining in list(pending_exits.items()):
                matrix_dec = dec_row.get(sym) if dec_row is not None else None
                if isinstance(matrix_dec, str) and matrix_dec != "EXIT":
                    defer_log.append((t_prev, sym, "cancel_upgrade"))
                    to_drop.append(sym)
                    cancelled_today.add(sym)
                    continue
                px = float(open_row.get(sym, float("nan")))
                if math.isnan(px) or px <= 0:
                    continue  # keep pending, try again tomorrow
                ma200 = float(ma200_row.get(sym, float("nan"))) if ma200_row is not None else float("nan")
                dd = float(dd_row.get(sym, float("nan"))) if dd_row is not None else float("nan")
                rs = float(rs_row.get(sym, float("nan"))) if rs_row is not None else float("nan")
                prev_close = float(close_prev.get(sym, float("nan"))) if close_prev is not None else float("nan")
                acute = _is_acute_breakdown(px, prev_close, ma200, dd, rs,
                                            defer_dd_threshold, defer_gap_pct)
                remaining -= 1
                if not acute and remaining > 0:
                    pending_exits[sym] = remaining
                    defer_log.append((t_prev, sym, "decrement"))
                    continue
                # Fire the SELL now (timer expired or acute breakdown).
                if floor_active and _would_breach_floor(
                    state.portfolio, open_row, sym, "EXIT", floor
                ):
                    blocked_log.append((t_prev, sym, "EXIT", "exposure_floor"))
                    defer_log.append((t_prev, sym, "cancel_floor"))
                    to_drop.append(sym)
                    continue
                reason = "EXIT_ACUTE" if acute else "EXIT_DEFERRED"
                fill = broker.exit_position(state.portfolio, str(t_now.date()), sym, px,
                                            reason=reason, **fee_kw)
                if fill is not None:
                    state.fills.append(fill)
                state.last_decision[sym] = "EXIT"
                decisions_log.append((t_prev, sym, "EXIT"))
                defer_log.append((t_prev, sym, "fire_acute" if acute else "fire_expired"))
                to_drop.append(sym)
            for sym in to_drop:
                pending_exits.pop(sym, None)

        if dec_row is not None:
            for sym in decision_iter:
                if sym in pending_exits or sym in cancelled_today:
                    continue  # deferred EXIT in flight, or just cancelled today
                new_dec = dec_row.get(sym)
                if not isinstance(new_dec, str):
                    continue
                prev_dec = state.last_decision.get(sym, "HOLD")
                if new_dec == prev_dec:
                    continue
                px = float(open_row.get(sym, float("nan")))
                if new_dec in ("REDUCE", "EXIT") and floor_active and _would_breach_floor(
                    state.portfolio, open_row, sym, new_dec, floor
                ):
                    blocked_log.append((t_prev, sym, new_dec, "exposure_floor"))
                    continue  # state does NOT advance; re-attempt next day
                if math.isnan(px) or px <= 0:
                    continue
                fill: broker.Fill | None = None
                if new_dec == "REDUCE" and prev_dec == "EXIT":
                    target_rupees = initial_allocation.get(sym, 0.0) * reentry_fraction
                    fill = broker.reenter(
                        state.portfolio, str(t_now.date()), sym, px, target_rupees,
                        reason="REENTRY", **fee_kw,
                    )
                    if fill is None:
                        continue  # no cash or other block -> do not advance state
                elif new_dec == "REDUCE":
                    fill = broker.reduce_half(state.portfolio, str(t_now.date()), sym, px,
                                              reason="REDUCE", **fee_kw)
                elif new_dec == "EXIT":
                    exit_reason = "EXIT"
                    if defer_days > 0 and ma200_df is not None:
                        ma200 = float(ma200_df.at[t_prev, sym]) if (
                            t_prev in ma200_df.index and sym in ma200_df.columns
                        ) else float("nan")
                        dd = float(drawdown_df.at[t_prev, sym]) if (
                            drawdown_df is not None and t_prev in drawdown_df.index
                            and sym in drawdown_df.columns
                        ) else float("nan")
                        rs = float(rank_df.at[t_prev, sym]) if (
                            rank_df is not None and t_prev in rank_df.index
                            and sym in rank_df.columns
                        ) else float("nan")
                        prev_close = float(close_df.at[t_prev, sym]) if (
                            t_prev in close_df.index and sym in close_df.columns
                        ) else float("nan")
                        acute_now = _is_acute_breakdown(
                            px, prev_close, ma200, dd, rs,
                            defer_dd_threshold, defer_gap_pct,
                        )
                        if not acute_now:
                            pending_exits[sym] = defer_days
                            defer_log.append((t_prev, sym, "enqueue"))
                            continue  # no state advance until timer resolves
                        exit_reason = "EXIT_ACUTE"
                        defer_log.append((t_prev, sym, "fire_acute"))
                    fill = broker.exit_position(state.portfolio, str(t_now.date()), sym, px,
                                                reason=exit_reason, **fee_kw)
                elif new_dec == "HOLD" and prev_dec == "REDUCE":
                    pos = state.portfolio.positions.get(sym)
                    if pos is not None and pos.state == STATE_REDUCED:
                        broker.rearm_full(state.portfolio, sym)
                state.last_decision[sym] = new_dec
                decisions_log.append((t_prev, sym, new_dec))
                if fill is not None:
                    state.fills.append(fill)

        # D-BT21 ranked re-arm: after decisions, if exposure still below floor
        # and we're not in DOWNTREND, actively upgrade top-ranked REDUCED names.
        if (rank_df is not None and trend is not None and trend != "DOWNTREND"
                and t_prev in rank_df.index):
            ranks_row = rank_df.loc[t_prev].to_dict()
            plan = _select_rearm_candidates(
                state.portfolio, open_row, ranks_row, floor, rearm_max_weight,
            )
            for sym, rupees in plan:
                px = float(open_row[sym])
                buy_fill = broker.buy(
                    state.portfolio, str(t_now.date()), sym, px, rupees,
                    reason="REARM", **fee_kw,
                )
                if buy_fill is None:
                    continue
                broker.rearm_full(state.portfolio, sym)
                state.fills.append(buy_fill)
                state.last_decision[sym] = "HOLD"
                rearm_log.append((t_prev, sym))

        # D-BT22 opportunistic refill: if exposure still short of the stop target
        # and trend != DOWNTREND, open fresh NIFTY50 entries (price > 50DMA &
        # 200DMA & RS > 0) at REFILL_ALLOCATION_FRACTION of equity each.
        if (rank_df is not None and refill_eligible_df is not None
                and trend is not None and trend != "DOWNTREND"
                and t_prev in rank_df.index and t_prev in refill_eligible_df.index):
            ranks_row = rank_df.loc[t_prev].to_dict()
            elig_row = refill_eligible_df.loc[t_prev].to_dict()
            plan = _select_refill_candidates(
                state.portfolio, open_row, elig_row, ranks_row,
                candidate_symbols, core_set, refill_stop, refill_fraction,
                refill_external_cap, refill_top_k,
            )
            for sym, rupees in plan:
                px = float(open_row[sym])
                buy_fill = broker.buy(
                    state.portfolio, str(t_now.date()), sym, px, rupees,
                    reason="REFILL", **fee_kw,
                )
                if buy_fill is None:
                    continue
                # Fresh entry: ensure a prior EXITED position is flipped to FULL so
                # subsequent hysteresis transitions work normally.
                pos = state.portfolio.positions.get(sym)
                if pos is not None:
                    pos.state = STATE_FULL
                state.fills.append(buy_fill)
                state.last_decision[sym] = "HOLD"
                refill_log.append((t_prev, sym, float(buy_fill.shares * buy_fill.price)))

        close_row = close_df.loc[t_now].to_dict()
        eq = state.portfolio.equity(close_row)
        equity_curve[t_now] = eq
        exposure_curve[t_now] = (state.portfolio.market_value(close_row) / eq) if eq > 0 else 0.0

    eq_series = pd.Series(equity_curve).sort_index()
    exp_series = pd.Series(exposure_curve).sort_index()
    hist = pd.DataFrame(decisions_log, columns=["date", "symbol", "decision"]) if decisions_log \
        else pd.DataFrame(columns=["date", "symbol", "decision"])
    blocked = pd.DataFrame(blocked_log, columns=["date", "symbol", "decision", "reason"]) if blocked_log \
        else pd.DataFrame(columns=["date", "symbol", "decision", "reason"])
    rearms = pd.DataFrame(rearm_log, columns=["date", "symbol"]) if rearm_log \
        else pd.DataFrame(columns=["date", "symbol"])
    refills = pd.DataFrame(refill_log, columns=["date", "symbol", "rupees"]) if refill_log \
        else pd.DataFrame(columns=["date", "symbol", "rupees"])
    defers = pd.DataFrame(defer_log, columns=["date", "symbol", "event"]) if defer_log \
        else pd.DataFrame(columns=["date", "symbol", "event"])

    return SimResult(
        equity_curve=eq_series,
        exposure_curve=exp_series,
        fills=state.fills,
        decisions_history=hist,
        blocked_history=blocked,
        rearm_history=rearms,
        refill_history=refills,
        defer_history=defers,
        final_positions={s: p.shares for s, p in state.portfolio.positions.items()},
        starting_capital=float(initial_capital),
        ending_equity=float(eq_series.iloc[-1]),
    )
