from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import requests
from click.testing import CliRunner

from portfolio_analyzer import cli
from portfolio_analyzer.scanner import ca_ingest, db as sdb
from portfolio_analyzer.scanner.bhavcopy import BhavRow
from portfolio_analyzer.scanner.corp_actions import (
    ACTION_BONUS,
    ACTION_SPLIT,
    CorpAction,
)


def _bar(isin: str, d: dt.date) -> BhavRow:
    return BhavRow(
        trade_date=d, isin=isin, symbol="TST", name="Test Ltd", series="EQ",
        open=100.0, high=100.0, low=100.0, close=100.0,
        prev_close=100.0, volume=1000, turnover=100000.0, trades=10,
    )


def _actions(isin: str = "INE000A01001") -> list[CorpAction]:
    return [
        CorpAction(
            isin=isin, ex_date=dt.date(2024, 6, 15), action_type=ACTION_BONUS,
            ratio_num=1.0, ratio_den=1.0, price_factor=0.5,
            raw_subject="Bonus 1:1", symbol="TST", name="Test Ltd",
        ),
        CorpAction(
            isin=isin, ex_date=dt.date(2024, 9, 1), action_type=ACTION_SPLIT,
            ratio_num=5.0, ratio_den=10.0, price_factor=0.5,
            raw_subject="Face Value Split From Rs 10 To Rs 5",
            symbol="TST", name="Test Ltd",
        ),
    ]


def _seed_bars(db_path: Path) -> None:
    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [_bar("INE000A01001", dt.date(2024, 1, 1))])
        sdb.upsert_market_data(conn, [
            _bar("INE000A01001", dt.date(2024, 1, 15)),
            _bar("INE000A01001", dt.date(2024, 7, 1)),
        ])


def test_ingest_ca_range_happy_path(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_bars(db_path)
    mocker.patch.object(ca_ingest.ca, "fetch_and_parse", return_value=_actions())
    result = ca_ingest.ingest_ca_range(
        dt.date(2024, 1, 1), dt.date(2024, 12, 31), db_path=db_path,
    )
    assert result.status == "ingested"
    assert result.fetched == 2
    assert result.stored == 2
    assert result.adjusted_bars > 0
    with sdb.open_db(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0]
    assert n == 2


def test_ingest_ca_range_skips_rebuild_when_requested(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_bars(db_path)
    mocker.patch.object(ca_ingest.ca, "fetch_and_parse", return_value=_actions())
    result = ca_ingest.ingest_ca_range(
        dt.date(2024, 1, 1), dt.date(2024, 12, 31), db_path=db_path, rebuild=False,
    )
    assert result.adjusted_bars == 0
    with sdb.open_db(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM cumulative_adjustments").fetchone()[0]
    assert n == 0


def test_ingest_ca_range_no_data(tmp_path: Path, mocker):
    mocker.patch.object(ca_ingest.ca, "fetch_and_parse", return_value=[])
    result = ca_ingest.ingest_ca_range(
        dt.date(2024, 1, 1), dt.date(2024, 1, 31), db_path=tmp_path / "s.db",
    )
    assert result.status == "no_data"


def test_ingest_ca_range_handles_network_error(tmp_path: Path, mocker):
    mocker.patch.object(
        ca_ingest.ca, "fetch_and_parse",
        side_effect=requests.ConnectionError("boom"),
    )
    result = ca_ingest.ingest_ca_range(
        dt.date(2024, 1, 1), dt.date(2024, 1, 31), db_path=tmp_path / "s.db",
    )
    assert result.status == "error"


def test_ingest_ca_range_rejects_reversed_window(tmp_path: Path):
    with pytest.raises(ValueError):
        ca_ingest.ingest_ca_range(
            dt.date(2024, 12, 31), dt.date(2024, 1, 1), db_path=tmp_path / "s.db",
        )


def test_rebuild_adjustments_standalone(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_bars(db_path)
    mocker.patch.object(ca_ingest.ca, "fetch_and_parse", return_value=_actions())
    ca_ingest.ingest_ca_range(
        dt.date(2024, 1, 1), dt.date(2024, 12, 31), db_path=db_path, rebuild=False,
    )
    n = ca_ingest.rebuild_adjustments(db_path=db_path)
    assert n > 0


def test_cli_ca_ingest_smoke(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_bars(db_path)
    mocker.patch.object(ca_ingest.ca, "fetch_and_parse", return_value=_actions())
    runner = CliRunner()
    res = runner.invoke(cli.main, [
        "scanner", "ca-ingest",
        "--start", "2024-01-01", "--end", "2024-12-31",
        "--db", str(db_path),
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["status"] == "ingested"
    assert payload["stored"] == 2
    assert payload["adjusted_bars"] > 0


def test_cli_ca_rebuild_adjustments(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_bars(db_path)
    mocker.patch.object(ca_ingest.ca, "fetch_and_parse", return_value=_actions())
    ca_ingest.ingest_ca_range(
        dt.date(2024, 1, 1), dt.date(2024, 12, 31), db_path=db_path, rebuild=False,
    )
    runner = CliRunner()
    res = runner.invoke(cli.main, [
        "scanner", "ca-rebuild-adjustments", "--db", str(db_path),
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["adjusted_bars"] > 0


def test_cli_status_includes_corporate_actions_section(tmp_path: Path, mocker):
    db_path = tmp_path / "s.db"
    _seed_bars(db_path)
    mocker.patch.object(ca_ingest.ca, "fetch_and_parse", return_value=_actions())
    ca_ingest.ingest_ca_range(
        dt.date(2024, 1, 1), dt.date(2024, 12, 31), db_path=db_path,
    )
    runner = CliRunner()
    res = runner.invoke(cli.main, ["scanner", "status", "--db", str(db_path)])
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["corporate_actions"]["total"] == 2
    assert payload["corporate_actions"]["adjusted_bars"] > 0
