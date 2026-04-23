from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from portfolio_analyzer import config as cfg
from portfolio_analyzer.scanner.bhavcopy import BhavRow

log = logging.getLogger(__name__)

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS stock_master (
        isin TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        name TEXT NOT NULL,
        series TEXT NOT NULL,
        exchange TEXT NOT NULL DEFAULT 'NSE',
        first_seen_date TEXT NOT NULL,
        last_seen_date TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_stock_master_symbol ON stock_master(symbol)",
    """
    CREATE TABLE IF NOT EXISTS market_data (
        isin TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        prev_close REAL,
        volume INTEGER NOT NULL,
        turnover REAL,
        trades INTEGER,
        PRIMARY KEY (isin, trade_date),
        FOREIGN KEY (isin) REFERENCES stock_master(isin)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_market_data_date ON market_data(trade_date)",
    """
    CREATE TABLE IF NOT EXISTS ingestion_log (
        trade_date TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        rows_ingested INTEGER NOT NULL,
        ingested_at TEXT NOT NULL
    )
    """,
]


def default_db_path(data_dir: Path | None = None) -> Path:
    data_dir = data_dir or cfg.DATA_DIR
    return data_dir / cfg.SCANNER_DB_FILE


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection with sensible pragmas; commit on clean exit."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)


def upsert_stock_master(conn: sqlite3.Connection, rows: Iterable[BhavRow]) -> int:
    """Insert new ISINs, refresh symbol/name/series and last_seen_date for known ones."""
    payload = [
        (
            r.isin, r.symbol, r.name, r.series,
            r.trade_date.isoformat(), r.trade_date.isoformat(),
        )
        for r in rows
    ]
    if not payload:
        return 0
    conn.executemany(
        """
        INSERT INTO stock_master
            (isin, symbol, name, series, first_seen_date, last_seen_date)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(isin) DO UPDATE SET
            symbol = excluded.symbol,
            name = excluded.name,
            series = excluded.series,
            last_seen_date = MAX(stock_master.last_seen_date, excluded.last_seen_date),
            first_seen_date = MIN(stock_master.first_seen_date, excluded.first_seen_date)
        """,
        payload,
    )
    return len(payload)


def upsert_market_data(conn: sqlite3.Connection, rows: Iterable[BhavRow]) -> int:
    payload = [
        (
            r.isin, r.trade_date.isoformat(),
            r.open, r.high, r.low, r.close, r.prev_close,
            r.volume, r.turnover, r.trades,
        )
        for r in rows
    ]
    if not payload:
        return 0
    conn.executemany(
        """
        INSERT INTO market_data
            (isin, trade_date, open, high, low, close, prev_close,
             volume, turnover, trades)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(isin, trade_date) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            prev_close = excluded.prev_close,
            volume = excluded.volume,
            turnover = excluded.turnover,
            trades = excluded.trades
        """,
        payload,
    )
    return len(payload)


def record_ingestion(
    conn: sqlite3.Connection, trade_date: dt.date, source: str, rows_ingested: int,
) -> None:
    conn.execute(
        """
        INSERT INTO ingestion_log (trade_date, source, rows_ingested, ingested_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(trade_date) DO UPDATE SET
            source = excluded.source,
            rows_ingested = excluded.rows_ingested,
            ingested_at = excluded.ingested_at
        """,
        (trade_date.isoformat(), source, rows_ingested, dt.datetime.now().isoformat()),
    )


def is_ingested(conn: sqlite3.Connection, trade_date: dt.date) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM ingestion_log WHERE trade_date = ?",
        (trade_date.isoformat(),),
    )
    return cur.fetchone() is not None


def ingestion_summary(conn: sqlite3.Connection) -> dict[str, object]:
    stocks = conn.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0]
    bars = conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    days = conn.execute("SELECT COUNT(*) FROM ingestion_log").fetchone()[0]
    last = conn.execute(
        "SELECT trade_date, rows_ingested, ingested_at FROM ingestion_log "
        "ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    return {
        "stocks": stocks,
        "bars": bars,
        "days_ingested": days,
        "latest": None if last is None else {
            "trade_date": last[0], "rows": last[1], "ingested_at": last[2],
        },
    }
