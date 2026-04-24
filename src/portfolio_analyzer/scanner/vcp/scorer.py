"""VCP scoring pipeline.

Four stages per candidate:
1. Stage-1 hard filters (liquidity, trend, price strength, near-highs).
2. Stage-2 fundamentals (hard rejects + soft 0..1 score).
3. Stage-3 VCP detection — 6 sub-scores blended into ``vcp_score``.
4. Readiness + final blend → ``decision``.

All scores are decimals in [0, 1] unless otherwise noted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .features import TechnicalFeatures
from .fundamentals import FundamentalFeatures

# Stage-1 hard-filter thresholds
MIN_TURNOVER_CR = 0.5
MIN_MARKET_CAP_CR = 100.0
MIN_RETURN_1Y = 0.20
MAX_DISTANCE_52W = 0.25  # within 25% of 52w high

# Stage-2 fundamentals
MIN_ROE = 0.10
MAX_DTE = 2.0
TARGET_REV_GROWTH = 0.15
TARGET_ROE = 0.20
TARGET_ROCE = 0.20

# Stage-3 tuning
ATR_COMPRESSION_TARGET = 0.7
PIVOT_RANGE_MAX = 0.08
RANGE_20D_MAX = 0.20
READINESS_BAND_BELOW = 0.05    # |dist| <= 5% below pivot → full readiness
READINESS_BAND_ABOVE = 0.02    # |dist| <= 2% above pivot → full readiness
MIN_MOMENTUM_20D = 0.02        # |return_20d| floor to avoid dead-stock volatility
MIN_PIVOT_TOUCHES = 2          # closes within 2% of pivot required for full pivot score
BREAKOUT_PRESSURE_MAX_STD = 0.005   # std(close_5)/close below this → +0.05 VCP bonus
BREAKOUT_PRESSURE_BONUS = 0.05
SHAKEOUT_BONUS = 0.10
RS_BOOST_MAX = 0.20            # combined *= (1 + 0.2 * min(rs,1)) when rs > 0

# Decision thresholds
BUY_FINAL = 0.75
BUY_PIVOT_BAND = 0.02          # within 2% of pivot
WATCHLIST_FINAL = 0.55
WATCHLIST_VCP = 0.40


@dataclass
class ScoreBreakdown:
    decision: str
    stage: str
    technical_score: float | None = None
    fundamental_score: float | None = None
    vcp_score: float | None = None
    readiness_score: float | None = None
    combined_score: float | None = None
    final_score: float | None = None
    reasons: list[str] = field(default_factory=list)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _stage1_hard(t: TechnicalFeatures) -> list[str]:
    """Return the list of failed hard-filter reasons (empty = pass)."""
    fails: list[str] = []
    if t.avg_turnover_20d_cr is None or t.avg_turnover_20d_cr < MIN_TURNOVER_CR:
        fails.append(f"turnover<{MIN_TURNOVER_CR}cr")
    if t.ema50 is None or t.ema200 is None:
        fails.append("insufficient_history")
        return fails
    if not (t.close > t.ema50 > t.ema200):
        fails.append("trend_stack_broken")
    if t.ema50_slope_20d is None or t.ema50_slope_20d <= 0:
        fails.append("ema50_not_rising")
    if t.return_1y is None or t.return_1y < MIN_RETURN_1Y:
        fails.append(f"return_1y<{MIN_RETURN_1Y:.0%}")
    if (t.distance_from_52w_high is None
            or t.distance_from_52w_high < -MAX_DISTANCE_52W):
        fails.append(f"far_from_52w_high>{MAX_DISTANCE_52W:.0%}")
    return fails


def _technical_score(t: TechnicalFeatures) -> float:
    """Continuous 0..1 rollup of the stage-1 signals."""
    stack = 1.0 if (t.ema50 and t.ema200 and t.close > t.ema50 > t.ema200) else 0.0
    slope = _clamp((t.ema50_slope_20d or 0.0) / 0.10)
    r1y = _clamp((t.return_1y or 0.0) / 0.60)
    near = _clamp(1.0 + (t.distance_from_52w_high or -1.0) / MAX_DISTANCE_52W)
    depth = _clamp((t.avg_turnover_20d_cr or 0.0) / 10.0)  # 10cr → full
    return (stack + slope + r1y + near + depth) / 5.0


def _stage2_hard(f: FundamentalFeatures | None) -> list[str]:
    if f is None:
        return ["no_fundamentals"]
    fails: list[str] = []
    if f.roe_latest is not None and f.roe_latest < MIN_ROE:
        fails.append(f"roe<{MIN_ROE:.0%}")
    if f.debt_to_equity is not None and f.debt_to_equity > MAX_DTE:
        fails.append(f"debt_to_equity>{MAX_DTE}")
    if f.market_cap_cr is not None and f.market_cap_cr < MIN_MARKET_CAP_CR:
        fails.append(f"market_cap<{MIN_MARKET_CAP_CR}cr")
    return fails


def _fundamental_score(f: FundamentalFeatures) -> float:
    growth = f.revenue_cagr_3y
    if growth is None:
        growth = f.revenue_growth_yoy
    s_growth = _clamp((growth or 0.0) / TARGET_REV_GROWTH)
    s_roe = _clamp((f.roe_latest or 0.0) / TARGET_ROE)
    s_roce = _clamp((f.roce_latest or 0.0) / TARGET_ROCE)
    if f.debt_to_equity is None:
        s_dte = 0.5
    elif f.debt_to_equity <= 0.5:
        s_dte = 1.0
    elif f.debt_to_equity >= MAX_DTE:
        s_dte = 0.0
    else:
        s_dte = 1.0 - (f.debt_to_equity - 0.5) / (MAX_DTE - 0.5)
    return 0.35 * s_growth + 0.25 * s_roe + 0.20 * s_roce + 0.20 * s_dte


def _contraction_score(t: TechnicalFeatures) -> float:
    """Sequential tightening required: each swing range must be tighter than the prior."""
    if len(t.swing_highs) < 3 or len(t.swing_lows) < 3:
        return 0.0
    h = [p for _, p in t.swing_highs[-3:]]
    low = [p for _, p in t.swing_lows[-3:]]
    r = [h[i] - low[i] for i in range(3)]
    if r[0] <= 0:
        return 0.0
    if not (r[1] < r[0] and r[2] < r[1]):
        return 0.0
    return _clamp(1.0 - r[2] / r[0])


def _volatility_score(t: TechnicalFeatures) -> float:
    if not t.atr5_recent or not t.atr30_trailing:
        return 0.0
    if t.return_20d is None or abs(t.return_20d) < MIN_MOMENTUM_20D:
        return 0.0  # dead stock: compression without prior movement is not VCP
    ratio = t.atr5_recent / t.atr30_trailing
    # ratio 0.7 → 1.0; 1.0 → 0.0; linear in between, clamped.
    return _clamp((1.0 - ratio) / (1.0 - ATR_COMPRESSION_TARGET))


def _volume_score(t: TechnicalFeatures) -> float:
    slope_part = _clamp(-(t.volume_slope_20d or 0.0) * 20.0)
    pct_part = 0.5
    if t.avg_volume_20d is not None and t.volume_last_50d:
        arr = np.asarray(t.volume_last_50d, dtype=float)
        if arr.size > 0:
            # Fraction of 50-day daily volumes at or below the recent 20d mean.
            pct = float(np.mean(arr <= t.avg_volume_20d))
            pct_part = _clamp(1.0 - pct)
    return 0.5 * slope_part + 0.5 * pct_part


def _structure_score(t: TechnicalFeatures) -> float:
    if len(t.swing_lows) < 3:
        return 0.0
    l0, l1, l2 = [p for _, p in t.swing_lows[-3:]]
    base = 0.5 * (l1 > l0) + 0.5 * (l2 > l1)
    # Shakeout: final swing undercuts the prior one but price has recovered above it.
    if l2 < l1 and t.close > l1:
        base += SHAKEOUT_BONUS
    return _clamp(base)


def _pivot_score(t: TechnicalFeatures) -> float:
    if t.pivot_range is None:
        return 0.0
    score = _clamp(1.0 - t.pivot_range / PIVOT_RANGE_MAX)
    # Strong pivots are retested: halve the score if closes haven't revisited it.
    if t.pivot_touches is not None and t.pivot_touches < MIN_PIVOT_TOUCHES:
        score *= 0.5
    return score


def _range_score(t: TechnicalFeatures) -> float:
    if t.range_20d is None:
        return 0.0
    return _clamp(1.0 - t.range_20d / RANGE_20D_MAX)


def _vcp_score(t: TechnicalFeatures) -> tuple[float, dict[str, float]]:
    parts = {
        "contraction": _contraction_score(t),
        "volatility": _volatility_score(t),
        "volume": _volume_score(t),
        "structure": _structure_score(t),
        "pivot": _pivot_score(t),
        "range": _range_score(t),
    }
    score = (
        0.28 * parts["contraction"]
        + 0.20 * parts["volatility"]
        + 0.15 * parts["volume"]
        + 0.17 * parts["structure"]
        + 0.15 * parts["pivot"]
        + 0.05 * parts["range"]
    )
    # Breakout pressure: tight closes = supply absorption, imminent expansion.
    if (t.close_std_5_norm is not None
            and t.close_std_5_norm < BREAKOUT_PRESSURE_MAX_STD):
        score += BREAKOUT_PRESSURE_BONUS
        parts["breakout_pressure"] = BREAKOUT_PRESSURE_BONUS
    return _clamp(score), parts


def _readiness_score(t: TechnicalFeatures) -> float:
    """Asymmetric: tighter band above pivot (late entries punished faster)."""
    if t.distance_to_pivot is None:
        return 0.0
    d = t.distance_to_pivot
    band = READINESS_BAND_ABOVE if d > 0 else READINESS_BAND_BELOW
    return _clamp(1.0 - abs(d) / band)


def score_candidate(
    t: TechnicalFeatures, f: FundamentalFeatures | None,
    *, rs_score: float | None = None,
) -> ScoreBreakdown:
    """End-to-end scoring; returns decision, stage, and all sub-scores."""
    reasons: list[str] = []

    s1_fails = _stage1_hard(t)
    if s1_fails:
        return ScoreBreakdown(
            decision="REJECT", stage="STAGE1_FAIL", reasons=s1_fails,
        )

    s2_fails = _stage2_hard(f)
    if s2_fails:
        return ScoreBreakdown(
            decision="REJECT", stage="STAGE2_FAIL", reasons=s2_fails,
        )
    assert f is not None  # narrowed by _stage2_hard

    tech = _technical_score(t)
    fund = _fundamental_score(f)
    vcp, parts = _vcp_score(t)
    readiness = _readiness_score(t)
    combined = 0.5 * vcp + 0.3 * tech + 0.2 * fund
    # RS reward-only boost — only amplifies real VCP setups, never rescues weak ones.
    if (rs_score is not None and rs_score > 0 and vcp >= WATCHLIST_VCP):
        boost = 1.0 + RS_BOOST_MAX * min(rs_score, 1.0)
        combined *= boost
        reasons.append(f"rs_boost={boost:.3f}")
    combined = _clamp(combined)
    final = combined * (0.5 + 0.5 * readiness)

    reasons.extend(f"{k}={v:.2f}" for k, v in parts.items())

    has_vcp = vcp >= WATCHLIST_VCP
    if (has_vcp
            and final >= BUY_FINAL
            and t.distance_to_pivot is not None
            and abs(t.distance_to_pivot) <= BUY_PIVOT_BAND):
        decision, stage = "BUY_ALERT", "READY"
    elif has_vcp and final >= WATCHLIST_FINAL:
        decision, stage = "WATCHLIST", "BUILDING"
    elif has_vcp:
        decision, stage = "WATCHLIST", "CONTRACTING"
    else:
        decision, stage = "REJECT", "STAGE3_FAIL"

    return ScoreBreakdown(
        decision=decision,
        stage=stage,
        technical_score=tech,
        fundamental_score=fund,
        vcp_score=vcp,
        readiness_score=readiness,
        combined_score=combined,
        final_score=final,
        reasons=reasons,
    )
