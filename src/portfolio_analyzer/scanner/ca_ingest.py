from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import requests

from portfolio_analyzer.scanner import corp_actions as ca
from portfolio_analyzer.scanner import db as sdb

log = logging.getLogger(__name__)

SOURCE_NSE_CA_API = "NSE_CA_API"


@dataclass(frozen=True)
class CaIngestResult:
    start: dt.date
    end: dt.date
    status: str  # "ingested" | "no_data" | "error"
    fetched: int = 0
    stored: int = 0
    adjusted_bars: int = 0
    detail: str = ""


def ingest_ca_range(
    start: dt.date,
    end: dt.date,
    db_path: Path | None = None,
    *,
    rebuild: bool = True,
) -> CaIngestResult:
    """Fetch NSE corporate actions for [start, end], upsert, optionally rebuild factors."""
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    db_path = db_path or sdb.default_db_path()

    try:
        actions = ca.fetch_and_parse(start, end)
    except requests.RequestException as exc:
        log.warning("Network error fetching CAs %s→%s: %s", start, end, exc)
        return CaIngestResult(start, end, "error", detail=str(exc))
    except (ValueError, OSError) as exc:
        log.warning("Parse error for CAs %s→%s: %s", start, end, exc)
        return CaIngestResult(start, end, "error", detail=str(exc))

    if not actions:
        return CaIngestResult(start, end, "no_data", detail="empty response")

    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        stored = sdb.upsert_corp_actions(conn, actions, source=SOURCE_NSE_CA_API)
        adjusted_bars = sdb.rebuild_cumulative_adjustments(conn) if rebuild else 0

    log.info(
        "Ingested %d CA rows (%d after parse) for %s→%s; adjusted_bars=%d",
        stored, len(actions), start.isoformat(), end.isoformat(), adjusted_bars,
    )
    return CaIngestResult(
        start=start, end=end, status="ingested",
        fetched=len(actions), stored=stored, adjusted_bars=adjusted_bars,
    )


def rebuild_adjustments(db_path: Path | None = None) -> int:
    db_path = db_path or sdb.default_db_path()
    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        return sdb.rebuild_cumulative_adjustments(conn)
