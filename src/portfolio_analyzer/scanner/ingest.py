from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

import requests

from portfolio_analyzer.scanner import bhavcopy as bc
from portfolio_analyzer.scanner import db as sdb

log = logging.getLogger(__name__)

SOURCE_NSE_UDIFF = "NSE_UDIFF"


@dataclass(frozen=True)
class IngestResult:
    trade_date: dt.date
    status: str  # "ingested" | "skipped" | "no_data" | "error"
    rows: int = 0
    detail: str = ""


def ingest_date(
    trade_date: dt.date,
    db_path: Path | None = None,
    *,
    force: bool = False,
) -> IngestResult:
    """Fetch, parse, and upsert the NSE bhavcopy for one `trade_date`.

    Returns IngestResult describing the outcome. Does not raise for 404s or
    empty files — those become `no_data`. Network errors become `error`.
    """
    db_path = db_path or sdb.default_db_path()

    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        if not force and sdb.is_ingested(conn, trade_date):
            return IngestResult(trade_date, "skipped", detail="already ingested")

    try:
        rows = bc.fetch_and_parse(trade_date)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            log.info("No bhavcopy for %s (404 — non-trading day?)", trade_date.isoformat())
            return IngestResult(trade_date, "no_data", detail="404")
        log.warning("HTTP error fetching %s: %s", trade_date.isoformat(), exc)
        return IngestResult(trade_date, "error", detail=str(exc))
    except requests.RequestException as exc:
        log.warning("Network error fetching %s: %s", trade_date.isoformat(), exc)
        return IngestResult(trade_date, "error", detail=str(exc))
    except (ValueError, OSError) as exc:
        log.warning("Parse error for %s: %s", trade_date.isoformat(), exc)
        return IngestResult(trade_date, "error", detail=str(exc))

    if not rows:
        return IngestResult(trade_date, "no_data", detail="empty after filter")

    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, rows)
        sdb.upsert_market_data(conn, rows)
        sdb.record_ingestion(conn, trade_date, SOURCE_NSE_UDIFF, len(rows))

    log.info("Ingested %d rows for %s", len(rows), trade_date.isoformat())
    return IngestResult(trade_date, "ingested", rows=len(rows))


def _iter_dates(start: dt.date, end: dt.date) -> list[dt.date]:
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    out: list[dt.date] = []
    cur = start
    while cur <= end:
        # Skip weekends — NSE is closed Sat/Sun. Holidays return 404 and are handled downstream.
        if cur.weekday() < 5:
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def ingest_range(
    start: dt.date,
    end: dt.date,
    db_path: Path | None = None,
    *,
    force: bool = False,
) -> list[IngestResult]:
    results: list[IngestResult] = []
    for d in _iter_dates(start, end):
        results.append(ingest_date(d, db_path=db_path, force=force))
    return results
