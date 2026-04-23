from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import zipfile
from dataclasses import dataclass

import requests

from portfolio_analyzer import config as cfg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BhavRow:
    trade_date: dt.date
    isin: str
    symbol: str
    name: str
    series: str
    open: float
    high: float
    low: float
    close: float
    prev_close: float | None
    volume: int
    turnover: float | None
    trades: int | None


def bhavcopy_url(trade_date: dt.date) -> str:
    return cfg.NSE_BHAVCOPY_URL_TEMPLATE.format(date=trade_date.strftime("%Y%m%d"))


def fetch_bhavcopy(trade_date: dt.date) -> bytes:
    """Download the NSE UDiFF bhavcopy zip for `trade_date` and return raw bytes.

    Raises requests.HTTPError for non-200 responses (e.g. 404 on weekends/holidays).
    """
    url = bhavcopy_url(trade_date)
    headers = {
        "User-Agent": cfg.REFRESH_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    log.info("Downloading bhavcopy %s from %s", trade_date.isoformat(), url)
    resp = requests.get(url, headers=headers, timeout=cfg.NSE_BHAVCOPY_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def unzip_bhavcopy(zip_bytes: bytes) -> str:
    """Extract the single CSV member from the bhavcopy zip and return its text."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not members:
            raise ValueError("Bhavcopy zip contains no CSV file")
        if len(members) > 1:
            raise ValueError(f"Bhavcopy zip has {len(members)} CSVs, expected 1")
        with zf.open(members[0]) as f:
            return f.read().decode("utf-8")


def _to_float(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    return float(value)


def _to_int(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    return int(value)


def parse_bhavcopy(csv_text: str) -> list[BhavRow]:
    """Parse UDiFF CM CSV text, keeping only NSE equity rows (STK + EQ/BE).

    Required columns are validated explicitly; missing columns raise ValueError.
    """
    required = {
        "TradDt", "FinInstrmTp", "SctySrs", "ISIN", "TckrSymb", "FinInstrmNm",
        "OpnPric", "HghPric", "LwPric", "ClsPric", "PrvsClsgPric",
        "TtlTradgVol", "TtlTrfVal", "TtlNbOfTxsExctd",
    }
    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = set(reader.fieldnames or [])
    missing = required - fieldnames
    if missing:
        raise ValueError(f"Bhavcopy CSV missing required columns: {sorted(missing)}")

    rows: list[BhavRow] = []
    for raw in reader:
        if (raw.get("FinInstrmTp") or "").strip() != cfg.NSE_EQUITY_INSTRUMENT_TYPE:
            continue
        series = (raw.get("SctySrs") or "").strip()
        if series not in cfg.NSE_EQUITY_SERIES:
            continue
        isin = (raw.get("ISIN") or "").strip()
        symbol = (raw.get("TckrSymb") or "").strip()
        if not isin or not symbol:
            continue
        try:
            trade_date = dt.date.fromisoformat((raw.get("TradDt") or "").strip())
            open_ = _to_float(raw.get("OpnPric", ""))
            high = _to_float(raw.get("HghPric", ""))
            low = _to_float(raw.get("LwPric", ""))
            close = _to_float(raw.get("ClsPric", ""))
            volume = _to_int(raw.get("TtlTradgVol", ""))
        except (ValueError, TypeError) as exc:
            log.warning("Skipping malformed row for %s: %s", symbol, exc)
            continue
        if None in (open_, high, low, close) or volume is None:
            continue
        rows.append(BhavRow(
            trade_date=trade_date,
            isin=isin,
            symbol=symbol,
            name=(raw.get("FinInstrmNm") or "").strip(),
            series=series,
            open=open_,
            high=high,
            low=low,
            close=close,
            prev_close=_to_float(raw.get("PrvsClsgPric", "")),
            volume=volume,
            turnover=_to_float(raw.get("TtlTrfVal", "")),
            trades=_to_int(raw.get("TtlNbOfTxsExctd", "")),
        ))
    return rows


def fetch_and_parse(trade_date: dt.date) -> list[BhavRow]:
    """Convenience wrapper: download → unzip → parse for one trading date."""
    zip_bytes = fetch_bhavcopy(trade_date)
    csv_text = unzip_bhavcopy(zip_bytes)
    return parse_bhavcopy(csv_text)
