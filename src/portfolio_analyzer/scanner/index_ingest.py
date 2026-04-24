"""Index (NIFTY 500 / NIFTY 50) close-series ingestion into the scanner DB.

Used by the VCP dashboard to compute the Phase 0 relative-strength metric
(``rs = stock_ret50 - benchmark_ret50``) entirely offline once ingested.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from portfolio_analyzer import config as cfg
from portfolio_analyzer.scanner.db import (
    default_db_path, init_schema, open_db, upsert_index_data,
)
from portfolio_analyzer.yf_fetch import fetch_daily_closes

logger = logging.getLogger(__name__)

# Canonical keys stored in ``index_data.index_symbol``. Kept short/stable so
# downstream consumers don't depend on yfinance naming.
INDEX_NIFTY500 = "NIFTY500"
INDEX_NIFTY50 = "NIFTY50"

_YF_TICKER = {
    INDEX_NIFTY500: cfg.NIFTY500_YF_SYMBOL,
    INDEX_NIFTY50: cfg.NIFTY50_YF_SYMBOL,
}


@dataclass
class IndexIngestResult:
    index_symbol: str
    yf_ticker: str
    rows_fetched: int
    rows_upserted: int
    first_date: dt.date | None
    last_date: dt.date | None


def ingest_index(
    index_symbol: str = INDEX_NIFTY500,
    *,
    days: int = 500,
    db_path: Path | None = None,
) -> IndexIngestResult:
    """Fetch daily closes for ``index_symbol`` via yfinance and upsert them.

    ``days`` is the trailing calendar-day window (yfinance chooses actual bars
    based on what's available). 500 covers well over 1y of trading days, more
    than enough for the 50-bar RS window plus buffer.
    """
    if index_symbol not in _YF_TICKER:
        raise ValueError(f"unknown index_symbol: {index_symbol!r}")
    ticker = _YF_TICKER[index_symbol]

    closes = fetch_daily_closes([ticker], days=days)
    series = closes.get(ticker)
    if series is None or series.empty:
        return IndexIngestResult(
            index_symbol=index_symbol, yf_ticker=ticker,
            rows_fetched=0, rows_upserted=0,
            first_date=None, last_date=None,
        )

    rows: list[tuple[dt.date, float]] = []
    for ts, value in series.items():
        td = ts.date() if hasattr(ts, "date") else ts
        rows.append((td, float(value)))

    path = db_path or default_db_path()
    with open_db(path) as conn:
        init_schema(conn)
        upserted = upsert_index_data(conn, index_symbol, rows, source="YF")

    first = rows[0][0] if rows else None
    last = rows[-1][0] if rows else None
    logger.info(
        "index-ingest: %s (%s) fetched=%d upserted=%d [%s..%s]",
        index_symbol, ticker, len(rows), upserted, first, last,
    )
    return IndexIngestResult(
        index_symbol=index_symbol, yf_ticker=ticker,
        rows_fetched=len(rows), rows_upserted=upserted,
        first_date=first, last_date=last,
    )
