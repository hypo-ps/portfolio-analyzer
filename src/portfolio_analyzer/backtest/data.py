from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd
import yfinance as yf

from portfolio_analyzer import config as cfg

log = logging.getLogger(__name__)

CACHE_DIR = cfg.DATA_DIR / "backtest_cache"
OHLC_COLS = ["Open", "Close"]


def _cache_path(ticker: str) -> Path:
    safe = ticker.replace("/", "_").replace(":", "_")
    return CACHE_DIR / f"{safe}.parquet"


def _load_cached(ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame | None:
    path = _cache_path(ticker)
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        log.warning("cache read failed for %s: %s", ticker, exc)
        return None
    if df.empty or not set(OHLC_COLS).issubset(df.columns):
        return None
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx
    covers_start = idx.min().date() <= start
    covers_end = idx.max().date() >= (end - dt.timedelta(days=7))
    if not (covers_start and covers_end):
        return None
    return df


def _save_cache(ticker: str, df: pd.DataFrame) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(_cache_path(ticker))
    except Exception as exc:
        log.warning("cache write failed for %s: %s", ticker, exc)


def _normalize(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    missing = [c for c in OHLC_COLS if c not in df.columns]
    if missing:
        return None
    out = df[OHLC_COLS].dropna(how="all").astype("float64")
    if out.empty:
        return None
    idx = pd.to_datetime(out.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out.index = idx
    return out


def _extract_batch(df: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    if df is None or df.empty:
        return out
    if isinstance(df.columns, pd.MultiIndex):
        for t in tickers:
            try:
                sub = df.xs(t, axis=1, level=1)
            except KeyError:
                continue
            norm = _normalize(sub)
            if norm is not None:
                out[t] = norm
    else:
        if len(tickers) == 1:
            norm = _normalize(df)
            if norm is not None:
                out[tickers[0]] = norm
    return out


def _fetch_single(ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame | None:
    try:
        df = yf.Ticker(ticker).history(
            start=start, end=end, interval="1d", auto_adjust=True
        )
    except Exception as exc:
        log.warning("yf single-ticker OHLC fetch failed for %s: %s", ticker, exc)
        return None
    return _normalize(df)


def fetch_ohlc(
    tickers: list[str],
    start: dt.date,
    end: dt.date,
    batch_size: int = cfg.YF_BATCH_SIZE,
) -> dict[str, pd.DataFrame]:
    """Fetch adjusted OHLC for tickers over [start, end). Parquet-cached per ticker.

    Returns {ticker: DataFrame[Open, Close]} indexed by tz-naive daily timestamps.
    Missing tickers are omitted.
    """
    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    result: dict[str, pd.DataFrame] = {}
    to_fetch: list[str] = []
    for t in unique:
        cached = _load_cached(t, start, end)
        if cached is not None:
            result[t] = cached
        else:
            to_fetch.append(t)

    if result:
        log.info("backtest data: %d/%d tickers served from cache", len(result), len(unique))

    for i in range(0, len(to_fetch), batch_size):
        batch = to_fetch[i : i + batch_size]
        log.info("backtest yf: fetching OHLC batch %d-%d of %d", i + 1, i + len(batch), len(to_fetch))
        try:
            df = yf.download(
                batch, start=start, end=end, interval="1d",
                auto_adjust=True, progress=False, threads=False, group_by="column",
            )
        except Exception as exc:
            log.warning("backtest yf batch failed: %s", exc)
            df = None
        got = _extract_batch(df, batch) if df is not None else {}
        for t, frame in got.items():
            result[t] = frame
            _save_cache(t, frame)
        missing = [t for t in batch if t not in got]
        for t in missing:
            single = _fetch_single(t, start, end)
            if single is not None:
                result[t] = single
                _save_cache(t, single)
            else:
                log.warning("backtest: no OHLC data for %s", t)
    return result
