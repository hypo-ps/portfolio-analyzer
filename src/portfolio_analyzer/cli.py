from __future__ import annotations

import datetime as dt
import json
import logging
import math
import sys
from pathlib import Path

import click
import pandas as pd

from portfolio_analyzer import config as cfg
from portfolio_analyzer.auth import interactive_login
from portfolio_analyzer.config import load_credentials
from portfolio_analyzer.instruments import (
    load_nifty50_symbols,
    load_nifty500_symbols,
    load_sector_map_default,
)
from portfolio_analyzer.kite_client import KiteClient
from portfolio_analyzer.market import compute_breadth_pct, compute_market_state
from portfolio_analyzer.refresh import refresh_constituents
from portfolio_analyzer.report import PendingExitOut, ScoredStock, build_report
from portfolio_analyzer.scoring import score_stock
from portfolio_analyzer.stock_analysis import compute_metrics
from portfolio_analyzer import strategy
from portfolio_analyzer.util import ohlc
from portfolio_analyzer.yf_fetch import fetch_daily_closes, to_stock_ticker

PREV_STATE_MAX_AGE_DAYS = 7


def _holding_symbols(raw: list[dict]) -> list[str]:
    """Collect unique tradingsymbols from Kite holdings. BSE-listed holdings are
    kept and resolved against their NSE-equivalent ticker on yfinance (same symbol
    for dual-listed names); if yfinance has no NSE data, the holding is dropped
    downstream with a 'no price history' warning."""
    seen: set[str] = set()
    symbols: list[str] = []
    for h in raw:
        sym = h.get("tradingsymbol")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        symbols.append(sym)
    return symbols


def _load_previous_states(
    output_dir: Path, today: dt.date,
) -> tuple[dict[str, str], dict[str, tuple[int, str]]]:
    """Return (prev_decisions, pending_exits) from the most recent prior JSON in
    output_dir (D-BT17 + D-BT28). `pending_exits[sym] = (days_remaining,
    enqueued_date)`. Both default to empty if no prior JSON or stale."""
    if not output_dir.exists() or not output_dir.is_dir():
        return {}, {}
    latest_path: Path | None = None
    latest_date: dt.date | None = None
    for p in output_dir.glob("*.json"):
        try:
            d = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if d >= today:
            continue
        if latest_date is None or d > latest_date:
            latest_date = d
            latest_path = p
    if latest_path is None or latest_date is None:
        logging.info("No prior JSON found in %s; treating all stocks as HOLD.", output_dir)
        return {}, {}
    age = (today - latest_date).days
    if age > PREV_STATE_MAX_AGE_DAYS:
        logging.warning(
            "Prior JSON %s is %d days old (>%d); treating all stocks as HOLD.",
            latest_path.name, age, PREV_STATE_MAX_AGE_DAYS,
        )
        return {}, {}
    try:
        data = json.loads(latest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Failed to parse %s (%s); using HOLD for all.", latest_path, exc)
        return {}, {}
    states: dict[str, str] = {}
    for entry in data.get("stocks", []):
        sym = entry.get("symbol")
        dec = entry.get("decision")
        if sym and dec in strategy.VALID_STATES:
            states[sym] = dec
    pending: dict[str, tuple[int, str]] = {}
    for entry in data.get("pending_exits", []) or []:
        sym = entry.get("symbol")
        days = entry.get("days_remaining")
        enq = entry.get("enqueued_date") or latest_path.stem
        if sym and isinstance(days, int) and days > 0:
            pending[sym] = (days, str(enq))
    logging.info(
        "Seeded from %s (%d symbols, %d pending exits).",
        latest_path.name, len(states), len(pending),
    )
    return states, pending


def run_analysis(out_file: Path | None, do_refresh: bool = True) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if do_refresh:
        refresh_constituents()

    creds = load_credentials()
    kite = interactive_login(creds)
    client = KiteClient(kite)

    logging.info("Fetching holdings...")
    holding_symbols = _holding_symbols(client.fetch_holdings())

    nifty500_syms = load_nifty500_symbols()
    load_nifty50_symbols()  # triggers warning if refresh failed

    all_stock_syms = list(dict.fromkeys(nifty500_syms + holding_symbols))
    tickers = [cfg.NIFTY500_YF_SYMBOL, cfg.NIFTY50_YF_SYMBOL] + [to_stock_ticker(s) for s in all_stock_syms]
    logging.info("Fetching daily closes from yfinance (%d tickers)...", len(tickers))
    closes = fetch_daily_closes(tickers)

    nifty500_close = closes.get(cfg.NIFTY500_YF_SYMBOL, pd.Series(dtype="float64"))
    nifty50_close = closes.get(cfg.NIFTY50_YF_SYMBOL, pd.Series(dtype="float64"))
    if nifty500_close.empty or nifty50_close.empty:
        raise RuntimeError("Failed to fetch NIFTY 500 / NIFTY 50 index history from yfinance.")

    breadth_series = {s: closes[to_stock_ticker(s)] for s in nifty500_syms if to_stock_ticker(s) in closes}
    breadth_pct = compute_breadth_pct(breadth_series)

    holding_series = {s: closes[to_stock_ticker(s)] for s in holding_symbols if to_stock_ticker(s) in closes}

    market = compute_market_state(nifty500_close, nifty50_close, breadth_pct)
    market_ret50 = ohlc.return_over(nifty500_close, cfg.RETURN_WINDOW)

    today = dt.date.today()
    prev_states, prev_pending = _load_previous_states(
        out_file.parent if out_file is not None else Path("output"), today,
    )

    sector_map = load_sector_map_default()
    scored_stocks: list[ScoredStock] = []
    pending_out: list[PendingExitOut] = []
    for symbol in holding_symbols:
        series = holding_series.get(symbol)
        if series is None or len(series) == 0:
            logging.warning("No price history for %s; excluded", symbol)
            continue
        metrics = compute_metrics(symbol, series, market_ret50 if not math.isnan(market_ret50) else float("nan"))
        scored = score_stock(metrics)
        prev = prev_states.get(symbol, strategy.STATE_HOLD)
        prev_close = float(series.iloc[-2]) if len(series) >= 2 else float("nan")
        pending = prev_pending.get(symbol)
        pending_days = pending[0] if pending is not None else None
        resolution = strategy.resolve_with_defer(
            prev_state=prev, metrics=metrics, raw_signal=scored.decision,
            prev_close=prev_close, pending_days=pending_days,
        )
        if resolution.event is not None:
            scored.reasons.append(f"[defer] {resolution.event}: {resolution.reason}")
        if resolution.pending_days_remaining is not None:
            enq = pending[1] if pending is not None else today.isoformat()
            pending_out.append(PendingExitOut(
                symbol=symbol,
                days_remaining=resolution.pending_days_remaining,
                enqueued_date=enq,
            ))
        scored_stocks.append(
            ScoredStock(
                metrics=metrics, scored=scored, sector=sector_map.get(symbol, "UNKNOWN"),
                decision=resolution.decision, prev_state=prev,
            )
        )

    report = build_report(
        date_str=today.isoformat(),
        market=market,
        scored_stocks=scored_stocks,
        pending_exits=pending_out,
    )
    payload = json.dumps(report.model_dump(), indent=2)
    sys.stdout.write(payload + "\n")
    if out_file is not None:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(payload + "\n")
        logging.info("Wrote %s", out_file)


@click.group()
def main() -> None:
    """Portfolio analyzer CLI."""


@main.command()
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Optional output JSON file. Default: stdout only.",
)
@click.option(
    "--no-refresh",
    is_flag=True,
    default=False,
    help="Skip daily constituent refresh (use existing local CSVs).",
)
def run(out_path: Path | None, no_refresh: bool) -> None:
    """Run portfolio analysis and emit the JSON report."""
    run_analysis(out_path, do_refresh=not no_refresh)


@main.command()
@click.option("--force", is_flag=True, default=False, help="Refresh even if files are fresh today.")
def refresh(force: bool) -> None:
    """Refresh NIFTY 500 / NIFTY 50 constituent lists and auto sector map."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    refresh_constituents(force=force)


@main.command()
@click.option("--start", "start_str", type=str, default=None,
              help="Backtest start date (YYYY-MM-DD). Default: end - 5 years.")
@click.option("--end", "end_str", type=str, default=None,
              help="Backtest end date (YYYY-MM-DD). Default: yesterday.")
@click.option("--years", type=int, default=5, show_default=True,
              help="Window length if --start not given.")
@click.option("--capital", type=float, default=1_000_000.0, show_default=True,
              help="Initial capital in INR.")
@click.option("--out", "out_path", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Output JSON path. Equity/fills CSVs are written alongside.")
def backtest(start_str: str | None, end_str: str | None, years: int,
             capital: float, out_path: Path | None) -> None:
    """Backtest Phase 0 decisions over a historical window."""
    from portfolio_analyzer.backtest.runner import run_backtest

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    end_date = dt.date.fromisoformat(end_str) if end_str else (dt.date.today() - dt.timedelta(days=1))
    start_date = dt.date.fromisoformat(start_str) if start_str else end_date.replace(
        year=end_date.year - years
    )
    report = run_backtest(
        start_date=start_date, end_date=end_date,
        initial_capital=capital, out_path=out_path,
    )
    sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")


@main.command()
@click.option("--input", "input_paths", required=True, multiple=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Backtest JSON report(s). Pass --input multiple times to "
                   "enable the Compare tab. Companion CSVs must share each stem.")
def tui(input_paths: tuple[Path, ...]) -> None:
    """Launch an interactive TUI to explore one or more backtest runs."""
    from portfolio_analyzer.tui.app import run_tui
    run_tui(list(input_paths))


if __name__ == "__main__":
    main()
