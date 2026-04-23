from __future__ import annotations

import csv
import logging
from pathlib import Path

from portfolio_analyzer import config as cfg

log = logging.getLogger(__name__)


def load_symbol_list(path: Path) -> list[str]:
    """Read symbols from a one-column CSV, ignoring blanks and comment lines."""
    if not path.exists():
        log.warning("Symbol list not found: %s", path)
        return []
    symbols: list[str] = []
    with path.open("r", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            cell = row[0].strip()
            if not cell or cell.startswith("#") or cell.lower() == "symbol":
                continue
            symbols.append(cell)
    if not symbols:
        log.warning("Symbol list is empty: %s (add NSE trading symbols)", path)
    return symbols


def load_sector_map(path: Path) -> dict[str, str]:
    if not path.exists():
        log.warning("Sector map not found: %s", path)
        return {}
    mapping: dict[str, str] = {}
    with path.open("r", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].startswith("#") or row[0].lower() == "symbol":
                continue
            if len(row) >= 2 and row[0].strip():
                mapping[row[0].strip()] = row[1].strip() or "UNKNOWN"
    return mapping


def load_nifty500_symbols() -> list[str]:
    return load_symbol_list(cfg.DATA_DIR / "nifty500.csv")


def load_nifty50_symbols() -> list[str]:
    return load_symbol_list(cfg.DATA_DIR / "nifty50.csv")


def load_sector_map_default() -> dict[str, str]:
    """Merge auto-generated and user sector maps. User overrides win."""
    auto = load_sector_map(cfg.DATA_DIR / "sector_map.auto.csv")
    user = load_sector_map(cfg.DATA_DIR / "sector_map.csv")
    merged = dict(auto)
    merged.update(user)
    return merged
