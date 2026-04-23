from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from portfolio_analyzer import config as cfg
from portfolio_analyzer.backtest import metrics as bt_metrics
from portfolio_analyzer.backtest import phase0_strategy, simulator
from portfolio_analyzer.backtest.data import fetch_ohlc
from portfolio_analyzer.instruments import load_nifty50_symbols, load_nifty500_symbols
from portfolio_analyzer.yf_fetch import to_stock_ticker

log = logging.getLogger(__name__)


def _frames_from_ohlc(ohlc: dict[str, pd.DataFrame], column: str) -> pd.DataFrame:
    cols = {t: df[column] for t, df in ohlc.items() if column in df.columns}
    if not cols:
        return pd.DataFrame()
    return pd.concat(cols, axis=1).sort_index()


def _rename_ns_to_symbol(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(columns=lambda t: t[:-3] if t.endswith(".NS") else t)


def _summarize_defer(defer_history: pd.DataFrame) -> dict:
    """Counters for the D-BT25 EXIT-defer lifecycle events."""
    if defer_history is None or defer_history.empty:
        return {"num_enqueued": 0, "num_fire_expired": 0, "num_fire_acute": 0,
                "num_cancel_upgrade": 0, "num_cancel_floor": 0}
    counts = defer_history["event"].value_counts()
    return {
        "num_enqueued": int(counts.get("enqueue", 0)),
        "num_fire_expired": int(counts.get("fire_expired", 0)),
        "num_fire_acute": int(counts.get("fire_acute", 0)),
        "num_cancel_upgrade": int(counts.get("cancel_upgrade", 0)),
        "num_cancel_floor": int(counts.get("cancel_floor", 0)),
    }


def run_backtest(
    start_date: dt.date,
    end_date: dt.date,
    initial_capital: float,
    out_path: Path | None = None,
) -> dict:
    nifty50 = load_nifty50_symbols()
    nifty500 = load_nifty500_symbols()
    if not nifty50 or not nifty500:
        raise RuntimeError("Missing NIFTY 50 / 500 constituent CSVs; run `refresh` first.")

    # Universe: NIFTY 50 (initial portfolio) ∪ NIFTY 500 (for breadth) + index tickers.
    universe_symbols = list(dict.fromkeys(nifty500 + nifty50))
    universe_tickers = [to_stock_ticker(s) for s in universe_symbols]
    index_tickers = [cfg.NIFTY500_YF_SYMBOL, cfg.NIFTY50_YF_SYMBOL]
    all_tickers = index_tickers + universe_tickers

    # Widen fetch window so rolling windows (252d high, 200d MA) are populated at start_date.
    fetch_start = start_date - dt.timedelta(days=int(cfg.HIGH_52W_WINDOW * 1.6) + 30)
    fetch_end = end_date + dt.timedelta(days=1)
    log.info("backtest: fetching OHLC %s -> %s for %d tickers", fetch_start, fetch_end, len(all_tickers))
    ohlc = fetch_ohlc(all_tickers, fetch_start, fetch_end)

    if cfg.NIFTY500_YF_SYMBOL not in ohlc or cfg.NIFTY50_YF_SYMBOL not in ohlc:
        raise RuntimeError("Missing NIFTY 500 / NIFTY 50 index OHLC; cannot proceed.")
    nifty500_close = ohlc[cfg.NIFTY500_YF_SYMBOL]["Close"]

    open_all = _frames_from_ohlc(ohlc, "Open")
    close_all = _frames_from_ohlc(ohlc, "Close")
    open_syms = _rename_ns_to_symbol(open_all.drop(columns=index_tickers, errors="ignore"))
    close_syms = _rename_ns_to_symbol(close_all.drop(columns=index_tickers, errors="ignore"))

    # Align to trading calendar of the NIFTY 500 index. Forward-fill close
    # prices so intraday gaps in any one ticker don't vanish a held position
    # from mark-to-market (opens stay as-is; missing opens block execution).
    cal_idx = nifty500_close.index
    open_syms = open_syms.reindex(cal_idx)
    close_syms = close_syms.reindex(cal_idx).ffill()

    universe_close = close_syms.reindex(columns=nifty500, fill_value=float("nan"))
    holding_close = close_syms.reindex(columns=nifty50, fill_value=float("nan"))

    log.info("backtest: computing market daily series and decisions matrix")
    market_daily = phase0_strategy.compute_market_daily(
        nifty500_close=nifty500_close,
        nifty50_close=ohlc[cfg.NIFTY50_YF_SYMBOL]["Close"],
        universe_close=universe_close,
    )
    # D-BT23: run the state machine and refill-eligibility over the full NIFTY
    # 500 universe so that (a) refill can source non-core candidates, and
    # (b) once a non-core name is refilled, the ordinary REDUCE/EXIT/REENTRY
    # transitions fire for it identically to core names. Core portfolio is
    # still seeded from NIFTY 50 at INIT.
    decisions, raw_signals, rs = phase0_strategy.compute_decisions(
        holding_close=universe_close, nifty500_close=nifty500_close,
    )
    refill_eligible = phase0_strategy.compute_refill_eligibility(universe_close)

    # D-BT15/20: simulator enforces a soft exposure floor during UPTREND+SIDEWAYS
    # days. D-BT21: when exposure still < floor, rank REDUCED names by RS and
    # upgrade top candidates to FULL until floor is met. D-BT22/23: if exposure
    # is still short of REFILL_STOP_EXPOSURE, open fresh entries from the NIFTY
    # 500 pool that pass the 50DMA/200DMA/dd>=-15%/RS>0 gate, ranked by RS,
    # subject to REFILL_EXTERNAL_EXPOSURE_CAP on non-core names in aggregate.
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)

    log.info("backtest: running simulator %s -> %s", start_date, end_date)
    result = simulator.run_simulation(
        start_date=start_ts, end_date=end_ts,
        initial_capital=initial_capital,
        holding_symbols=nifty50,
        open_df=open_syms, close_df=close_syms,
        decisions=decisions,
        market_trend=market_daily.trend,
        rank_df=rs,
        refill_eligible_df=refill_eligible,
        candidate_symbols=nifty500,
    )

    perf = bt_metrics.perf_stats(result.equity_curve)
    diag = bt_metrics.exit_diagnostics(result.decisions_history, open_syms, close_syms)
    rearm_diag = bt_metrics.rearm_diagnostics(result.rearm_history, open_syms, close_syms)
    refill_diag = bt_metrics.refill_diagnostics(result.refill_history, open_syms, close_syms)
    bench = bt_metrics.benchmark_equity(nifty500_close, initial_capital, start_ts, end_ts)
    bench_perf = bt_metrics.perf_stats(bench) if not bench.empty else None
    avg_exp = bt_metrics.avg_exposure(result.exposure_curve)

    report = {
        "window": {"start": str(start_date), "end": str(end_date)},
        "initial_capital": initial_capital,
        "universe": {"initial_portfolio": "NIFTY50", "breadth": "NIFTY500"},
        "performance": asdict(perf),
        "benchmark_nifty500": asdict(bench_perf) if bench_perf else None,
        "exits": asdict(diag),
        "rearms": asdict(rearm_diag),
        "refills": asdict(refill_diag),
        "avg_exposure": avg_exp,
        "ending_equity": result.ending_equity,
        "num_fills": len(result.fills),
        "num_holdings_at_end": sum(1 for s in result.final_positions.values() if s > 0),
        "num_blocked_by_exposure_floor": int(len(result.blocked_history)),
        "market_regime_days": {
            "UPTREND": int((market_daily.trend == "UPTREND").sum()),
            "SIDEWAYS": int((market_daily.trend == "SIDEWAYS").sum()),
            "DOWNTREND": int((market_daily.trend == "DOWNTREND").sum()),
        },
        "exit_deferrals": _summarize_defer(result.defer_history),
    }
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2, default=str) + "\n")
        _write_artifacts(out_path, result, bench)
        log.info("backtest: wrote %s", out_path)
    return report


def _write_artifacts(out_path: Path, result: simulator.SimResult, bench: pd.Series) -> None:
    stem = out_path.with_suffix("")
    eq = result.equity_curve.rename("equity").to_frame()
    if not bench.empty:
        eq["benchmark"] = bench.reindex(eq.index).ffill()
    eq["exposure"] = result.exposure_curve.reindex(eq.index)
    eq.to_csv(stem.parent / f"{stem.name}_equity.csv")
    if result.decisions_history is not None and not result.decisions_history.empty:
        result.decisions_history.to_csv(stem.parent / f"{stem.name}_decisions.csv", index=False)
    if result.blocked_history is not None and not result.blocked_history.empty:
        result.blocked_history.to_csv(stem.parent / f"{stem.name}_blocked.csv", index=False)
    if result.rearm_history is not None and not result.rearm_history.empty:
        result.rearm_history.to_csv(stem.parent / f"{stem.name}_rearms.csv", index=False)
    if result.refill_history is not None and not result.refill_history.empty:
        result.refill_history.to_csv(stem.parent / f"{stem.name}_refills.csv", index=False)
    if result.defer_history is not None and not result.defer_history.empty:
        result.defer_history.to_csv(stem.parent / f"{stem.name}_defers.csv", index=False)
    if result.fills:
        pd.DataFrame([f.__dict__ for f in result.fills]).to_csv(
            stem.parent / f"{stem.name}_fills.csv", index=False,
        )
