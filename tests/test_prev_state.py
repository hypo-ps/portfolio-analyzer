from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from portfolio_analyzer.cli import _load_previous_states


def _write(path: Path, stocks: list[dict]) -> None:
    path.write_text(json.dumps({"stocks": stocks}))


def test_returns_empty_when_dir_missing(tmp_path: Path):
    assert _load_previous_states(tmp_path / "nope", dt.date(2026, 4, 22)) == {}


def test_returns_empty_when_no_prior_json(tmp_path: Path):
    _write(tmp_path / "2026-04-22.json", [{"symbol": "INFY", "decision": "HOLD"}])
    # today == file date -> no strictly-prior file
    assert _load_previous_states(tmp_path, dt.date(2026, 4, 22)) == {}


def test_loads_decisions_from_most_recent_prior_file(tmp_path: Path):
    _write(tmp_path / "2026-04-18.json", [
        {"symbol": "INFY", "decision": "HOLD"},
        {"symbol": "TCS", "decision": "REDUCE"},
    ])
    _write(tmp_path / "2026-04-21.json", [
        {"symbol": "INFY", "decision": "REDUCE"},
        {"symbol": "TCS", "decision": "EXIT"},
    ])
    got = _load_previous_states(tmp_path, dt.date(2026, 4, 22))
    assert got == {"INFY": "REDUCE", "TCS": "EXIT"}


def test_stale_file_beyond_max_age_is_ignored(tmp_path: Path):
    _write(tmp_path / "2026-04-10.json", [{"symbol": "INFY", "decision": "REDUCE"}])
    assert _load_previous_states(tmp_path, dt.date(2026, 4, 22)) == {}


def test_ignores_unknown_decision_values(tmp_path: Path):
    _write(tmp_path / "2026-04-21.json", [
        {"symbol": "INFY", "decision": "HOLD"},
        {"symbol": "TCS", "decision": "SELL"},   # not a valid state
        {"symbol": "ITC"},                        # missing decision
    ])
    assert _load_previous_states(tmp_path, dt.date(2026, 4, 22)) == {"INFY": "HOLD"}


def test_corrupt_json_yields_empty(tmp_path: Path):
    (tmp_path / "2026-04-21.json").write_text("{not-json")
    assert _load_previous_states(tmp_path, dt.date(2026, 4, 22)) == {}


def test_non_date_filenames_are_skipped(tmp_path: Path):
    (tmp_path / "latest.json").write_text(json.dumps({"stocks": []}))
    _write(tmp_path / "2026-04-21.json", [{"symbol": "INFY", "decision": "HOLD"}])
    assert _load_previous_states(tmp_path, dt.date(2026, 4, 22)) == {"INFY": "HOLD"}
