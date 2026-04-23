"""Widget factories for each TUI tab. Each `build_*` function returns the
widget(s) that compose a tab's body."""

from __future__ import annotations

from typing import Iterable

import pandas as pd
from textual.containers import Vertical
from textual.widgets import DataTable, Static
from textual_plotext import PlotextPlot

from portfolio_analyzer.tui.loader import (
    BacktestArtifacts, holdings_from_fills, per_quarter, per_year,
)


def _fmt_pct(x: float) -> str:
    return "--" if pd.isna(x) else f"{x * 100:+.2f}%"


def _fmt_money(x: float) -> str:
    return "--" if pd.isna(x) else f"Rs.{x:,.0f}"


def _fmt_num(x: float, nd: int = 2) -> str:
    return "--" if pd.isna(x) else f"{x:.{nd}f}"


def _rows_to_table(table: DataTable, columns: Iterable[str],
                   rows: Iterable[tuple]) -> None:
    table.clear(columns=True)
    table.add_columns(*columns)
    for row in rows:
        table.add_row(*row)


def build_summary(art: BacktestArtifacts) -> Static:
    r = art.report
    perf = r.get("performance", {})
    bench = r.get("benchmark_nifty500", {})
    w = r.get("window", {})
    regime = r.get("market_regime_days", {})
    lines = [
        f"[b]Window[/]         {w.get('start')} -> {w.get('end')}   ({perf.get('days', '?')} days)",
        f"[b]Universe[/]       init={r.get('universe', {}).get('initial_portfolio')}  breadth={r.get('universe', {}).get('breadth')}",
        f"[b]Initial capital[/] {_fmt_money(r.get('initial_capital', float('nan')))}",
        f"[b]Ending equity[/]  {_fmt_money(r.get('ending_equity', float('nan')))}",
        "",
        f"[b]Portfolio[/]      CAGR {_fmt_pct(perf.get('cagr', float('nan')))}   "
        f"Sharpe {_fmt_num(perf.get('sharpe', float('nan')))}   "
        f"MaxDD {_fmt_pct(perf.get('max_drawdown', float('nan')))}   "
        f"Vol {_fmt_pct(perf.get('volatility_annual', float('nan')))}",
        f"[b]Benchmark[/]      CAGR {_fmt_pct(bench.get('cagr', float('nan')))}   "
        f"Sharpe {_fmt_num(bench.get('sharpe', float('nan')))}   "
        f"MaxDD {_fmt_pct(bench.get('max_drawdown', float('nan')))}   "
        f"Vol {_fmt_pct(bench.get('volatility_annual', float('nan')))}",
        "",
        f"[b]Avg exposure[/]   {_fmt_num(r.get('avg_exposure', float('nan')), 3)}",
        f"[b]Holdings at end[/] {r.get('num_holdings_at_end', '?')}",
        f"[b]Fills[/]          {r.get('num_fills', '?')}   "
        f"Exits {r.get('exits', {}).get('num_exits', '?')}   "
        f"Reduces {r.get('exits', {}).get('num_reduces', '?')}   "
        f"Rearms {r.get('rearms', {}).get('num_rearms', '?')}   "
        f"Refills {r.get('refills', {}).get('num_refills', '?')}",
        f"[b]Blocked by floor[/] {r.get('num_blocked_by_exposure_floor', '?')}",
        "",
        f"[b]Regime days[/]    UP {regime.get('UPTREND', 0)}  "
        f"SIDE {regime.get('SIDEWAYS', 0)}  DOWN {regime.get('DOWNTREND', 0)}",
    ]
    return Static("\n".join(lines), id="summary-body")


def build_equity(art: BacktestArtifacts) -> PlotextPlot:
    plot = PlotextPlot(id="equity-plot")
    eq = art.equity
    if eq.empty:
        return plot
    dates = [d.strftime("%Y-%m-%d") for d in eq.index]
    plot.plt.date_form("Y-m-d")
    plot.plt.plot(dates, list(eq["equity"].values), label="Portfolio", color="cyan")
    if "benchmark" in eq.columns and eq["benchmark"].notna().any():
        plot.plt.plot(dates, list(eq["benchmark"].values), label="NIFTY 500", color="orange")
    plot.plt.title("Equity curve")
    plot.plt.xlabel("Date")
    plot.plt.ylabel("Rs.")
    return plot


def build_per_year(art: BacktestArtifacts) -> Vertical:
    df = per_year(art.equity)
    table = DataTable(id="year-table", zebra_stripes=True)
    _rows_to_table(
        table,
        ["Year", "Port", "Bench", "Alpha", "Sharpe", "Vol", "MaxDD", "AvgExpo", "End Eq"],
        [(str(int(r.year)), _fmt_pct(r.port_ret), _fmt_pct(r.bench_ret),
          _fmt_pct(r.alpha), _fmt_num(r.sharpe), _fmt_pct(r.vol_ann),
          _fmt_pct(r.maxdd), _fmt_num(r.avg_expo, 3), _fmt_money(r.end_eq))
         for r in df.itertuples(index=False)],
    )
    plot = PlotextPlot(id="year-plot")
    if not df.empty:
        years = [str(int(y)) for y in df["year"].tolist()]
        plot.plt.multiple_bar(
            years,
            [(df["port_ret"] * 100).tolist(), (df["bench_ret"] * 100).tolist()],
            labels=["Portfolio", "Benchmark"],
        )
        plot.plt.title("Annual returns (%)")
    return Vertical(table, plot, id="year-body")


def build_per_quarter(art: BacktestArtifacts) -> DataTable:
    df = per_quarter(art.equity)
    table = DataTable(id="quarter-table", zebra_stripes=True)
    _rows_to_table(
        table,
        ["Quarter", "Port", "Bench", "Alpha", "AvgExpo", "End Eq"],
        [(r.quarter, _fmt_pct(r.port_ret), _fmt_pct(r.bench_ret),
          _fmt_pct(r.alpha), _fmt_num(r.avg_expo, 3), _fmt_money(r.end_eq))
         for r in df.itertuples(index=False)],
    )
    return table


def build_fills(art: BacktestArtifacts) -> DataTable:
    f = art.fills.sort_values("date")
    table = DataTable(id="fills-table", zebra_stripes=True)
    _rows_to_table(
        table,
        ["Date", "Symbol", "Side", "Shares", "Price", "Rupees", "Reason"],
        [(r.date.strftime("%Y-%m-%d"), r.symbol, r.side,
          _fmt_num(r.shares, 2), _fmt_num(r.price, 2),
          _fmt_money(float(r.shares) * float(r.price)), r.reason)
         for r in f.itertuples(index=False)],
    )
    return table


def build_refills(art: BacktestArtifacts) -> DataTable:
    r = art.refills.sort_values("date")
    table = DataTable(id="refills-table", zebra_stripes=True)
    _rows_to_table(
        table,
        ["Date", "Symbol", "Rupees"],
        [(row.date.strftime("%Y-%m-%d"), row.symbol, _fmt_money(row.rupees))
         for row in r.itertuples(index=False)],
    )
    return table


def build_holdings(art: BacktestArtifacts) -> DataTable:
    h = holdings_from_fills(art.fills)
    table = DataTable(id="holdings-table", zebra_stripes=True)
    _rows_to_table(
        table,
        ["Symbol", "Shares", "Avg cost", "Invested", "Realized"],
        [(row.symbol, _fmt_num(row.shares, 2), _fmt_money(row.avg_cost),
          _fmt_money(row.invested), _fmt_money(row.realized))
         for row in h.itertuples(index=False)],
    )
    return table
