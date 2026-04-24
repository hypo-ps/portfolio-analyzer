"""Loader for the scanner dashboard (reads vcp_candidates + joined tables)."""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from portfolio_analyzer.scanner.db import default_db_path, init_schema, open_db


DASH_DECISIONS_DEFAULT = ("BUY_ALERT", "WATCHLIST")
DASH_DECISIONS_ALL = ("BUY_ALERT", "WATCHLIST", "IGNORE", "SKIP", "REJECT")


@dataclass(frozen=True)
class CandidateRow:
    isin: str
    symbol: str
    trade_date: dt.date
    decision: str
    stage: str | None
    close: float | None
    pivot: float | None
    distance_to_pivot: float | None
    technical_score: float | None
    vcp_score: float | None
    fundamental_score: float | None
    readiness_score: float | None
    combined_score: float | None
    final_score: float | None
    return_50d: float | None
    benchmark_return_50d: float | None
    rs_score: float | None
    sector_score: float | None
    reasons: str | None
    # Joined from fundamentals_meta (may be None if not covered)
    sector: str | None
    industry: str | None
    market_cap_cr: float | None
    stock_pe: float | None
    roe_latest: float | None
    roce_latest: float | None


@dataclass(frozen=True)
class DashboardData:
    db_path: Path
    trade_date: dt.date | None
    rows: tuple[CandidateRow, ...]
    include_rejects: bool
    benchmark_return_50d: float | None
    universe_counts: dict[str, int]  # per-decision row count for this date


def _latest_trade_date(conn: sqlite3.Connection) -> dt.date | None:
    row = conn.execute("SELECT MAX(trade_date) FROM vcp_candidates").fetchone()
    if not row or not row[0]:
        return None
    return dt.date.fromisoformat(row[0])


def _row_counts(conn: sqlite3.Connection, td: dt.date) -> dict[str, int]:
    rows = conn.execute(
        "SELECT decision, COUNT(*) FROM vcp_candidates "
        "WHERE trade_date = ? GROUP BY decision ORDER BY decision",
        (td.isoformat(),),
    ).fetchall()
    return {d: n for d, n in rows}


def _benchmark_ret50(conn: sqlite3.Connection, td: dt.date) -> float | None:
    row = conn.execute(
        "SELECT benchmark_return_50d FROM vcp_candidates "
        "WHERE trade_date = ? AND benchmark_return_50d IS NOT NULL LIMIT 1",
        (td.isoformat(),),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _load_rows(
    conn: sqlite3.Connection, td: dt.date, include_rejects: bool,
) -> list[CandidateRow]:
    decisions = DASH_DECISIONS_ALL if include_rejects else DASH_DECISIONS_DEFAULT
    placeholders = ",".join("?" for _ in decisions)
    sql = f"""
        SELECT
            c.isin, c.symbol, c.trade_date, c.decision, c.stage,
            c.close, c.pivot, c.distance_to_pivot,
            c.technical_score, c.vcp_score, c.fundamental_score,
            c.readiness_score, c.combined_score, c.final_score,
            c.return_50d, c.benchmark_return_50d, c.rs_score,
            c.sector_score, c.reasons,
            f.sector, f.industry, f.market_cap_cr, f.stock_pe,
            f.roe_latest, f.roce_latest
        FROM vcp_candidates c
        LEFT JOIN fundamentals_meta f ON f.isin = c.isin
        WHERE c.trade_date = ? AND c.decision IN ({placeholders})
        ORDER BY c.final_score DESC NULLS LAST, c.symbol
    """
    rows = conn.execute(sql, (td.isoformat(), *decisions)).fetchall()
    out: list[CandidateRow] = []
    for r in rows:
        out.append(CandidateRow(
            isin=r[0], symbol=r[1], trade_date=dt.date.fromisoformat(r[2]),
            decision=r[3], stage=r[4],
            close=r[5], pivot=r[6], distance_to_pivot=r[7],
            technical_score=r[8], vcp_score=r[9], fundamental_score=r[10],
            readiness_score=r[11], combined_score=r[12], final_score=r[13],
            return_50d=r[14], benchmark_return_50d=r[15], rs_score=r[16],
            sector_score=r[17], reasons=r[18],
            sector=r[19], industry=r[20], market_cap_cr=r[21], stock_pe=r[22],
            roe_latest=r[23], roce_latest=r[24],
        ))
    return out


def load_dashboard(
    *, db_path: Path | None = None, trade_date: dt.date | None = None,
    include_rejects: bool = False,
) -> DashboardData:
    """Read the latest scan (or ``trade_date``) and return a DashboardData blob."""
    path = db_path or default_db_path()
    with open_db(path) as conn:
        init_schema(conn)  # idempotent; adds missing columns on older DBs
        td = trade_date or _latest_trade_date(conn)
        if td is None:
            return DashboardData(
                db_path=path, trade_date=None, rows=(),
                include_rejects=include_rejects,
                benchmark_return_50d=None, universe_counts={},
            )
        rows = _load_rows(conn, td, include_rejects)
        counts = _row_counts(conn, td)
        bench = _benchmark_ret50(conn, td)
    return DashboardData(
        db_path=path, trade_date=td, rows=tuple(rows),
        include_rejects=include_rejects,
        benchmark_return_50d=bench, universe_counts=counts,
    )
