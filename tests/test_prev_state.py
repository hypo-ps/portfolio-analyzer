from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from portfolio_analyzer.cli import _load_previous_states


def _write(path: Path, stocks: list[dict], pending: list[dict] | None = None) -> None:
    payload: dict = {"stocks": stocks}
    if pending is not None:
        payload["pending_exits"] = pending
    path.write_text(json.dumps(payload))


def test_returns_empty_when_dir_missing(tmp_path: Path):
    assert _load_previous_states(tmp_path / "nope", dt.date(2026, 4, 22)) == ({}, {})


def test_returns_empty_when_no_prior_json(tmp_path: Path):
    _write(tmp_path / "2026-04-22.json", [{"symbol": "INFY", "decision": "HOLD"}])
    # today == file date -> no strictly-prior file
    assert _load_previous_states(tmp_path, dt.date(2026, 4, 22)) == ({}, {})


def test_loads_decisions_from_most_recent_prior_file(tmp_path: Path):
    _write(tmp_path / "2026-04-18.json", [
        {"symbol": "INFY", "decision": "HOLD"},
        {"symbol": "TCS", "decision": "REDUCE"},
    ])
    _write(tmp_path / "2026-04-21.json", [
        {"symbol": "INFY", "decision": "REDUCE"},
        {"symbol": "TCS", "decision": "EXIT"},
    ])
    states, pending = _load_previous_states(tmp_path, dt.date(2026, 4, 22))
    assert states == {"INFY": "REDUCE", "TCS": "EXIT"}
    assert pending == {}


def test_stale_file_beyond_max_age_is_ignored(tmp_path: Path):
    _write(tmp_path / "2026-04-10.json", [{"symbol": "INFY", "decision": "REDUCE"}])
    assert _load_previous_states(tmp_path, dt.date(2026, 4, 22)) == ({}, {})


def test_ignores_unknown_decision_values(tmp_path: Path):
    _write(tmp_path / "2026-04-21.json", [
        {"symbol": "INFY", "decision": "HOLD"},
        {"symbol": "TCS", "decision": "SELL"},   # not a valid state
        {"symbol": "ITC"},                        # missing decision
    ])
    states, pending = _load_previous_states(tmp_path, dt.date(2026, 4, 22))
    assert states == {"INFY": "HOLD"}
    assert pending == {}


def test_corrupt_json_yields_empty(tmp_path: Path):
    (tmp_path / "2026-04-21.json").write_text("{not-json")
    assert _load_previous_states(tmp_path, dt.date(2026, 4, 22)) == ({}, {})


def test_non_date_filenames_are_skipped(tmp_path: Path):
    (tmp_path / "latest.json").write_text(json.dumps({"stocks": []}))
    _write(tmp_path / "2026-04-21.json", [{"symbol": "INFY", "decision": "HOLD"}])
    states, pending = _load_previous_states(tmp_path, dt.date(2026, 4, 22))
    assert states == {"INFY": "HOLD"}
    assert pending == {}


def test_pending_exits_roundtrip_from_prior_json(tmp_path: Path):
    # D-BT28: pending_exits carry a countdown plus enqueue date between runs.
    _write(
        tmp_path / "2026-04-21.json",
        [{"symbol": "INFY", "decision": "HOLD"}, {"symbol": "TCS", "decision": "REDUCE"}],
        pending=[
            {"symbol": "INFY", "days_remaining": 2, "enqueued_date": "2026-04-21"},
            {"symbol": "TCS", "days_remaining": 1, "enqueued_date": "2026-04-20"},
            {"symbol": "OLD", "days_remaining": 0},   # 0 or missing -> drop
            {"symbol": "BAD", "days_remaining": "x"},  # wrong type -> drop
        ],
    )
    states, pending = _load_previous_states(tmp_path, dt.date(2026, 4, 22))
    assert states == {"INFY": "HOLD", "TCS": "REDUCE"}
    assert pending == {"INFY": (2, "2026-04-21"), "TCS": (1, "2026-04-20")}
