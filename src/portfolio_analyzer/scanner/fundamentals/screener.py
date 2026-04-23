"""Screener.in fetcher + parser.

Scrapes the public company page (`/company/{SYMBOL}/consolidated/`) and falls
back to the standalone variant when the consolidated view is unavailable.

robots.txt allows `/company/...`; this module stays under the disallowed
query-string patterns (`?q=`, `?page=`, etc.) and throttles requests via
`cfg.SCREENER_REQUEST_DELAY_SEC`.

Numbers on Screener are rendered in Indian format (e.g. ``5,03,200``) and
money values are in rupees crore. All monetary columns returned by this module
are in *crore*; percentages are returned as decimals (``28%`` → ``0.28``).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Iterable

import requests
from bs4 import BeautifulSoup, Tag

from portfolio_analyzer import config as cfg

log = logging.getLogger(__name__)

SOURCE = "screener"

_PL_LABEL_MAP = {
    "Sales": "sales_cr", "Revenue": "sales_cr",
    "Expenses": "expenses_cr",
    "Operating Profit": "operating_profit_cr",
    "OPM %": "opm_pct",
    "Other Income": "other_income_cr",
    "Interest": "interest_cr",
    "Depreciation": "depreciation_cr",
    "Profit before tax": "profit_before_tax_cr",
    "Tax %": "tax_pct",
    "Net Profit": "net_profit_cr",
    "EPS in Rs": "eps",
    "Dividend Payout %": "dividend_payout_pct",
}

_BS_LABEL_MAP = {
    "Equity Capital": "equity_capital_cr",
    "Reserves": "reserves_cr",
    "Borrowings": "borrowings_cr",
    "Total Assets": "total_assets_cr",
}

_RATIOS_LABEL_MAP = {
    "Debtor Days": "debtor_days",
    "Inventory Days": "inventory_days",
    "Days Payable": "days_payable",
    "Cash Conversion Cycle": "cash_conversion_cycle",
    "Working Capital Days": "working_capital_days",
    "ROCE %": "roce_pct",
}


@dataclass
class ScreenerCompany:
    symbol: str
    name: str
    variant: str  # 'consolidated' | 'standalone'
    meta: dict[str, object] = field(default_factory=dict)
    annual_financials: list[dict[str, object]] = field(default_factory=list)
    annual_ratios: list[dict[str, object]] = field(default_factory=list)


class ScreenerNotFoundError(Exception):
    """Raised when Screener returns 404 for every variant we try."""


def company_url(symbol: str, variant: str) -> str:
    v = variant if variant else ""
    return cfg.SCREENER_COMPANY_URL_TEMPLATE.format(symbol=symbol, variant=v).rstrip("/") + "/"


def fetch_company(
    symbol: str, *, session: requests.Session | None = None,
    variants: Iterable[str] = cfg.SCREENER_VARIANTS,
    sleep: float | None = None,
) -> tuple[str, str]:
    """Fetch the Screener HTML for `symbol`, returning (variant, html).

    Tries each variant in order (default: consolidated then standalone).
    Raises ``ScreenerNotFoundError`` if all variants 404.
    """
    sess = session or requests.Session()
    headers = {
        "User-Agent": cfg.REFRESH_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_status: int | None = None
    delay = cfg.SCREENER_REQUEST_DELAY_SEC if sleep is None else sleep
    for variant in variants:
        url = company_url(symbol, variant)
        for attempt in range(cfg.SCREENER_MAX_RETRIES + 1):
            log.info("screener: GET %s (attempt %d)", url, attempt + 1)
            resp = sess.get(url, headers=headers, timeout=cfg.SCREENER_TIMEOUT)
            last_status = resp.status_code
            if resp.status_code == 200:
                if delay:
                    time.sleep(delay)
                return variant or "standalone", resp.text
            if resp.status_code == 404:
                break  # try next variant
            if resp.status_code in (429, 500, 502, 503, 504):
                backoff = (2 ** attempt) * max(delay, 1.0)
                log.warning("screener: %s got %d, backing off %.1fs", url, resp.status_code, backoff)
                time.sleep(backoff)
                continue
            resp.raise_for_status()
    raise ScreenerNotFoundError(f"No Screener page for {symbol!r} (last_status={last_status})")


def _clean_number(text: str) -> float | None:
    """Parse Screener's number renderings to float (or None).

    Handles Indian commas, percent signs, rupee glyph, em-dash, parentheses,
    empty cells, and the trailing ``+`` marker used for expandable rows.
    """
    s = (text or "").strip()
    if not s or s in {"-", "–", "—"}:
        return None
    s = s.replace(",", "").replace("₹", "").replace("Rs", "").replace("+", "").strip()
    # Strip currency-unit suffixes ("Cr.", "Cr", "Lakh", "L", "K") used in top-ratios.
    s = re.sub(r"\s*(Cr\.?|Lakh|Lacs?|L|K)\s*$", "", s, flags=re.IGNORECASE).strip()
    pct = s.endswith("%")
    if pct:
        s = s[:-1].strip()
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1].strip()
    try:
        v = float(s)
    except ValueError:
        return None
    if neg:
        v = -v
    if pct:
        v = v / 100.0
    return v



def _fiscal_year_from_header(text: str) -> int | None:
    """Convert 'Mar 2024' → 2024. Skips 'TTM' and short-period headers."""
    s = (text or "").strip()
    if not s or s.upper() == "TTM":
        return None
    # Reject headers with a period-length suffix like 'Mar 20183m' or '9m'
    if re.search(r"\d+m$", s):
        return None
    m = re.match(r"^\s*([A-Za-z]{3})\s+(\d{4})\s*$", s)
    if not m:
        return None
    return int(m.group(2))


def _top_ratio_value(items: list[str], label: str) -> str | None:
    """Return the raw value string from 'top-ratios' li for a given label."""
    for li in items:
        if li.lower().startswith(label.lower() + " ") or li.lower() == label.lower():
            return li[len(label):].strip()
    return None


def _extract_meta(soup: BeautifulSoup) -> dict[str, object]:
    top = soup.select_one("#top-ratios")
    items: list[str] = []
    if top:
        items = [li.get_text(" ", strip=True) for li in top.find_all("li")]

    def _val(label: str) -> float | None:
        raw = _top_ratio_value(items, label)
        return _clean_number(raw) if raw else None

    def _pair(label: str) -> tuple[float | None, float | None]:
        raw = _top_ratio_value(items, label)
        if not raw:
            return (None, None)
        parts = re.split(r"\s*/\s*", raw)
        if len(parts) != 2:
            return (None, None)
        return (_clean_number(parts[0]), _clean_number(parts[1]))

    hi, lo = _pair("High / Low")
    sector_a = soup.find("a", attrs={"title": "Broad Sector"})
    industry_a = soup.find("a", attrs={"title": "Broad Industry"})
    return {
        "sector": sector_a.get_text(strip=True) if sector_a else None,
        "industry": industry_a.get_text(strip=True) if industry_a else None,
        "market_cap_cr": _val("Market Cap"),
        "current_price": _val("Current Price"),
        "face_value": _val("Face Value"),
        "book_value": _val("Book Value"),
        "stock_pe": _val("Stock P/E"),
        "dividend_yield": _val("Dividend Yield"),
        "roe_latest": _val("ROE"),
        "roce_latest": _val("ROCE"),
        "high_52w": hi,
        "low_52w": lo,
        "promoter_holding": _val("Promoter holding"),
    }


def _parse_year_table(
    section: Tag | None, label_map: dict[str, str],
) -> dict[int, dict[str, object]]:
    """Given a Screener financial table section, return {fiscal_year: {col: value}}."""
    out: dict[int, dict[str, object]] = {}
    if section is None:
        return out
    table = section.find("table")
    if table is None:
        return out
    thead = table.find("thead")
    if thead is None:
        return out
    header_cells = [th.get_text(strip=True) for th in thead.find_all("th")]
    years = [_fiscal_year_from_header(h) for h in header_cells]
    tbody = table.find("tbody")
    if tbody is None:
        return out
    for tr in tbody.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        label_raw = cells[0].get_text(strip=True).rstrip("+").strip()
        col = label_map.get(label_raw)
        if col is None:
            continue
        for idx, td in enumerate(cells[1:], start=1):
            year = years[idx] if idx < len(years) else None
            if year is None:
                continue
            val = _clean_number(td.get_text(strip=True))
            out.setdefault(year, {"fiscal_year": year})[col] = val
    return out


def parse_company(symbol: str, html: str, *, variant: str = "consolidated") -> ScreenerCompany:
    """Parse a Screener HTML document into a typed ``ScreenerCompany``."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    name = h1.get_text(strip=True) if h1 else symbol

    meta = _extract_meta(soup)

    pl = _parse_year_table(soup.select_one("#profit-loss"), _PL_LABEL_MAP)
    bs = _parse_year_table(soup.select_one("#balance-sheet"), _BS_LABEL_MAP)
    for year, row in bs.items():
        pl.setdefault(year, {"fiscal_year": year}).update(
            {k: v for k, v in row.items() if k != "fiscal_year"}
        )
    ratios = _parse_year_table(soup.select_one("#ratios"), _RATIOS_LABEL_MAP)

    annual_financials = sorted(pl.values(), key=lambda r: r["fiscal_year"])  # type: ignore[arg-type]
    annual_ratios = sorted(ratios.values(), key=lambda r: r["fiscal_year"])  # type: ignore[arg-type]
    return ScreenerCompany(
        symbol=symbol, name=name, variant=variant,
        meta=meta,
        annual_financials=annual_financials,
        annual_ratios=annual_ratios,
    )
