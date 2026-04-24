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
    """
    CREATE TABLE IF NOT EXISTS fundamentals_meta (
        isin TEXT NOT NULL,
        source TEXT NOT NULL,
        sector TEXT,
        industry TEXT,
        market_cap_cr REAL,
        current_price REAL,
        face_value REAL,
        book_value REAL,
        stock_pe REAL,
        dividend_yield REAL,
        roe_latest REAL,
        roce_latest REAL,
        high_52w REAL,
        low_52w REAL,
        promoter_holding REAL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (isin, source),
        FOREIGN KEY (isin) REFERENCES stock_master(isin)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financials_annual (
        isin TEXT NOT NULL,
        fiscal_year INTEGER NOT NULL,
        source TEXT NOT NULL,
        report_type TEXT NOT NULL DEFAULT 'consolidated',
        sales_cr REAL,
        expenses_cr REAL,
        operating_profit_cr REAL,
        opm_pct REAL,
        other_income_cr REAL,
        interest_cr REAL,
        depreciation_cr REAL,
        profit_before_tax_cr REAL,
        tax_pct REAL,
        net_profit_cr REAL,
        eps REAL,
        dividend_payout_pct REAL,
        equity_capital_cr REAL,
        reserves_cr REAL,
        borrowings_cr REAL,
        total_assets_cr REAL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (isin, fiscal_year, source, report_type),
        FOREIGN KEY (isin) REFERENCES stock_master(isin)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ratios_annual (
        isin TEXT NOT NULL,
        fiscal_year INTEGER NOT NULL,
        source TEXT NOT NULL,
        report_type TEXT NOT NULL DEFAULT 'consolidated',
        roce_pct REAL,
        debtor_days REAL,
        inventory_days REAL,
        days_payable REAL,
        cash_conversion_cycle REAL,
        working_capital_days REAL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (isin, fiscal_year, source, report_type),
        FOREIGN KEY (isin) REFERENCES stock_master(isin)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financials_quarterly (
        isin TEXT NOT NULL,
        period_end TEXT NOT NULL,
        source TEXT NOT NULL,
        report_type TEXT NOT NULL DEFAULT 'consolidated',
        sales_cr REAL,
        expenses_cr REAL,
        operating_profit_cr REAL,
        opm_pct REAL,
        other_income_cr REAL,
        interest_cr REAL,
        depreciation_cr REAL,
        profit_before_tax_cr REAL,
        tax_pct REAL,
        net_profit_cr REAL,
        eps REAL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (isin, period_end, source, report_type),
        FOREIGN KEY (isin) REFERENCES stock_master(isin)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fundamentals_ingestion_log (
        isin TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        detail TEXT,
        report_type TEXT,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (isin, source)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fin_annual_year ON financials_annual(fiscal_year)",
    "CREATE INDEX IF NOT EXISTS idx_fund_meta_sector ON fundamentals_meta(sector)",
    "CREATE INDEX IF NOT EXISTS idx_fin_quarterly_period ON financials_quarterly(period_end)",
    """
    CREATE TABLE IF NOT EXISTS vcp_candidates (
        isin TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        close REAL,
        pivot REAL,
        distance_to_pivot REAL,
        technical_score REAL,
        vcp_score REAL,
        fundamental_score REAL,
        readiness_score REAL,
        combined_score REAL,
        final_score REAL,
        decision TEXT NOT NULL,
        stage TEXT,
        reasons TEXT,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (isin, trade_date),
        FOREIGN KEY (isin) REFERENCES stock_master(isin)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_vcp_date_score ON vcp_candidates(trade_date, final_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_vcp_decision ON vcp_candidates(decision)",
    """
    CREATE TABLE IF NOT EXISTS index_data (
        index_symbol TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        close REAL NOT NULL,
        source TEXT NOT NULL DEFAULT 'YF',
        ingested_at TEXT NOT NULL,
        PRIMARY KEY (index_symbol, trade_date)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_index_data_date ON index_data(trade_date)",
]


# Per-table columns that may need adding when upgrading a pre-existing DB.
# SQLite has no "ADD COLUMN IF NOT EXISTS", so we check PRAGMA and add only
# missing columns from init_schema.
_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "vcp_candidates": [
        ("return_50d", "REAL"),
        ("benchmark_return_50d", "REAL"),
        ("rs_score", "REAL"),
    ],
}


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
    _apply_column_migrations(conn)


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    """Add any columns in ``_COLUMN_MIGRATIONS`` that are missing on existing
    tables (SQLite has no ``ADD COLUMN IF NOT EXISTS``)."""
    for table, cols in _COLUMN_MIGRATIONS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, col_type in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


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
        "fundamentals": fundamentals_summary(conn),
        "vcp": vcp_summary(conn),
        "indices": index_summary(conn),
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



_META_COLS = (
    "sector", "industry", "market_cap_cr", "current_price", "face_value",
    "book_value", "stock_pe", "dividend_yield", "roe_latest", "roce_latest",
    "high_52w", "low_52w", "promoter_holding",
)

_FIN_ANNUAL_COLS = (
    "sales_cr", "expenses_cr", "operating_profit_cr", "opm_pct",
    "other_income_cr", "interest_cr", "depreciation_cr",
    "profit_before_tax_cr", "tax_pct", "net_profit_cr", "eps",
    "dividend_payout_pct", "equity_capital_cr", "reserves_cr",
    "borrowings_cr", "total_assets_cr",
)

_RATIOS_ANNUAL_COLS = (
    "roce_pct", "debtor_days", "inventory_days", "days_payable",
    "cash_conversion_cycle", "working_capital_days",
)

_FIN_QUARTERLY_COLS = (
    "sales_cr", "expenses_cr", "operating_profit_cr", "opm_pct",
    "other_income_cr", "interest_cr", "depreciation_cr",
    "profit_before_tax_cr", "tax_pct", "net_profit_cr", "eps",
)


def upsert_fundamentals_meta(
    conn: sqlite3.Connection, isin: str, source: str, meta: dict[str, object],
) -> None:
    now = dt.datetime.now().isoformat()
    values = [meta.get(c) for c in _META_COLS]
    cols = ", ".join(("isin", "source", *_META_COLS, "fetched_at"))
    placeholders = ", ".join(["?"] * (len(_META_COLS) + 3))
    updates = ", ".join(f"{c} = excluded.{c}" for c in (*_META_COLS, "fetched_at"))
    conn.execute(
        f"INSERT INTO fundamentals_meta ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(isin, source) DO UPDATE SET {updates}",
        (isin, source, *values, now),
    )


def upsert_financials_annual(
    conn: sqlite3.Connection, isin: str, source: str, report_type: str,
    rows: Iterable[dict[str, object]],
) -> int:
    now = dt.datetime.now().isoformat()
    payload: list[tuple] = []
    for r in rows:
        year = r.get("fiscal_year")
        if year is None:
            continue
        payload.append((
            isin, int(year), source, report_type,
            *[r.get(c) for c in _FIN_ANNUAL_COLS], now,
        ))
    if not payload:
        return 0
    cols = ", ".join(
        ("isin", "fiscal_year", "source", "report_type", *_FIN_ANNUAL_COLS, "fetched_at")
    )
    placeholders = ", ".join(["?"] * (len(_FIN_ANNUAL_COLS) + 5))
    updates = ", ".join(f"{c} = excluded.{c}" for c in (*_FIN_ANNUAL_COLS, "fetched_at"))
    conn.executemany(
        f"INSERT INTO financials_annual ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(isin, fiscal_year, source, report_type) DO UPDATE SET {updates}",
        payload,
    )
    return len(payload)


def upsert_ratios_annual(
    conn: sqlite3.Connection, isin: str, source: str, report_type: str,
    rows: Iterable[dict[str, object]],
) -> int:
    now = dt.datetime.now().isoformat()
    payload: list[tuple] = []
    for r in rows:
        year = r.get("fiscal_year")
        if year is None:
            continue
        payload.append((
            isin, int(year), source, report_type,
            *[r.get(c) for c in _RATIOS_ANNUAL_COLS], now,
        ))
    if not payload:
        return 0
    cols = ", ".join(
        ("isin", "fiscal_year", "source", "report_type", *_RATIOS_ANNUAL_COLS, "fetched_at")
    )
    placeholders = ", ".join(["?"] * (len(_RATIOS_ANNUAL_COLS) + 5))
    updates = ", ".join(f"{c} = excluded.{c}" for c in (*_RATIOS_ANNUAL_COLS, "fetched_at"))
    conn.executemany(
        f"INSERT INTO ratios_annual ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(isin, fiscal_year, source, report_type) DO UPDATE SET {updates}",
        payload,
    )
    return len(payload)


def upsert_financials_quarterly(
    conn: sqlite3.Connection, isin: str, source: str, report_type: str,
    rows: Iterable[dict[str, object]],
) -> int:
    now = dt.datetime.now().isoformat()
    payload: list[tuple] = []
    for r in rows:
        period = r.get("period_end")
        if not period:
            continue
        payload.append((
            isin, str(period), source, report_type,
            *[r.get(c) for c in _FIN_QUARTERLY_COLS], now,
        ))
    if not payload:
        return 0
    cols = ", ".join(
        ("isin", "period_end", "source", "report_type", *_FIN_QUARTERLY_COLS, "fetched_at")
    )
    placeholders = ", ".join(["?"] * (len(_FIN_QUARTERLY_COLS) + 5))
    updates = ", ".join(f"{c} = excluded.{c}" for c in (*_FIN_QUARTERLY_COLS, "fetched_at"))
    conn.executemany(
        f"INSERT INTO financials_quarterly ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(isin, period_end, source, report_type) DO UPDATE SET {updates}",
        payload,
    )
    return len(payload)


def record_fundamentals_ingestion(
    conn: sqlite3.Connection, isin: str, source: str, status: str,
    *, detail: str | None = None, report_type: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO fundamentals_ingestion_log
            (isin, source, status, detail, report_type, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(isin, source) DO UPDATE SET
            status = excluded.status,
            detail = excluded.detail,
            report_type = excluded.report_type,
            fetched_at = excluded.fetched_at
        """,
        (isin, source, status, detail, report_type, dt.datetime.now().isoformat()),
    )


def last_fundamentals_fetch(
    conn: sqlite3.Connection, isin: str, source: str,
) -> dt.datetime | None:
    row = conn.execute(
        "SELECT fetched_at FROM fundamentals_ingestion_log "
        "WHERE isin = ? AND source = ? AND status = 'ok'",
        (isin, source),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return dt.datetime.fromisoformat(row[0])
    except ValueError:
        return None


def fundamentals_summary(conn: sqlite3.Connection) -> dict[str, object]:
    covered = conn.execute("SELECT COUNT(*) FROM fundamentals_meta").fetchone()[0]
    annual = conn.execute("SELECT COUNT(*) FROM financials_annual").fetchone()[0]
    ratios = conn.execute("SELECT COUNT(*) FROM ratios_annual").fetchone()[0]
    quarterly = conn.execute("SELECT COUNT(*) FROM financials_quarterly").fetchone()[0]
    by_status_rows = conn.execute(
        "SELECT status, COUNT(*) FROM fundamentals_ingestion_log "
        "GROUP BY status ORDER BY status"
    ).fetchall()
    last = conn.execute(
        "SELECT MAX(fetched_at) FROM fundamentals_ingestion_log"
    ).fetchone()[0]
    return {
        "companies_covered": covered,
        "annual_rows": annual,
        "ratios_rows": ratios,
        "quarterly_rows": quarterly,
        "by_status": {s: c for s, c in by_status_rows},
        "last_fetch": last,
    }



_VCP_COLS = (
    "symbol", "close", "pivot", "distance_to_pivot",
    "technical_score", "vcp_score", "fundamental_score", "readiness_score",
    "combined_score", "final_score", "decision", "stage", "reasons",
    "return_50d", "benchmark_return_50d", "rs_score",
)


def upsert_vcp_candidates(
    conn: sqlite3.Connection, rows: Iterable[dict[str, object]],
) -> int:
    """Insert or replace VCP scan results keyed on (isin, trade_date)."""
    now = dt.datetime.now().isoformat()
    payload: list[tuple] = []
    for r in rows:
        isin = r.get("isin")
        td = r.get("trade_date")
        if not isin or not td:
            continue
        td_iso = td.isoformat() if hasattr(td, "isoformat") else str(td)
        payload.append((
            isin, td_iso, *[r.get(c) for c in _VCP_COLS], now,
        ))
    if not payload:
        return 0
    cols = ", ".join(("isin", "trade_date", *_VCP_COLS, "computed_at"))
    placeholders = ", ".join(["?"] * (len(_VCP_COLS) + 3))
    updates = ", ".join(f"{c} = excluded.{c}" for c in (*_VCP_COLS, "computed_at"))
    conn.executemany(
        f"INSERT INTO vcp_candidates ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(isin, trade_date) DO UPDATE SET {updates}",
        payload,
    )
    return len(payload)


def vcp_summary(conn: sqlite3.Connection) -> dict[str, object]:
    total = conn.execute("SELECT COUNT(*) FROM vcp_candidates").fetchone()[0]
    by_decision_rows = conn.execute(
        "SELECT decision, COUNT(*) FROM vcp_candidates "
        "GROUP BY decision ORDER BY decision"
    ).fetchall()
    last_date = conn.execute(
        "SELECT MAX(trade_date) FROM vcp_candidates"
    ).fetchone()[0]
    last_total = 0
    if last_date:
        last_total = conn.execute(
            "SELECT COUNT(*) FROM vcp_candidates WHERE trade_date = ?",
            (last_date,),
        ).fetchone()[0]
    return {
        "total": total,
        "by_decision": {d: c for d, c in by_decision_rows},
        "latest_scan_date": last_date,
        "latest_scan_rows": last_total,
    }


def upsert_index_data(
    conn: sqlite3.Connection,
    index_symbol: str,
    rows: Iterable[tuple[dt.date, float]],
    *,
    source: str = "YF",
) -> int:
    """Insert or replace (index_symbol, trade_date, close) rows."""
    now = dt.datetime.now().isoformat()
    payload = [
        (index_symbol, td.isoformat(), float(close), source, now)
        for td, close in rows
        if close is not None and float(close) > 0
    ]
    if not payload:
        return 0
    conn.executemany(
        "INSERT INTO index_data (index_symbol, trade_date, close, source, ingested_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(index_symbol, trade_date) DO UPDATE SET "
        "close = excluded.close, source = excluded.source, "
        "ingested_at = excluded.ingested_at",
        payload,
    )
    return len(payload)


def load_index_closes(
    conn: sqlite3.Connection, index_symbol: str, *, as_of: dt.date | None = None,
    lookback: int | None = None,
) -> list[tuple[dt.date, float]]:
    """Return (trade_date, close) rows for an index, oldest -> newest."""
    params: list = [index_symbol]
    sql = "SELECT trade_date, close FROM index_data WHERE index_symbol = ?"
    if as_of is not None:
        sql += " AND trade_date <= ?"
        params.append(as_of.isoformat())
    sql += " ORDER BY trade_date"
    rows = conn.execute(sql, params).fetchall()
    pairs = [(dt.date.fromisoformat(d), float(c)) for d, c in rows]
    if lookback is not None and len(pairs) > lookback:
        pairs = pairs[-lookback:]
    return pairs


def index_summary(conn: sqlite3.Connection) -> dict[str, object]:
    totals = conn.execute(
        "SELECT index_symbol, COUNT(*), MIN(trade_date), MAX(trade_date) "
        "FROM index_data GROUP BY index_symbol ORDER BY index_symbol"
    ).fetchall()
    return {
        "by_index": [
            {"index": sym, "bars": n, "first": first, "last": last}
            for sym, n, first, last in totals
        ],
    }
