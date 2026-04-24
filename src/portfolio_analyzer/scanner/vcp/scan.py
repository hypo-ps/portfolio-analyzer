"""VCP scan orchestrator.

Walks every ISIN in ``stock_master`` that has enough adjusted OHLCV history as
of ``trade_date``, computes technical + fundamental features, scores the
candidate, and upserts the result into ``vcp_candidates``.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from portfolio_analyzer import config as cfg

from ..db import (
    default_db_path,
    init_schema,
    load_index_closes,
    open_db,
    upsert_vcp_candidates,
)
from ..index_ingest import INDEX_NIFTY500
from .features import MIN_BARS, compute_technical_features
from .fundamentals import load_fundamental_features
from .scorer import score_candidate

logger = logging.getLogger(__name__)

BARS_LOOKBACK = 320  # enough for EMA200 + 1y return + buffer
RS_WINDOW = cfg.RETURN_WINDOW  # Phase 0 RS uses 50 trading days

# Sector-strength tuning lives in ``portfolio_analyzer.config`` (see D-S33).
SECTOR_BOOST_MAX = cfg.SECTOR_BOOST_MAX
SECTOR_DOWNGRADE_CUTOFF = cfg.SECTOR_DOWNGRADE_CUTOFF
SECTOR_BOOST_STAGES = cfg.SECTOR_BOOST_STAGES


@dataclass
class ScanResult:
    trade_date: dt.date
    universe: int
    scored: int
    skipped_history: int
    by_decision: dict[str, int]
    stored: int
    benchmark_index: str | None = None
    benchmark_return_50d: float | None = None


def _benchmark_return_50d(
    conn: sqlite3.Connection, trade_date: dt.date,
    *, index_symbol: str = INDEX_NIFTY500, window: int = RS_WINDOW,
) -> float | None:
    """Return the benchmark's ``window``-bar return ending on/before ``trade_date``.

    Returns None if the index hasn't been ingested or has fewer than
    ``window+1`` bars as of the scan date.
    """
    closes = load_index_closes(
        conn, index_symbol, as_of=trade_date, lookback=window + 5,
    )
    if len(closes) < window + 1:
        return None
    latest = closes[-1][1]
    base = closes[-(window + 1)][1]
    if base <= 0:
        return None
    return (latest - base) / base


def _iter_universe(
    conn: sqlite3.Connection, trade_date: dt.date, only_symbols: tuple[str, ...] | None,
    limit: int | None,
) -> list[tuple[str, str]]:
    """Return (isin, symbol) pairs that have at least one bar on/near trade_date."""
    params: list = [trade_date.isoformat()]
    sql = (
        "SELECT DISTINCT s.isin, s.symbol "
        "FROM stock_master s "
        "JOIN market_data m ON m.isin = s.isin "
        "WHERE m.trade_date <= ? "
    )
    if only_symbols:
        placeholders = ",".join(["?"] * len(only_symbols))
        sql += f"AND s.symbol IN ({placeholders}) "
        params.extend(only_symbols)
    sql += "ORDER BY s.symbol"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def _load_bars(
    conn: sqlite3.Connection, isin: str, trade_date: dt.date, lookback: int,
) -> tuple[np.ndarray, ...] | None:
    """Return (open, high, low, close, volume) arrays ending on/near trade_date.

    Uses the ``adjusted_market_data`` view so splits/bonuses don't distort
    the signals. Returns None if too little history.
    """
    rows = conn.execute(
        "SELECT adj_open, adj_high, adj_low, adj_close, adj_volume "
        "FROM adjusted_market_data "
        "WHERE isin = ? AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?",
        (isin, trade_date.isoformat(), lookback),
    ).fetchall()
    if len(rows) < MIN_BARS:
        return None
    rows = list(reversed(rows))  # oldest -> newest
    arr = np.array(rows, dtype=float)
    return arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4]


def _compute_sector_strength(
    rows: list[dict[str, object]],
) -> dict[str, float]:
    """Return ``sector -> score in [0, 1]`` computed once per scan.

    ``rows`` carries one lightweight entry per scored stock with
    ``sector``, ``return_50d``, ``return_20d``, ``return_3m``, ``ema50``,
    ``ema200`` and ``close``. The score blends relative-strength (sector
    median return vs cross-universe median, weighted 0.5/0.3/0.2 for
    50/20/63 day windows) min-max normalised across sectors with breadth
    (fraction above EMA50 / EMA200) at 0.7 / 0.3. Stocks without a sector
    are ignored; sectors with zero ``return_50d`` members are dropped.
    """
    buckets: dict[str, list[dict[str, object]]] = defaultdict(list)
    for r in rows:
        if r["sector"] is None:
            continue
        buckets[r["sector"]].append(r)  # type: ignore[arg-type]

    all_50 = [r["return_50d"] for r in rows if r["return_50d"] is not None]
    all_20 = [r["return_20d"] for r in rows if r["return_20d"] is not None]
    all_3m = [r["return_3m"] for r in rows if r["return_3m"] is not None]
    idx_50 = float(np.median(all_50)) if all_50 else 0.0
    idx_20 = float(np.median(all_20)) if all_20 else 0.0
    idx_3m = float(np.median(all_3m)) if all_3m else 0.0

    raw: dict[str, dict[str, float]] = {}
    for sector, stocks in buckets.items():
        r50 = [s["return_50d"] for s in stocks if s["return_50d"] is not None]
        r20 = [s["return_20d"] for s in stocks if s["return_20d"] is not None]
        r3m = [s["return_3m"] for s in stocks if s["return_3m"] is not None]
        if not r50:
            continue
        sec_50 = float(np.median(r50))
        sec_20 = float(np.median(r20)) if r20 else 0.0
        sec_3m = float(np.median(r3m)) if r3m else 0.0
        rs = (
            0.5 * (sec_50 - idx_50)
            + 0.3 * (sec_20 - idx_20)
            + 0.2 * (sec_3m - idx_3m)
        )
        above_50 = sum(
            1 for s in stocks
            if s["close"] is not None and s["close"] > (s["ema50"] or 0.0)
        )
        above_200 = sum(
            1 for s in stocks
            if s["close"] is not None and s["close"] > (s["ema200"] or 0.0)
        )
        breadth = 0.6 * (above_50 / len(stocks)) + 0.4 * (above_200 / len(stocks))
        raw[sector] = {"rs": rs, "breadth": breadth}

    if not raw:
        return {}
    rs_values = [v["rs"] for v in raw.values()]
    lo, hi = min(rs_values), max(rs_values)
    span = hi - lo if hi != lo else 1.0
    out: dict[str, float] = {}
    for sector, parts in raw.items():
        norm_rs = (parts["rs"] - lo) / span
        out[sector] = 0.7 * norm_rs + 0.3 * parts["breadth"]
    return out


def _log_sector_leaderboard(scores: dict[str, float], *, k: int = 3) -> None:
    """Log the strongest / weakest ``k`` sectors for quick visibility."""
    if not scores:
        return
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top = ", ".join(f"{s}={v:.2f}" for s, v in ranked[:k])
    bottom = ", ".join(f"{s}={v:.2f}" for s, v in ranked[-k:][::-1])
    logger.info(
        "vcp-scan: sectors=%d top=[%s] bottom=[%s]",
        len(ranked), top, bottom,
    )


def scan_date(
    trade_date: dt.date,
    *,
    db_path: Path | None = None,
    only_symbols: tuple[str, ...] | None = None,
    limit: int | None = None,
    store_rejects: bool = False,
) -> ScanResult:
    """Score every eligible ISIN as of ``trade_date`` and upsert results.

    By default rejects are **not** persisted (the candidates table stays lean);
    pass ``store_rejects=True`` to keep the full audit trail.
    """
    path = db_path or default_db_path()
    with open_db(path) as conn:
        init_schema(conn)
        universe = _iter_universe(conn, trade_date, only_symbols, limit)
        bench_ret50 = _benchmark_return_50d(conn, trade_date)
        logger.info(
            "vcp-scan: universe=%d as_of=%s bench_ret50=%s",
            len(universe), trade_date,
            f"{bench_ret50:+.4f}" if bench_ret50 is not None else "n/a",
        )

        rows: list[dict[str, object]] = []
        skipped_history = 0
        by_decision: dict[str, int] = {}

        # First pass: extract tech + fund + returns per stock. Scoring is
        # deferred so we can compute sector-strength context once over the
        # full universe before scoring any row.
        sector_inputs: list[dict[str, object]] = []
        candidates_temp: list[tuple] = []

        for idx, (isin, symbol) in enumerate(universe, start=1):
            bars = _load_bars(conn, isin, trade_date, BARS_LOOKBACK)
            if bars is None:
                skipped_history += 1
                continue
            opens, highs, lows, closes, volumes = bars
            tech = compute_technical_features(opens, highs, lows, closes, volumes)
            if tech is None:
                skipped_history += 1
                continue
            fund = load_fundamental_features(conn, isin)

            ret50: float | None = None
            if closes.size >= RS_WINDOW + 1:
                base = float(closes[-(RS_WINDOW + 1)])
                if base > 0:
                    ret50 = (float(closes[-1]) - base) / base
            rs_score: float | None = None
            if ret50 is not None and bench_ret50 is not None:
                rs_score = ret50 - bench_ret50

            sector_inputs.append({
                "sector": fund.sector if fund else None,
                "return_50d": ret50,
                "return_20d": tech.return_20d,
                "return_3m": tech.return_3m,
                "ema50": tech.ema50,
                "ema200": tech.ema200,
                "close": tech.close,
            })
            candidates_temp.append(
                (isin, symbol, tech, fund, rs_score, ret50),
            )

            if idx % 100 == 0:
                logger.info("vcp-scan: %d/%d processed", idx, len(universe))

        sector_scores = _compute_sector_strength(sector_inputs)
        _log_sector_leaderboard(sector_scores)

        # Second pass: score each candidate and apply sector context.
        for isin, symbol, tech, fund, rs_score, ret50 in candidates_temp:
            result = score_candidate(tech, fund, rs_score=rs_score)
            sector = fund.sector if fund else None
            sector_score = sector_scores.get(sector) if sector else None

            if sector_score is not None:
                if result.stage in SECTOR_BOOST_STAGES:
                    boost = 1.0 + SECTOR_BOOST_MAX * sector_score
                    if result.final_score is not None:
                        result.final_score *= boost
                    result.reasons.append(f"sector_boost={boost:.3f}")
                if (sector_score < SECTOR_DOWNGRADE_CUTOFF
                        and result.decision == "BUY_ALERT"):
                    result.decision = "WATCHLIST"
                    result.reasons.append("weak_sector_downgrade")

            by_decision[result.decision] = by_decision.get(result.decision, 0) + 1

            if result.decision == "REJECT" and not store_rejects:
                continue

            rows.append({
                "isin": isin,
                "trade_date": trade_date,
                "symbol": symbol,
                "close": tech.close,
                "pivot": tech.pivot,
                "distance_to_pivot": tech.distance_to_pivot,
                "technical_score": result.technical_score,
                "vcp_score": result.vcp_score,
                "fundamental_score": result.fundamental_score,
                "readiness_score": result.readiness_score,
                "combined_score": result.combined_score,
                "final_score": result.final_score,
                "decision": result.decision,
                "stage": result.stage,
                "reasons": "; ".join(result.reasons) if result.reasons else None,
                "return_50d": ret50,
                "benchmark_return_50d": bench_ret50,
                "rs_score": rs_score,
                "sector": sector,
                "sector_score": sector_score,
            })

        stored = upsert_vcp_candidates(conn, rows)

    return ScanResult(
        trade_date=trade_date,
        universe=len(universe),
        scored=len(universe) - skipped_history,
        skipped_history=skipped_history,
        by_decision=by_decision,
        stored=stored,
        benchmark_index=INDEX_NIFTY500 if bench_ret50 is not None else None,
        benchmark_return_50d=bench_ret50,
    )
