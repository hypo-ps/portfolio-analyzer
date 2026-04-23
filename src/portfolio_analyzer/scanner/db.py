from __future__ import annotations

import bisect
import datetime as dt
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from portfolio_analyzer import config as cfg
from portfolio_analyzer.scanner.bhavcopy import BhavRow
from portfolio_analyzer.scanner.corp_actions import (
    PRICE_ADJUSTING_ACTIONS,
    CorpAction,
)

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
    """
    CREATE TABLE IF NOT EXISTS corporate_actions (
        isin TEXT NOT NULL,
        ex_date TEXT NOT NULL,
        action_type TEXT NOT NULL,
        ratio_num REAL,
        ratio_den REAL,
        price_factor REAL NOT NULL DEFAULT 1.0,
        raw_subject TEXT NOT NULL,
        symbol TEXT NOT NULL,
        name TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'NSE_CA_API',
        ingested_at TEXT NOT NULL,
        PRIMARY KEY (isin, ex_date, action_type, raw_subject)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ca_isin_exdate ON corporate_actions(isin, ex_date)",
    "CREATE INDEX IF NOT EXISTS idx_ca_action_type ON corporate_actions(action_type)",
    """
    CREATE TABLE IF NOT EXISTS cumulative_adjustments (
        isin TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        factor REAL NOT NULL,
        PRIMARY KEY (isin, trade_date)
    )
    """,
    """
    CREATE VIEW IF NOT EXISTS adjusted_market_data AS
    SELECT
        m.isin,
        m.trade_date,
        m.open  * COALESCE(cf.factor, 1.0) AS adj_open,
        m.high  * COALESCE(cf.factor, 1.0) AS adj_high,
        m.low   * COALESCE(cf.factor, 1.0) AS adj_low,
        m.close * COALESCE(cf.factor, 1.0) AS adj_close,
        CAST(m.volume / COALESCE(cf.factor, 1.0) AS INTEGER) AS adj_volume,
        m.open, m.high, m.low, m.close, m.volume,
        COALESCE(cf.factor, 1.0) AS adjustment_factor
    FROM market_data m
    LEFT JOIN cumulative_adjustments cf
      ON cf.isin = m.isin AND cf.trade_date = m.trade_date
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
        "corporate_actions": corp_actions_summary(conn),
    }


def upsert_corp_actions(
    conn: sqlite3.Connection, actions: Iterable[CorpAction],
    *, source: str = "NSE_CA_API",
) -> int:
    now = dt.datetime.now().isoformat()
    payload = [
        (
            a.isin, a.ex_date.isoformat(), a.action_type,
            a.ratio_num, a.ratio_den, a.price_factor,
            a.raw_subject, a.symbol, a.name, source, now,
        )
        for a in actions
    ]
    if not payload:
        return 0
    conn.executemany(
        """
        INSERT INTO corporate_actions
            (isin, ex_date, action_type, ratio_num, ratio_den, price_factor,
             raw_subject, symbol, name, source, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(isin, ex_date, action_type, raw_subject) DO UPDATE SET
            ratio_num = excluded.ratio_num,
            ratio_den = excluded.ratio_den,
            price_factor = excluded.price_factor,
            symbol = excluded.symbol,
            name = excluded.name,
            source = excluded.source,
            ingested_at = excluded.ingested_at
        """,
        payload,
    )
    return len(payload)


def rebuild_cumulative_adjustments(conn: sqlite3.Connection) -> int:
    """Materialize cumulative price-adjustment factors for every (isin, trade_date).

    For a trade_date t, factor(t) = product of price_factor for every
    price-adjusting CA with ex_date > t (strictly after). A bar on ex_date
    itself is already ex-action and gets factor 1.0.
    Only rows where factor != 1.0 are stored.
    """
    conn.execute("DELETE FROM cumulative_adjustments")
    placeholders = ",".join("?" * len(PRICE_ADJUSTING_ACTIONS))
    ca_rows = conn.execute(
        f"SELECT isin, ex_date, price_factor FROM corporate_actions "
        f"WHERE action_type IN ({placeholders}) ORDER BY isin, ex_date",
        tuple(sorted(PRICE_ADJUSTING_ACTIONS)),
    ).fetchall()

    by_isin: dict[str, list[tuple[str, float]]] = {}
    for isin, ex_date, pf in ca_rows:
        by_isin.setdefault(isin, []).append((ex_date, pf))

    insert_rows: list[tuple[str, str, float]] = []
    for isin, events in by_isin.items():
        trade_dates = [
            r[0] for r in conn.execute(
                "SELECT trade_date FROM market_data WHERE isin = ? ORDER BY trade_date",
                (isin,),
            ).fetchall()
        ]
        if not trade_dates:
            continue
        ex_dates = [e[0] for e in events]
        suffix = [1.0] * (len(events) + 1)
        for i in range(len(events) - 1, -1, -1):
            suffix[i] = suffix[i + 1] * events[i][1]
        for td in trade_dates:
            idx = bisect.bisect_right(ex_dates, td)
            cum = suffix[idx]
            if cum != 1.0:
                insert_rows.append((isin, td, cum))

    if insert_rows:
        conn.executemany(
            "INSERT INTO cumulative_adjustments (isin, trade_date, factor) "
            "VALUES (?, ?, ?)",
            insert_rows,
        )
    return len(insert_rows)


def corp_actions_summary(conn: sqlite3.Connection) -> dict[str, object]:
    total = conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0]
    by_type_rows = conn.execute(
        "SELECT action_type, COUNT(*) FROM corporate_actions "
        "GROUP BY action_type ORDER BY action_type"
    ).fetchall()
    span = conn.execute(
        "SELECT MIN(ex_date), MAX(ex_date) FROM corporate_actions"
    ).fetchone()
    adj_rows = conn.execute("SELECT COUNT(*) FROM cumulative_adjustments").fetchone()[0]
    return {
        "total": total,
        "by_type": {t: c for t, c in by_type_rows},
        "earliest_ex_date": span[0],
        "latest_ex_date": span[1],
        "adjusted_bars": adj_rows,
    }
