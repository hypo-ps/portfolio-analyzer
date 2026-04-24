"""VCP scan orchestrator.

Walks every ISIN in ``stock_master`` that has enough adjusted OHLCV history as
of ``trade_date``, computes technical + fundamental features, scores the
candidate, and upserts the result into ``vcp_candidates``.
"""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
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

            result = score_candidate(tech, fund, rs_score=rs_score)
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
            })

            if idx % 100 == 0:
                logger.info("vcp-scan: %d/%d processed", idx, len(universe))

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
