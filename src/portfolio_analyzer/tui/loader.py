"""Load a backtest JSON report and its companion CSV artifacts into analysis-
ready frames. Companion file layout matches `backtest/runner.py::_write_artifacts`:
`{stem}_equity.csv`, `_fills.csv`, `_decisions.csv`, `_blocked.csv`,
`_rearms.csv`, `_refills.csv`."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd


@dataclass
class BacktestArtifacts:
    path: Path
    report: dict
    equity: pd.DataFrame
    fills: pd.DataFrame
    refills: pd.DataFrame
    rearms: pd.DataFrame
    blocked: pd.DataFrame
    decisions: pd.DataFrame = field(default_factory=pd.DataFrame)


def _read_csv_opt(path: Path, **kw) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, **kw)


def load(json_path: Path) -> BacktestArtifacts:
    """Load the JSON report and sibling CSVs sharing its stem."""
    json_path = Path(json_path)
    with json_path.open() as fh:
        report = json.load(fh)
    stem = json_path.with_suffix("")

    equity = _read_csv_opt(stem.parent / f"{stem.name}_equity.csv",
                           index_col=0, parse_dates=True).sort_index()
    fills = _read_csv_opt(stem.parent / f"{stem.name}_fills.csv",
                          parse_dates=["date"])
    refills = _read_csv_opt(stem.parent / f"{stem.name}_refills.csv",
                            parse_dates=["date"])
    rearms = _read_csv_opt(stem.parent / f"{stem.name}_rearms.csv",
                           parse_dates=["date"])
    blocked = _read_csv_opt(stem.parent / f"{stem.name}_blocked.csv",
                            parse_dates=["date"])
    decisions = _read_csv_opt(stem.parent / f"{stem.name}_decisions.csv",
                              parse_dates=["date"])
    return BacktestArtifacts(
        path=json_path, report=report, equity=equity, fills=fills,
        refills=refills, rearms=rearms, blocked=blocked, decisions=decisions,
    )


def _cagr(eq: pd.Series) -> float:
    if len(eq) < 2:
        return float("nan")
    n = len(eq)
    return float((eq.iloc[-1] / eq.iloc[0]) ** (252.0 / n) - 1.0)


def _sharpe(rets: pd.Series) -> float:
    if len(rets) < 2 or rets.std() == 0:
        return float("nan")
    return float((rets.mean() / rets.std()) * np.sqrt(252))


def _max_drawdown(eq: pd.Series) -> float:
    if eq.empty:
        return float("nan")
    return float((eq / eq.cummax() - 1.0).min())


def per_year(equity: pd.DataFrame) -> pd.DataFrame:
    """Calendar-year portfolio vs benchmark split. Columns:
    year, port_ret, bench_ret, alpha, sharpe, vol_ann, maxdd, avg_expo,
    start_eq, end_eq."""
    if equity.empty:
        return pd.DataFrame()
    rows = []
    for y, blk in equity.groupby(equity.index.year):
        if len(blk) < 2:
            continue
        port_rets = blk["equity"].pct_change().dropna()
        has_bench = "benchmark" in blk.columns and blk["benchmark"].notna().any()
        port_r = float(blk["equity"].iloc[-1] / blk["equity"].iloc[0] - 1.0)
        bench_r = float(blk["benchmark"].iloc[-1] / blk["benchmark"].iloc[0] - 1.0) \
            if has_bench else float("nan")
        rows.append({
            "year": int(y),
            "port_ret": port_r,
            "bench_ret": bench_r,
            "alpha": port_r - bench_r if has_bench else float("nan"),
            "sharpe": _sharpe(port_rets),
            "vol_ann": float(port_rets.std() * np.sqrt(252)),
            "maxdd": _max_drawdown(blk["equity"]),
            "avg_expo": float(blk["exposure"].mean()) if "exposure" in blk.columns else float("nan"),
            "start_eq": float(blk["equity"].iloc[0]),
            "end_eq": float(blk["equity"].iloc[-1]),
        })
    return pd.DataFrame(rows)


def per_quarter(equity: pd.DataFrame) -> pd.DataFrame:
    """Calendar-quarter split. Columns: quarter, port_ret, bench_ret, alpha,
    avg_expo, end_eq."""
    if equity.empty:
        return pd.DataFrame()
    rows = []
    q = equity.index.to_period("Q")
    for qp, blk in equity.groupby(q):
        if len(blk) < 2:
            continue
        has_bench = "benchmark" in blk.columns and blk["benchmark"].notna().any()
        port_r = float(blk["equity"].iloc[-1] / blk["equity"].iloc[0] - 1.0)
        bench_r = float(blk["benchmark"].iloc[-1] / blk["benchmark"].iloc[0] - 1.0) \
            if has_bench else float("nan")
        rows.append({
            "quarter": str(qp),
            "port_ret": port_r,
            "bench_ret": bench_r,
            "alpha": port_r - bench_r if has_bench else float("nan"),
            "avg_expo": float(blk["exposure"].mean()) if "exposure" in blk.columns else float("nan"),
            "end_eq": float(blk["equity"].iloc[-1]),
        })
    return pd.DataFrame(rows)


def window_label(report: dict) -> str:
    """Render a compact `YYYY-YYYY` label from `report['window']`."""
    w = report.get("window", {}) or {}
    s, e = w.get("start"), w.get("end")
    if not s or not e:
        return "?"
    return f"{str(s)[:4]}-{str(e)[:4]}"


def normalize_equity(art: BacktestArtifacts, column: str = "equity") -> pd.Series:
    """Return `column` rebased to 1.0 indexed by trading-day offset from start.
    Enables overlaying curves from different-length/timeframe runs on one plot."""
    eq = art.equity
    if eq.empty or column not in eq.columns:
        return pd.Series(dtype="float64")
    s = eq[column].dropna()
    if s.empty:
        return pd.Series(dtype="float64")
    return pd.Series((s / s.iloc[0]).values, index=range(len(s)), dtype="float64")


def drawdown_series(art: BacktestArtifacts, column: str = "equity") -> pd.Series:
    """Return running drawdown of `column` indexed by trading-day offset."""
    eq = art.equity
    if eq.empty or column not in eq.columns:
        return pd.Series(dtype="float64")
    s = eq[column].dropna()
    if s.empty:
        return pd.Series(dtype="float64")
    dd = (s / s.cummax() - 1.0).values
    return pd.Series(dd, index=range(len(s)), dtype="float64")


def compare_metrics(arts: list[BacktestArtifacts]) -> pd.DataFrame:
    """One row per backtest with portfolio vs benchmark headline metrics.
    Columns: label, start, end, cagr, bench_cagr, alpha, sharpe, bench_sharpe,
    maxdd, bench_maxdd, vol, avg_expo, end_eq, fills, refills."""
    rows = []
    for a in arts:
        r = a.report
        perf = r.get("performance") or {}
        bench = r.get("benchmark_nifty500") or {}
        w = r.get("window", {}) or {}
        rows.append({
            "label": window_label(r),
            "start": str(w.get("start", "?")),
            "end": str(w.get("end", "?")),
            "cagr": float(perf.get("cagr", float("nan"))),
            "bench_cagr": float(bench.get("cagr", float("nan"))),
            "alpha": float(perf.get("cagr", float("nan"))) - float(bench.get("cagr", float("nan"))),
            "sharpe": float(perf.get("sharpe", float("nan"))),
            "bench_sharpe": float(bench.get("sharpe", float("nan"))),
            "maxdd": float(perf.get("max_drawdown", float("nan"))),
            "bench_maxdd": float(bench.get("max_drawdown", float("nan"))),
            "vol": float(perf.get("volatility_annual", float("nan"))),
            "avg_expo": float(r.get("avg_exposure", float("nan"))),
            "end_eq": float(r.get("ending_equity", float("nan"))),
            "fills": int(r.get("num_fills", 0)),
            "refills": int((r.get("refills") or {}).get("num_refills", 0)),
        })
    return pd.DataFrame(rows)


def holdings_from_fills(fills: pd.DataFrame) -> pd.DataFrame:
    """Replay BUY/SELL fills to reconstruct end-of-window share counts plus
    rupee flow per symbol. `avg_cost` is the weighted-average BUY price across
    the full history (BUY leg only). Columns: symbol, shares, avg_cost,
    invested, realized."""
    if fills.empty:
        return pd.DataFrame(columns=["symbol", "shares", "avg_cost", "invested", "realized"])
    rows: dict[str, dict] = {}
    for r in fills.sort_values("date").itertuples(index=False):
        h = rows.setdefault(r.symbol, {"shares": 0.0, "buy_shares": 0.0,
                                        "invested": 0.0, "realized": 0.0})
        if r.side == "BUY":
            h["shares"] += float(r.shares)
            h["buy_shares"] += float(r.shares)
            h["invested"] += float(r.shares) * float(r.price)
        else:
            h["shares"] -= float(r.shares)
            h["realized"] += float(r.shares) * float(r.price)
    out = []
    for sym, h in rows.items():
        avg_cost = (h["invested"] / h["buy_shares"]) if h["buy_shares"] > 1e-9 else float("nan")
        out.append({"symbol": sym, "shares": h["shares"], "avg_cost": avg_cost,
                    "invested": h["invested"], "realized": h["realized"]})
    df = pd.DataFrame(out)
    return df[df["shares"] > 1e-9].sort_values("shares", ascending=False).reset_index(drop=True)
