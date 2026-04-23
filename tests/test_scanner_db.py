from __future__ import annotations

import datetime as dt
from pathlib import Path

from portfolio_analyzer.scanner import db as sdb
from portfolio_analyzer.scanner.bhavcopy import BhavRow


def _row(
    *, isin: str = "INE009A01021", symbol: str = "INFY", name: str = "INFOSYS LIMITED",
    series: str = "EQ", date: dt.date = dt.date(2026, 4, 22),
    open_: float = 1295.0, high: float = 1297.7, low: float = 1255.9, close: float = 1268.6,
    prev: float | None = 1313.2, volume: int = 20088378,
    turnover: float | None = 25498272367.7, trades: int | None = 477139,
) -> BhavRow:
    return BhavRow(
        trade_date=date, isin=isin, symbol=symbol, name=name, series=series,
        open=open_, high=high, low=low, close=close, prev_close=prev,
        volume=volume, turnover=turnover, trades=trades,
    )


def test_init_schema_creates_tables(tmp_path: Path):
    db_path = tmp_path / "s.db"
    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [r[0] for r in cur.fetchall()]
    assert "stock_master" in tables
    assert "market_data" in tables
    assert "ingestion_log" in tables


def test_upsert_stock_master_inserts_then_refreshes(tmp_path: Path):
    db_path = tmp_path / "s.db"
    d1, d2 = dt.date(2026, 4, 21), dt.date(2026, 4, 22)
    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [_row(date=d1, name="OLD NAME")])
        sdb.upsert_stock_master(conn, [_row(date=d2, name="INFOSYS LIMITED")])
        row = conn.execute(
            "SELECT symbol, name, first_seen_date, last_seen_date "
            "FROM stock_master WHERE isin = ?",
            ("INE009A01021",),
        ).fetchone()
    assert row == ("INFY", "INFOSYS LIMITED", d1.isoformat(), d2.isoformat())


def test_upsert_market_data_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "s.db"
    r = _row()
    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [r])
        sdb.upsert_market_data(conn, [r])
        sdb.upsert_market_data(conn, [r])
        count = conn.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    assert count == 1


def test_upsert_market_data_updates_values_on_conflict(tmp_path: Path):
    db_path = tmp_path / "s.db"
    r1 = _row(close=100.0, volume=1000)
    r2 = _row(close=105.5, volume=2500)
    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [r1])
        sdb.upsert_market_data(conn, [r1])
        sdb.upsert_market_data(conn, [r2])
        close, volume = conn.execute(
            "SELECT close, volume FROM market_data WHERE isin = ? AND trade_date = ?",
            (r1.isin, r1.trade_date.isoformat()),
        ).fetchone()
    assert (close, volume) == (105.5, 2500)


def test_ingestion_log_record_and_query(tmp_path: Path):
    db_path = tmp_path / "s.db"
    d = dt.date(2026, 4, 22)
    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        assert sdb.is_ingested(conn, d) is False
        sdb.record_ingestion(conn, d, "NSE_UDIFF", 2600)
        assert sdb.is_ingested(conn, d) is True
        sdb.record_ingestion(conn, d, "NSE_UDIFF", 2700)
        count, rows = conn.execute(
            "SELECT COUNT(*), MAX(rows_ingested) FROM ingestion_log"
        ).fetchone()
    assert (count, rows) == (1, 2700)


def test_ingestion_summary_reports_counts(tmp_path: Path):
    db_path = tmp_path / "s.db"
    d = dt.date(2026, 4, 22)
    r = _row(date=d)
    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [r])
        sdb.upsert_market_data(conn, [r])
        sdb.record_ingestion(conn, d, "NSE_UDIFF", 1)
        summary = sdb.ingestion_summary(conn)
    assert summary["stocks"] == 1
    assert summary["bars"] == 1
    assert summary["days_ingested"] == 1
    assert summary["latest"]["trade_date"] == d.isoformat()
    assert summary["latest"]["rows"] == 1


def test_default_db_path_uses_config(tmp_path: Path):
    path = sdb.default_db_path(data_dir=tmp_path)
    assert path == tmp_path / "scanner.db"
