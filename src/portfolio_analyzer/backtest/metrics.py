from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252
EXIT_LOOKAHEAD_DAYS = 21


@dataclass
class PerfStats:
    cagr: float
    max_drawdown: float
    sharpe: float
    volatility_annual: float
    total_return: float
    days: int


def perf_stats(equity: pd.Series) -> PerfStats:
    if equity.empty or len(equity) < 2:
        return PerfStats(0.0, 0.0, 0.0, 0.0, 0.0, len(equity))
    eq = equity.astype(float)
    total_return = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    days = len(eq)
    years = days / TRADING_DAYS_PER_YEAR
    cagr = float((eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0) if years > 0 else 0.0
    rolling_max = eq.cummax()
    drawdown = eq / rolling_max - 1.0
    max_dd = float(drawdown.min())
    daily_ret = eq.pct_change().dropna()
    vol = float(daily_ret.std() * math.sqrt(TRADING_DAYS_PER_YEAR)) if len(daily_ret) > 1 else 0.0
    mean_daily = float(daily_ret.mean()) if len(daily_ret) else 0.0
    sharpe = (mean_daily * TRADING_DAYS_PER_YEAR) / vol if vol > 0 else 0.0
    return PerfStats(
        cagr=cagr, max_drawdown=max_dd, sharpe=sharpe,
        volatility_annual=vol, total_return=total_return, days=days,
    )


@dataclass
class ExitDiagnostics:
    num_exits: int
    num_reduces: int
    avg_forward_return_21d: float
    exit_quality_rate: float  # fraction of EXITs whose 21d fwd return is negative (D-BT16)


def exit_diagnostics(
    decisions_history: pd.DataFrame,
    open_df: pd.DataFrame,
    close_df: pd.DataFrame,
    lookahead_days: int = EXIT_LOOKAHEAD_DAYS,
) -> ExitDiagnostics:
    """For every EXIT signal, measure the T+1 Open -> T+lookahead Close return."""
    if decisions_history.empty:
        return ExitDiagnostics(0, 0, 0.0, 0.0)
    exits = decisions_history[decisions_history["decision"] == "EXIT"]
    reduces = decisions_history[decisions_history["decision"] == "REDUCE"]
    fwd_returns: list[float] = []
    idx = open_df.index
    for _, row in exits.iterrows():
        signal_date = pd.Timestamp(row["date"])
        sym = row["symbol"]
        if sym not in open_df.columns:
            continue
        pos = idx.searchsorted(signal_date)
        entry_pos = pos + 1
        exit_pos = entry_pos + lookahead_days
        if entry_pos >= len(idx) or exit_pos >= len(idx):
            continue
        entry_px = open_df.iloc[entry_pos].get(sym)
        exit_px = close_df.iloc[exit_pos].get(sym)
        if entry_px is None or exit_px is None:
            continue
        if np.isnan(entry_px) or np.isnan(exit_px) or entry_px <= 0:
            continue
        fwd_returns.append(float(exit_px / entry_px - 1.0))
    if not fwd_returns:
        return ExitDiagnostics(len(exits), len(reduces), 0.0, 0.0)
    arr = np.array(fwd_returns)
    avg = float(arr.mean())
    hit = float((arr < 0).mean())  # exit quality: fraction where stock fell after EXIT
    return ExitDiagnostics(
        num_exits=len(exits),
        num_reduces=len(reduces),
        avg_forward_return_21d=avg,
        exit_quality_rate=hit,
    )


def avg_exposure(exposure_curve: pd.Series) -> float:
    """Mean of daily invested_mv / equity across the backtest window (D-BT16)."""
    if exposure_curve is None or exposure_curve.empty:
        return 0.0
    return float(exposure_curve.astype(float).mean())


@dataclass
class RearmDiagnostics:
    num_rearms: int
    avg_forward_return_21d: float


def rearm_diagnostics(
    rearm_history: pd.DataFrame,
    open_df: pd.DataFrame,
    close_df: pd.DataFrame,
    lookahead_days: int = EXIT_LOOKAHEAD_DAYS,
) -> RearmDiagnostics:
    """Average T+1 Open -> T+lookahead Close return across ranked re-arm upgrades (D-BT21)."""
    if rearm_history is None or rearm_history.empty:
        return RearmDiagnostics(0, 0.0)
    fwd_returns: list[float] = []
    idx = open_df.index
    for _, row in rearm_history.iterrows():
        signal_date = pd.Timestamp(row["date"])
        sym = row["symbol"]
        if sym not in open_df.columns:
            continue
        pos = idx.searchsorted(signal_date)
        entry_pos = pos + 1
        exit_pos = entry_pos + lookahead_days
        if entry_pos >= len(idx) or exit_pos >= len(idx):
            continue
        entry_px = open_df.iloc[entry_pos].get(sym)
        exit_px = close_df.iloc[exit_pos].get(sym)
        if entry_px is None or exit_px is None:
            continue
        if np.isnan(entry_px) or np.isnan(exit_px) or entry_px <= 0:
            continue
        fwd_returns.append(float(exit_px / entry_px - 1.0))
    if not fwd_returns:
        return RearmDiagnostics(len(rearm_history), 0.0)
    return RearmDiagnostics(
        num_rearms=len(rearm_history),
        avg_forward_return_21d=float(np.array(fwd_returns).mean()),
    )


@dataclass
class RefillDiagnostics:
    num_refills: int
    total_rupees_deployed: float
    avg_forward_return_21d: float


def refill_diagnostics(
    refill_history: pd.DataFrame,
    open_df: pd.DataFrame,
    close_df: pd.DataFrame,
    lookahead_days: int = EXIT_LOOKAHEAD_DAYS,
) -> RefillDiagnostics:
    """Average T+1 Open -> T+lookahead Close return across opportunistic refills (D-BT22)."""
    if refill_history is None or refill_history.empty:
        return RefillDiagnostics(0, 0.0, 0.0)
    total_rupees = float(refill_history["rupees"].astype(float).sum()) if "rupees" in refill_history.columns else 0.0
    fwd_returns: list[float] = []
    idx = open_df.index
    for _, row in refill_history.iterrows():
        signal_date = pd.Timestamp(row["date"])
        sym = row["symbol"]
        if sym not in open_df.columns:
            continue
        pos = idx.searchsorted(signal_date)
        entry_pos = pos + 1
        exit_pos = entry_pos + lookahead_days
        if entry_pos >= len(idx) or exit_pos >= len(idx):
            continue
        entry_px = open_df.iloc[entry_pos].get(sym)
        exit_px = close_df.iloc[exit_pos].get(sym)
        if entry_px is None or exit_px is None:
            continue
        if np.isnan(entry_px) or np.isnan(exit_px) or entry_px <= 0:
            continue
        fwd_returns.append(float(exit_px / entry_px - 1.0))
    if not fwd_returns:
        return RefillDiagnostics(len(refill_history), total_rupees, 0.0)
    return RefillDiagnostics(
        num_refills=len(refill_history),
        total_rupees_deployed=total_rupees,
        avg_forward_return_21d=float(np.array(fwd_returns).mean()),
    )


def benchmark_equity(index_close: pd.Series, initial_capital: float,
                     start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.Series:
    """Buy-and-hold equity curve for a reference index over the same window."""
    mask = (index_close.index >= start_date) & (index_close.index <= end_date)
    px = index_close[mask].dropna()
    if px.empty:
        return pd.Series(dtype=float)
    shares = initial_capital / float(px.iloc[0])
    return (shares * px).rename("benchmark")
