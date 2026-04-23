from __future__ import annotations

import datetime as dt
import logging

import pandas as pd
import yfinance as yf

from portfolio_analyzer import config as cfg

log = logging.getLogger(__name__)


def to_stock_ticker(symbol: str) -> str:
    """NSE tradingsymbol -> Yahoo Finance ticker (e.g. 'INFY' -> 'INFY.NS')."""
    return f"{symbol}.NS"


def _extract_close(df: pd.DataFrame, tickers: list[str]) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    if df is None or df.empty:
        return out
    if isinstance(df.columns, pd.MultiIndex):
        if "Close" not in df.columns.get_level_values(0):
            return out
        close = df["Close"]
        for t in tickers:
            if t in close.columns:
                s = close[t].dropna().astype("float64")
                if len(s) > 0:
                    s.index = pd.to_datetime(s.index)
                    out[t] = s
    else:
        if "Close" not in df.columns or len(tickers) != 1:
            return out
        s = df["Close"].dropna().astype("float64")
        if len(s) > 0:
            s.index = pd.to_datetime(s.index)
            out[tickers[0]] = s
    return out


def _fetch_single(ticker: str, from_date: dt.date, to_date: dt.date) -> pd.Series | None:
    """Fallback per-ticker fetch using Ticker.history (more forgiving than batch download)."""
    try:
        df = yf.Ticker(ticker).history(
            start=from_date,
            end=to_date,
            interval="1d",
            auto_adjust=False,
        )
    except Exception as exc:
        log.warning("yf: single-ticker fetch failed for %s: %s", ticker, exc)
        return None
    if df is None or df.empty or "Close" not in df.columns:
        return None
    s = df["Close"].dropna().astype("float64")
    if len(s) == 0:
        return None
    s.index = pd.to_datetime(s.index).tz_localize(None)
    return s


def fetch_daily_closes(
    tickers: list[str],
    days: int = cfg.HISTORY_DAYS_FETCH,
    batch_size: int = cfg.YF_BATCH_SIZE,
) -> dict[str, pd.Series]:
    """Batch-fetch daily close series from Yahoo Finance, with per-ticker fallback for misses.

    Returns {ticker: pd.Series} keyed by the Yahoo ticker (e.g. 'INFY.NS', '^NSEI').
    Missing or failed tickers are omitted.
    """
    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    to_date = dt.date.today() + dt.timedelta(days=1)  # yf 'end' is exclusive
    from_date = to_date - dt.timedelta(days=int(days * 1.6) + 10)

    result: dict[str, pd.Series] = {}
    missing_from_batch: list[str] = []
    for i in range(0, len(unique), batch_size):
        batch = unique[i : i + batch_size]
        log.info(
            "yf: fetching batch %d-%d of %d",
            i + 1,
            i + len(batch),
            len(unique),
        )
        try:
            df = yf.download(
                batch,
                start=from_date,
                end=to_date,
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
                group_by="column",
            )
        except Exception as exc:
            log.warning("yf: batch fetch failed (%d tickers): %s", len(batch), exc)
            missing_from_batch.extend(batch)
            continue
        got = _extract_close(df, batch)
        result.update(got)
        missing_from_batch.extend(t for t in batch if t not in got)

    if missing_from_batch:
        log.info("yf: retrying %d tickers individually", len(missing_from_batch))
        recovered: list[str] = []
        still_missing: list[str] = []
        for t in missing_from_batch:
            s = _fetch_single(t, from_date, to_date)
            if s is not None:
                result[t] = s
                recovered.append(t)
            else:
                still_missing.append(t)
        if recovered:
            log.info("yf: recovered %d/%d via fallback", len(recovered), len(missing_from_batch))
        if still_missing:
            log.warning("yf: no data for %d tickers after fallback: %s", len(still_missing), still_missing)
    return result
