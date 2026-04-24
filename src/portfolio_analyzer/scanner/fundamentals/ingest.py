"""Fundamentals ingestion orchestrator.

Iterates `stock_master`, fetches each company's page from Screener.in, parses
the HTML, and upserts the results into `fundamentals_meta`, `financials_annual`
and `ratios_annual`. Failures and stale entries are tracked in
`fundamentals_ingestion_log` so re-runs can skip anything fetched recently.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

from portfolio_analyzer import config as cfg
from portfolio_analyzer.scanner import db as sdb
from portfolio_analyzer.scanner.fundamentals import screener

log = logging.getLogger(__name__)

SOURCE = screener.SOURCE


@dataclass(frozen=True)
class FundamentalsIngestResult:
    processed: int
    ok: int
    not_found: int
    error: int
    skipped: int
    detail: str = ""


def _iter_universe(
    conn, *, only_symbols: tuple[str, ...] | None, limit: int | None,
) -> list[tuple[str, str]]:
    """Return a list of (isin, symbol) ordered by symbol."""
    if only_symbols:
        placeholders = ",".join(["?"] * len(only_symbols))
        sql = (
            f"SELECT isin, symbol FROM stock_master "
            f"WHERE symbol IN ({placeholders}) ORDER BY symbol"
        )
        rows = conn.execute(sql, only_symbols).fetchall()
    else:
        sql = "SELECT isin, symbol FROM stock_master ORDER BY symbol"
        rows = conn.execute(sql).fetchall()
    if limit is not None:
        rows = rows[:limit]
    return [(r[0], r[1]) for r in rows]


def _is_fresh(last_fetched: dt.datetime | None, refresh_after_days: int) -> bool:
    if last_fetched is None:
        return False
    age = dt.datetime.now() - last_fetched
    return age < dt.timedelta(days=refresh_after_days)


def ingest_one(
    conn, isin: str, symbol: str, *,
    session: requests.Session | None = None,
    sleep: float | None = None,
) -> str:
    """Fetch + parse + upsert fundamentals for a single symbol. Returns status."""
    try:
        variant, html = screener.fetch_company(symbol, session=session, sleep=sleep)
    except screener.ScreenerNotFoundError as exc:
        sdb.record_fundamentals_ingestion(
            conn, isin, SOURCE, "not_found", detail=str(exc),
        )
        return "not_found"
    except requests.RequestException as exc:
        sdb.record_fundamentals_ingestion(
            conn, isin, SOURCE, "error", detail=f"network: {exc}",
        )
        return "error"

    try:
        company = screener.parse_company(symbol, html, variant=variant)
    except Exception as exc:  # parser bugs must not crash a long run
        log.exception("screener: parse failed for %s", symbol)
        sdb.record_fundamentals_ingestion(
            conn, isin, SOURCE, "error", detail=f"parse: {exc}",
        )
        return "error"

    sdb.upsert_fundamentals_meta(conn, isin, SOURCE, company.meta)
    sdb.upsert_financials_annual(
        conn, isin, SOURCE, company.variant, company.annual_financials,
    )
    sdb.upsert_ratios_annual(
        conn, isin, SOURCE, company.variant, company.annual_ratios,
    )
    sdb.upsert_financials_quarterly(
        conn, isin, SOURCE, company.variant, company.quarterly_financials,
    )
    sdb.record_fundamentals_ingestion(
        conn, isin, SOURCE, "ok", report_type=company.variant,
    )
    return "ok"


def ingest_fundamentals(
    *,
    db_path: Path | None = None,
    only_symbols: Iterable[str] | None = None,
    limit: int | None = None,
    refresh_after_days: int | None = None,
    force: bool = False,
    session: requests.Session | None = None,
    sleep: float | None = None,
) -> FundamentalsIngestResult:
    """Drive the full Screener ingestion across `stock_master`.

    Skips entries with a successful fetch newer than ``refresh_after_days``
    unless ``force`` is set.
    """
    db_path = db_path or sdb.default_db_path()
    syms = tuple(s.upper() for s in only_symbols) if only_symbols else None
    refresh_days = (
        refresh_after_days
        if refresh_after_days is not None
        else cfg.SCREENER_REFRESH_AFTER_DAYS
    )

    processed = ok = nf = err = skipped = 0
    sess = session or requests.Session()

    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        universe = _iter_universe(conn, only_symbols=syms, limit=limit)
        total = len(universe)
        log.info("fundamentals: %d symbols in universe", total)

        for idx, (isin, symbol) in enumerate(universe, start=1):
            processed += 1
            if not force:
                last = sdb.last_fundamentals_fetch(conn, isin, SOURCE)
                if _is_fresh(last, refresh_days):
                    skipped += 1
                    log.debug("fundamentals: skip %s (fresh)", symbol)
                    continue
            log.info("fundamentals: [%d/%d] %s", idx, total, symbol)
            status = ingest_one(conn, isin, symbol, session=sess, sleep=sleep)
            if status == "ok":
                ok += 1
            elif status == "not_found":
                nf += 1
            else:
                err += 1
            if idx % 25 == 0:
                conn.commit()

    log.info(
        "fundamentals: processed=%d ok=%d not_found=%d error=%d skipped=%d",
        processed, ok, nf, err, skipped,
    )
    return FundamentalsIngestResult(
        processed=processed, ok=ok, not_found=nf, error=err, skipped=skipped,
    )
