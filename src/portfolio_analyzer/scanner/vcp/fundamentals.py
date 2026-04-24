"""Fundamental feature extraction for the VCP scanner.

Reads the Screener-sourced tables (``fundamentals_meta``, ``financials_annual``,
``ratios_annual``) and produces a single ``FundamentalFeatures`` row per ISIN.
All growth rates are decimals (0.15 = +15%).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class FundamentalFeatures:
    isin: str
    sector: str | None
    industry: str | None
    market_cap_cr: float | None
    stock_pe: float | None
    roe_latest: float | None       # decimal (0.18 = 18%)
    roce_latest: float | None      # decimal
    promoter_holding: float | None # decimal
    latest_fy: int | None
    latest_sales_cr: float | None
    latest_net_profit_cr: float | None
    latest_opm_pct: float | None   # percentage as stored (e.g. 22.5)
    revenue_growth_yoy: float | None
    revenue_cagr_3y: float | None
    revenue_cagr_5y: float | None
    profit_cagr_3y: float | None
    debt_to_equity: float | None
    years_of_data: int


def _cagr(start: float, end: float, years: int) -> float | None:
    if years <= 0 or start is None or end is None or start <= 0 or end <= 0:
        return None
    return (end / start) ** (1.0 / years) - 1.0


def load_fundamental_features(
    conn: sqlite3.Connection, isin: str, *, source: str = "screener",
) -> FundamentalFeatures | None:
    """Return FundamentalFeatures for ``isin`` or None if no data exists.

    ``report_type`` prefers consolidated over standalone when both are present.
    """
    meta = conn.execute(
        "SELECT sector, industry, market_cap_cr, stock_pe, roe_latest, "
        "roce_latest, promoter_holding FROM fundamentals_meta "
        "WHERE isin = ? AND source = ?",
        (isin, source),
    ).fetchone()

    # Prefer consolidated; fall back to standalone if not available.
    report_type = conn.execute(
        "SELECT report_type FROM financials_annual "
        "WHERE isin = ? AND source = ? "
        "ORDER BY CASE report_type WHEN 'consolidated' THEN 0 ELSE 1 END "
        "LIMIT 1",
        (isin, source),
    ).fetchone()
    if report_type is None and meta is None:
        return None
    report_type = report_type[0] if report_type else "consolidated"

    rows = conn.execute(
        "SELECT fiscal_year, sales_cr, net_profit_cr, opm_pct, "
        "equity_capital_cr, reserves_cr, borrowings_cr "
        "FROM financials_annual "
        "WHERE isin = ? AND source = ? AND report_type = ? "
        "ORDER BY fiscal_year ASC",
        (isin, source, report_type),
    ).fetchall()

    (sector, industry, mcap, pe, roe, roce, prom) = (
        meta if meta else (None,) * 7
    )

    years_of_data = len(rows)
    latest_fy = rows[-1][0] if rows else None
    latest_sales = rows[-1][1] if rows else None
    latest_np = rows[-1][2] if rows else None
    latest_opm = rows[-1][3] if rows else None

    # YoY and CAGR on sales
    rev_yoy = None
    if len(rows) >= 2 and rows[-2][1] and rows[-1][1] is not None:
        prev = rows[-2][1]
        curr = rows[-1][1]
        if prev and prev > 0:
            rev_yoy = (curr - prev) / prev

    rev_cagr_3y = None
    if len(rows) >= 4 and rows[-4][1] and rows[-1][1]:
        rev_cagr_3y = _cagr(rows[-4][1], rows[-1][1], 3)
    rev_cagr_5y = None
    if len(rows) >= 6 and rows[-6][1] and rows[-1][1]:
        rev_cagr_5y = _cagr(rows[-6][1], rows[-1][1], 5)

    profit_cagr_3y = None
    if len(rows) >= 4 and rows[-4][2] and rows[-1][2]:
        profit_cagr_3y = _cagr(rows[-4][2], rows[-1][2], 3)

    # Debt-to-equity from latest balance-sheet row
    d_to_e = None
    if rows:
        _, _, _, _, eq, res, borr = rows[-1]
        equity = (eq or 0.0) + (res or 0.0)
        if equity > 0 and borr is not None:
            d_to_e = borr / equity

    return FundamentalFeatures(
        isin=isin,
        sector=sector,
        industry=industry,
        market_cap_cr=mcap,
        stock_pe=pe,
        roe_latest=roe,
        roce_latest=roce,
        promoter_holding=prom,
        latest_fy=latest_fy,
        latest_sales_cr=latest_sales,
        latest_net_profit_cr=latest_np,
        latest_opm_pct=latest_opm,
        revenue_growth_yoy=rev_yoy,
        revenue_cagr_3y=rev_cagr_3y,
        revenue_cagr_5y=rev_cagr_5y,
        profit_cagr_3y=profit_cagr_3y,
        debt_to_equity=d_to_e,
        years_of_data=years_of_data,
    )
