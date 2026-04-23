from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from dataclasses import dataclass
from pathlib import Path

import requests

from portfolio_analyzer import config as cfg

log = logging.getLogger(__name__)

NIFTY500_FILE = "nifty500.csv"
NIFTY50_FILE = "nifty50.csv"
SECTOR_AUTO_FILE = "sector_map.auto.csv"


@dataclass
class ParsedIndex:
    symbols: list[str]
    industry_by_symbol: dict[str, str]


def _parse_nse_csv(text: str) -> ParsedIndex:
    """Parse NSE constituent CSV (Company Name, Industry, Symbol, Series, ISIN Code)."""
    reader = csv.DictReader(io.StringIO(text))
    symbols: list[str] = []
    industries: dict[str, str] = {}
    for row in reader:
        symbol = (row.get("Symbol") or "").strip()
        if not symbol:
            continue
        symbols.append(symbol)
        industry = (row.get("Industry") or "").strip()
        if industry:
            industries[symbol] = industry
    return ParsedIndex(symbols=symbols, industry_by_symbol=industries)


def _download(url: str) -> str:
    headers = {"User-Agent": cfg.REFRESH_USER_AGENT}
    resp = requests.get(url, headers=headers, timeout=cfg.REFRESH_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _write_symbol_list(path: Path, symbols: list[str], source_url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    lines = [f"# refreshed {today} from {source_url}", "symbol", *symbols]
    path.write_text("\n".join(lines) + "\n")


def _write_sector_auto(path: Path, industries: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    with path.open("w", newline="") as f:
        f.write(f"# refreshed {today} from NSE constituent files (Industry column)\n")
        writer = csv.writer(f)
        writer.writerow(["symbol", "sector"])
        for symbol in sorted(industries):
            writer.writerow([symbol, industries[symbol]])


def _is_fresh_today(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    mtime_date = dt.date.fromtimestamp(path.stat().st_mtime)
    return mtime_date == dt.date.today()


def _all_fresh(data_dir: Path) -> bool:
    return all(
        _is_fresh_today(data_dir / name)
        for name in (NIFTY500_FILE, NIFTY50_FILE, SECTOR_AUTO_FILE)
    )


def refresh_constituents(data_dir: Path | None = None, force: bool = False) -> bool:
    """Refresh NIFTY 500 / 50 symbol lists and auto sector map.

    Returns True if refreshed, False if skipped (already fresh or network failure
    with existing files still usable).
    """
    data_dir = data_dir or cfg.DATA_DIR
    if not force and _all_fresh(data_dir):
        log.info("Constituent lists already fresh for today; skipping refresh.")
        return False

    try:
        log.info("Downloading NIFTY 500 constituents...")
        parsed_500 = _parse_nse_csv(_download(cfg.NIFTY500_CSV_URL))
        log.info("Downloading NIFTY 50 constituents...")
        parsed_50 = _parse_nse_csv(_download(cfg.NIFTY50_CSV_URL))
    except requests.RequestException as exc:
        log.warning("Constituent refresh failed: %s. Using existing files if present.", exc)
        return False

    if len(parsed_500.symbols) < 100 or len(parsed_50.symbols) < 10:
        log.warning(
            "Refresh produced suspiciously small lists (n500=%d, n50=%d); aborting write.",
            len(parsed_500.symbols),
            len(parsed_50.symbols),
        )
        return False

    _write_symbol_list(data_dir / NIFTY500_FILE, parsed_500.symbols, cfg.NIFTY500_CSV_URL)
    _write_symbol_list(data_dir / NIFTY50_FILE, parsed_50.symbols, cfg.NIFTY50_CSV_URL)

    combined_industries = dict(parsed_500.industry_by_symbol)
    combined_industries.update(parsed_50.industry_by_symbol)
    _write_sector_auto(data_dir / SECTOR_AUTO_FILE, combined_industries)

    log.info(
        "Refreshed: %d NIFTY 500 symbols, %d NIFTY 50 symbols, %d sector entries.",
        len(parsed_500.symbols),
        len(parsed_50.symbols),
        len(combined_industries),
    )
    return True
