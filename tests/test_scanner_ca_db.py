from __future__ import annotations

import datetime as dt
from pathlib import Path

from portfolio_analyzer.scanner import db as sdb
from portfolio_analyzer.scanner.bhavcopy import BhavRow
from portfolio_analyzer.scanner.corp_actions import (
    ACTION_BONUS,
    ACTION_DIVIDEND,
    ACTION_SPLIT,
    CorpAction,
)


def _bar(isin: str, d: dt.date, close: float = 100.0, volume: int = 1000) -> BhavRow:
    return BhavRow(
        trade_date=d, isin=isin, symbol="TST", name="Test Ltd", series="EQ",
        open=close, high=close, low=close, close=close,
        prev_close=close, volume=volume, turnover=close * volume, trades=10,
    )


def _ca(
    isin: str, ex_date: dt.date, action_type: str, *,
    price_factor: float = 1.0, subject: str = "sample",
    ratio_num: float | None = None, ratio_den: float | None = None,
) -> CorpAction:
    return CorpAction(
        isin=isin, ex_date=ex_date, action_type=action_type,
        ratio_num=ratio_num, ratio_den=ratio_den, price_factor=price_factor,
        raw_subject=subject, symbol="TST", name="Test Ltd",
    )


def _open(tmp_path: Path):
    db_path = tmp_path / "s.db"
    conn_ctx = sdb.open_db(db_path)
    return db_path, conn_ctx


def test_schema_creates_ca_tables_and_view(tmp_path: Path):
    with sdb.open_db(tmp_path / "s.db") as conn:
        sdb.init_schema(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        views = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()}
    assert "corporate_actions" in tables
    assert "cumulative_adjustments" in tables
    assert "adjusted_market_data" in views


def test_upsert_corp_actions_is_idempotent(tmp_path: Path):
    d = dt.date(2024, 6, 15)
    a = _ca("INE000A01001", d, ACTION_BONUS, price_factor=0.5,
            subject="Bonus 1:1", ratio_num=1, ratio_den=1)
    with sdb.open_db(tmp_path / "s.db") as conn:
        sdb.init_schema(conn)
        sdb.upsert_corp_actions(conn, [a])
        sdb.upsert_corp_actions(conn, [a])
        n = conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0]
    assert n == 1


def test_upsert_allows_two_rows_per_date_with_different_subjects(tmp_path: Path):
    d = dt.date(2024, 6, 15)
    bonus = _ca("INE000A01001", d, ACTION_BONUS, price_factor=0.2,
                subject="Bonus 4:1")
    split = _ca("INE000A01001", d, ACTION_SPLIT, price_factor=0.5,
                subject="Face Value Split From Rs 10 To Rs 5")
    with sdb.open_db(tmp_path / "s.db") as conn:
        sdb.init_schema(conn)
        sdb.upsert_corp_actions(conn, [bonus, split])
        n = conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0]
    assert n == 2


def test_rebuild_cumulative_adjustments_split_one_to_ten(tmp_path: Path):
    isin = "INE000A01001"
    ex = dt.date(2024, 6, 15)
    with sdb.open_db(tmp_path / "s.db") as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [_bar(isin, dt.date(2024, 6, 1))])
        sdb.upsert_market_data(conn, [
            _bar(isin, dt.date(2024, 6, 1), close=1000.0, volume=100),
            _bar(isin, dt.date(2024, 6, 14), close=1000.0, volume=100),
            _bar(isin, ex, close=100.0, volume=1000),
            _bar(isin, dt.date(2024, 6, 16), close=100.0, volume=1000),
        ])
        sdb.upsert_corp_actions(conn, [_ca(isin, ex, ACTION_SPLIT, price_factor=0.1)])
        n = sdb.rebuild_cumulative_adjustments(conn)
        rows = conn.execute(
            "SELECT trade_date, factor FROM cumulative_adjustments "
            "WHERE isin = ? ORDER BY trade_date", (isin,),
        ).fetchall()
    # Only dates strictly BEFORE ex_date get the 0.1 factor.
    assert n == 2
    assert rows == [("2024-06-01", 0.1), ("2024-06-14", 0.1)]


def test_rebuild_chains_bonus_then_split(tmp_path: Path):
    isin = "INE000A01001"
    bonus_ex = dt.date(2024, 3, 1)   # 1:1 → factor 0.5
    split_ex = dt.date(2024, 9, 1)   # Rs 10 → Re 1 → factor 0.1
    with sdb.open_db(tmp_path / "s.db") as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [_bar(isin, dt.date(2024, 1, 1))])
        sdb.upsert_market_data(conn, [
            _bar(isin, dt.date(2024, 1, 15)),
            _bar(isin, dt.date(2024, 6, 1)),
            _bar(isin, dt.date(2024, 10, 1)),
        ])
        sdb.upsert_corp_actions(conn, [
            _ca(isin, bonus_ex, ACTION_BONUS, price_factor=0.5, subject="Bonus 1:1"),
            _ca(isin, split_ex, ACTION_SPLIT, price_factor=0.1, subject="Split"),
        ])
        sdb.rebuild_cumulative_adjustments(conn)
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT trade_date, factor FROM cumulative_adjustments WHERE isin = ?",
            (isin,),
        ).fetchall()}
    # Before bonus: both events apply → 0.5 * 0.1 = 0.05
    # Between bonus and split: only split applies → 0.1
    # After split: no event applies → not stored
    assert rows["2024-01-15"] == 0.05
    assert rows["2024-06-01"] == 0.1
    assert "2024-10-01" not in rows


def test_rebuild_ignores_dividend_and_other(tmp_path: Path):
    isin = "INE000A01001"
    with sdb.open_db(tmp_path / "s.db") as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [_bar(isin, dt.date(2024, 1, 1))])
        sdb.upsert_market_data(conn, [_bar(isin, dt.date(2024, 1, 15))])
        sdb.upsert_corp_actions(conn, [
            _ca(isin, dt.date(2024, 6, 1), ACTION_DIVIDEND,
                price_factor=1.0, subject="Interim Dividend - Rs 5"),
        ])
        n = sdb.rebuild_cumulative_adjustments(conn)
    assert n == 0


def test_adjusted_market_data_view_applies_factor(tmp_path: Path):
    isin = "INE000A01001"
    ex = dt.date(2024, 6, 15)
    with sdb.open_db(tmp_path / "s.db") as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [_bar(isin, dt.date(2024, 6, 1))])
        sdb.upsert_market_data(conn, [
            _bar(isin, dt.date(2024, 6, 1), close=1000.0, volume=100),
            _bar(isin, ex, close=100.0, volume=1000),
        ])
        sdb.upsert_corp_actions(conn, [
            _ca(isin, ex, ACTION_SPLIT, price_factor=0.1, subject="Split"),
        ])
        sdb.rebuild_cumulative_adjustments(conn)
        rows = conn.execute(
            "SELECT trade_date, adj_close, adj_volume, adjustment_factor "
            "FROM adjusted_market_data WHERE isin = ? ORDER BY trade_date",
            (isin,),
        ).fetchall()
    assert rows[0] == ("2024-06-01", 100.0, 1000, 0.1)      # pre-split adjusted
    assert rows[1] == (ex.isoformat(), 100.0, 1000, 1.0)     # on/after ex_date unchanged


def test_corp_actions_summary_reports_counts(tmp_path: Path):
    isin = "INE000A01001"
    with sdb.open_db(tmp_path / "s.db") as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [_bar(isin, dt.date(2024, 1, 1))])
        sdb.upsert_market_data(conn, [_bar(isin, dt.date(2024, 1, 15))])
        sdb.upsert_corp_actions(conn, [
            _ca(isin, dt.date(2024, 2, 1), ACTION_BONUS, price_factor=0.5, subject="Bonus 1:1"),
            _ca(isin, dt.date(2024, 3, 1), ACTION_DIVIDEND, subject="Interim Dividend - Rs 5"),
        ])
        sdb.rebuild_cumulative_adjustments(conn)
        summary = sdb.corp_actions_summary(conn)
    assert summary["total"] == 2
    assert summary["by_type"] == {ACTION_BONUS: 1, ACTION_DIVIDEND: 1}
    assert summary["earliest_ex_date"] == "2024-02-01"
    assert summary["latest_ex_date"] == "2024-03-01"
    assert summary["adjusted_bars"] == 1
