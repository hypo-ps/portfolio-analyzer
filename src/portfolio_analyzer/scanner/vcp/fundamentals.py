"""Fundamental feature extraction for the VCP scanner.

Reads the Screener-sourced tables (``fundamentals_meta``, ``financials_annual``,
``ratios_annual``, ``financials_quarterly``) and produces a single
``FundamentalFeatures`` row per ISIN. All growth rates are decimals
(0.15 = +15%); ``opm_pct`` is stored as decimal (0.22 = 22%).
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
    latest_opm_pct: float | None   # decimal (0.22 = 22%)
    revenue_growth_yoy: float | None
    revenue_cagr_3y: float | None
    revenue_cagr_5y: float | None
    profit_cagr_3y: float | None
    debt_to_equity: float | None
    years_of_data: int
    # Quarterly-derived features (D-S25). None if insufficient data.
    quarters_of_data: int = 0
    ttm_sales_cr: float | None = None
    ttm_net_profit_cr: float | None = None
    ttm_sales_growth_yoy: float | None = None   # TTM vs prior-TTM (≥8 quarters)
    ttm_profit_growth_yoy: float | None = None  # TTM vs prior-TTM (≥8 quarters)
    q_sales_yoy_latest: float | None = None     # q[-1] vs q[-5]
    q_sales_yoy_prev: float | None = None       # q[-2] vs q[-6]
    sales_accel_smoothed: float | None = None   # 2Q-smoothed YoY acceleration
    profit_accel_smoothed: float | None = None  # 2Q-smoothed YoY acceleration
    opm_trend: float | None = None              # opm[-1] − mean(opm[-4:])


def _cagr(start: float, end: float, years: int) -> float | None:
    if years <= 0 or start is None or end is None or start <= 0 or end <= 0:
        return None
    return (end / start) ** (1.0 / years) - 1.0


def _quarter_yoy(latest: float | None, base: float | None) -> float | None:
    """YoY growth with a strictly-positive base (prior losses → None)."""
    if latest is None or base is None or base <= 0:
        return None
    return (latest - base) / base


def _quarterly_features(
    rows: list[tuple[float | None, float | None, float | None]],
) -> dict[str, float | None]:
    """Compute TTM, YoY, smoothed acceleration and OPM trend from quarterly rows.

    ``rows`` is sorted ASC by period_end as (sales_cr, net_profit_cr, opm_pct).
    Any feature whose inputs aren't fully available is returned as ``None``;
    callers rely on partial-weight normalization to downstream the gap.
    """
    out: dict[str, float | None] = {
        "ttm_sales_cr": None, "ttm_net_profit_cr": None,
        "ttm_sales_growth_yoy": None, "ttm_profit_growth_yoy": None,
        "q_sales_yoy_latest": None, "q_sales_yoy_prev": None,
        "sales_accel_smoothed": None, "profit_accel_smoothed": None,
        "opm_trend": None,
    }
    n = len(rows)
    # TTM (latest 4Q sum) — needs ≥4 non-None sales/profit each.
    if n >= 4:
        last4_sales = [r[0] for r in rows[-4:]]
        last4_np = [r[1] for r in rows[-4:]]
        if all(v is not None for v in last4_sales):
            out["ttm_sales_cr"] = float(sum(last4_sales))  # type: ignore[arg-type]
        if all(v is not None for v in last4_np):
            out["ttm_net_profit_cr"] = float(sum(last4_np))  # type: ignore[arg-type]
        # OPM trend: latest minus 4Q mean (requires all 4 opm values).
        last4_opm = [r[2] for r in rows[-4:]]
        if all(v is not None for v in last4_opm):
            mean_opm = sum(last4_opm) / 4.0  # type: ignore[operator]
            out["opm_trend"] = float(last4_opm[-1] - mean_opm)  # type: ignore[operator]
    # TTM YoY — needs ≥8 quarters with strictly-positive prior-TTM base.
    if n >= 8:
        prev4_sales = [r[0] for r in rows[-8:-4]]
        prev4_np = [r[1] for r in rows[-8:-4]]
        if (out["ttm_sales_cr"] is not None
                and all(v is not None for v in prev4_sales)):
            base = float(sum(prev4_sales))  # type: ignore[arg-type]
            if base > 0:
                out["ttm_sales_growth_yoy"] = (
                    out["ttm_sales_cr"] - base
                ) / base
        if (out["ttm_net_profit_cr"] is not None
                and all(v is not None for v in prev4_np)):
            base = float(sum(prev4_np))  # type: ignore[arg-type]
            if base > 0:
                out["ttm_profit_growth_yoy"] = (
                    out["ttm_net_profit_cr"] - base
                ) / base
    # Quarterly YoY snapshots — q[-1] vs q[-5], q[-2] vs q[-6].
    if n >= 5:
        out["q_sales_yoy_latest"] = _quarter_yoy(rows[-1][0], rows[-5][0])
    if n >= 6:
        out["q_sales_yoy_prev"] = _quarter_yoy(rows[-2][0], rows[-6][0])
    # Smoothed acceleration — 4 YoY values → needs 8 quarters.
    if n >= 8:
        s_yoy = [
            _quarter_yoy(rows[-i][0], rows[-(i + 4)][0]) for i in (1, 2, 3, 4)
        ]
        p_yoy = [
            _quarter_yoy(rows[-i][1], rows[-(i + 4)][1]) for i in (1, 2, 3, 4)
        ]
        if all(v is not None for v in s_yoy):
            recent = (s_yoy[0] + s_yoy[1]) / 2.0  # type: ignore[operator]
            prior = (s_yoy[2] + s_yoy[3]) / 2.0   # type: ignore[operator]
            out["sales_accel_smoothed"] = float(recent - prior)
        if all(v is not None for v in p_yoy):
            recent = (p_yoy[0] + p_yoy[1]) / 2.0  # type: ignore[operator]
            prior = (p_yoy[2] + p_yoy[3]) / 2.0   # type: ignore[operator]
            out["profit_accel_smoothed"] = float(recent - prior)
    return out


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

    # Quarterly-derived features (D-S25). Same report_type preference as
    # annual. Silently empty when ingestion hasn't populated quarters.
    q_rows = conn.execute(
        "SELECT sales_cr, net_profit_cr, opm_pct FROM financials_quarterly "
        "WHERE isin = ? AND source = ? AND report_type = ? "
        "ORDER BY period_end ASC",
        (isin, source, report_type),
    ).fetchall()
    q_feats = _quarterly_features(list(q_rows))
    quarters_of_data = len(q_rows)

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
        quarters_of_data=quarters_of_data,
        ttm_sales_cr=q_feats["ttm_sales_cr"],
        ttm_net_profit_cr=q_feats["ttm_net_profit_cr"],
        ttm_sales_growth_yoy=q_feats["ttm_sales_growth_yoy"],
        ttm_profit_growth_yoy=q_feats["ttm_profit_growth_yoy"],
        q_sales_yoy_latest=q_feats["q_sales_yoy_latest"],
        q_sales_yoy_prev=q_feats["q_sales_yoy_prev"],
        sales_accel_smoothed=q_feats["sales_accel_smoothed"],
        profit_accel_smoothed=q_feats["profit_accel_smoothed"],
        opm_trend=q_feats["opm_trend"],
    )
