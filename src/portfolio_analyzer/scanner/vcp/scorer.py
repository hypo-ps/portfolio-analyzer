"""VCP scoring pipeline.

Four stages per candidate:
1. Stage-1 hard filters (liquidity, trend, price strength, near-highs).
2. Stage-2 fundamentals (hard rejects + soft 0..1 score).
3. Stage-3 VCP detection — 6 sub-scores blended into ``vcp_score``.
4. Lifecycle state detection → decision map (see ``_detect_state``).

All scores are decimals in [0, 1] unless otherwise noted.

Lifecycle states (mutually exclusive, priority-ordered):
    EXTENDED > BREAKOUT > READY > EARLY_READY > CONTRACTING >
    BASE_BUILDING > TREND > NONE.
Stage-1/2 hard-fail short-circuits to ``stage=FAIL`` / ``decision=REJECT``.
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

# RS boost gate (applies to combined_score, not to state detection)
WATCHLIST_VCP = 0.40

# State-machine thresholds (D-S23; CONTRACTING/READY relaxed D-S24;
# READY further relaxed + EARLY_READY added D-S28)
STATE_READY_VCP = 0.50
STATE_READY_PIVOT = 0.50
STATE_READY_RANGE_20D = 0.12        # D-S28: was 0.08
STATE_READY_STD5 = 0.015            # D-S28: was 0.010
STATE_READY_DIST_BELOW = -0.03
# EARLY_READY: same coil-quality gates as READY, but price still 2–6% below
# the pivot (coil is in, breakout has not arrived yet).
STATE_EARLY_READY_VCP = 0.50
STATE_EARLY_READY_PIVOT = 0.50
STATE_EARLY_READY_RANGE_20D = 0.12
STATE_EARLY_READY_STD5 = 0.015
STATE_EARLY_READY_DIST_LO = -0.06
STATE_EARLY_READY_DIST_HI = -0.02
STATE_CONTRACTING_VCP = 0.45
STATE_CONTRACTING_CONTRACTION = 0.0    # sub-score must exceed this
STATE_CONTRACTING_VOLATILITY = 0.40
STATE_CONTRACTING_VOLUME = 0.40
STATE_CONTRACTING_STRUCTURE = 0.50
STATE_CONTRACTING_MIN_SUBS = 2         # at least N of 4 sub-score gates must fire
STATE_BASE_VCP = 0.30
STATE_BASE_STRUCTURE = 0.30
STATE_BASE_RANGE_LO = 0.08     # range_20d must be > 0.08 to be BASE (else CONTRACTING)
STATE_BASE_RANGE_HI = 0.15
STATE_TREND_RETURN_3M = 0.10
STATE_TREND_RANGE_MIN = 0.15
STATE_TREND_VCP_MAX = 0.30
STATE_EXTENDED_DIST = 0.03        # D-S30: was 0.05
STATE_EXTENDED_EMA50_DIST = 0.10  # D-S30: was 0.15

STATE_TO_DECISION: dict[str, str] = {
    "READY": "BUY_ALERT",
    "EARLY_READY": "WATCHLIST",
    "CONTRACTING": "WATCHLIST",
    "BASE_BUILDING": "IGNORE",
    "TREND": "IGNORE",
    "NONE": "IGNORE",
    "BREAKOUT": "SKIP",
    "EXTENDED": "SKIP",
    "FAIL": "REJECT",
}


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
    """VCP volume score (D-S27): three explicit parts in [0, 1].

    - ``slope_part``: 20-bar log-volume slope; negative slope → full score.
    - ``dryup_part``: 20d / 50d average-volume ratio; 1.0 → 0, ≤0.70 → 1.0.
    - ``exp_part``: within ±2% of pivot, 3-bar average vs 20d mean;
      1.0× → 0, ≥1.5× → 1.0. Zero otherwise.

    Weighted 0.40·slope + 0.40·dryup + 0.20·expansion.
    """
    slope_part = _clamp(-(t.volume_slope_20d or 0.0) * 20.0)

    dryup_part = 0.0
    if (t.avg_volume_20d is not None and t.avg_volume_50d is not None
            and t.avg_volume_50d > 0):
        ratio = t.avg_volume_20d / t.avg_volume_50d
        dryup_part = _clamp((1.0 - ratio) / 0.30)

    exp_part = 0.0
    if (t.distance_to_pivot is not None
            and abs(t.distance_to_pivot) <= 0.02
            and t.avg_volume_20d is not None and t.avg_volume_20d > 0
            and len(t.volume_last_50d) >= 3):
        recent3 = float(np.mean(np.asarray(t.volume_last_50d[-3:], dtype=float)))
        exp_part = _clamp((recent3 / t.avg_volume_20d - 1.0) / 0.5)

    return 0.4 * slope_part + 0.4 * dryup_part + 0.2 * exp_part


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


def _detect_state(
    t: TechnicalFeatures, vcp: float, parts: dict[str, float],
) -> str:
    """Classify lifecycle stage. Priority-ordered; first match wins.

    EXTENDED > BREAKOUT > READY > EARLY_READY > CONTRACTING >
    BASE_BUILDING > TREND > NONE.
    """
    d = t.distance_to_pivot
    r20 = t.range_20d
    r5 = t.range_5d_norm

    # EXTENDED: post-breakout run, expanding volatility, stretched from EMA50.
    if (d is not None and d > STATE_EXTENDED_DIST
            and t.atr_expanding is True
            and t.distance_to_ema50 is not None
            and t.distance_to_ema50 > STATE_EXTENDED_EMA50_DIST):
        return "EXTENDED"

    # BREAKOUT (D-S29): crossed pivot with 5-day range expansion, confirmed
    # by a 3-bar volume expansion (mean(vol[-3:]) ≥ 1.3× avg20). Using a
    # 3-bar window avoids false triggers from a single noisy high-volume
    # bar; the 1.3× multiplier is lower than `volume_spike`'s 1.5× because
    # it's now an averaged signal.
    if (d is not None and d > 0.0
            and r5 is not None and r20 is not None and r5 > r20
            and t.volume_expansion_3bar is True):
        return "BREAKOUT"

    # READY: coiled just below (or at) pivot, pivot tight, std collapsed.
    if (vcp >= STATE_READY_VCP
            and d is not None and STATE_READY_DIST_BELOW <= d <= 0.0
            and parts.get("pivot", 0.0) > STATE_READY_PIVOT
            and r20 is not None and r20 <= STATE_READY_RANGE_20D
            and t.close_std_5_norm is not None
            and t.close_std_5_norm < STATE_READY_STD5):
        return "READY"

    # EARLY_READY (D-S28): coil in place but price still 2–6% below pivot.
    # Same tightness gates as READY; only the distance band differs.
    if (vcp >= STATE_EARLY_READY_VCP
            and d is not None
            and STATE_EARLY_READY_DIST_LO <= d <= STATE_EARLY_READY_DIST_HI
            and parts.get("pivot", 0.0) > STATE_EARLY_READY_PIVOT
            and r20 is not None and r20 <= STATE_EARLY_READY_RANGE_20D
            and t.close_std_5_norm is not None
            and t.close_std_5_norm < STATE_EARLY_READY_STD5):
        return "EARLY_READY"

    # CONTRACTING: valid VCP forming. Gate on vcp_score, then require at least
    # MIN_SUBS of four sub-score conditions — tolerates one weak leg so mid-base
    # setups aren't filtered out by a single laggy dimension.
    if vcp >= STATE_CONTRACTING_VCP:
        subs_hit = (
            int(parts.get("contraction", 0.0) > STATE_CONTRACTING_CONTRACTION)
            + int(parts.get("volatility", 0.0) > STATE_CONTRACTING_VOLATILITY)
            + int(parts.get("volume", 0.0) > STATE_CONTRACTING_VOLUME)
            + int(parts.get("structure", 0.0) > STATE_CONTRACTING_STRUCTURE)
        )
        if subs_hit >= STATE_CONTRACTING_MIN_SUBS:
            return "CONTRACTING"

    # BASE_BUILDING: early consolidation; looser than CONTRACTING.
    if (r20 is not None
            and STATE_BASE_RANGE_LO < r20 <= STATE_BASE_RANGE_HI
            and vcp >= STATE_BASE_VCP
            and parts.get("structure", 0.0) >= STATE_BASE_STRUCTURE):
        return "BASE_BUILDING"

    # TREND: strong uptrend but no meaningful consolidation yet.
    if (t.return_3m is not None and t.return_3m > STATE_TREND_RETURN_3M
            and r20 is not None and r20 > STATE_TREND_RANGE_MIN
            and vcp < STATE_TREND_VCP_MAX):
        return "TREND"

    return "NONE"


def score_candidate(
    t: TechnicalFeatures, f: FundamentalFeatures | None,
    *, rs_score: float | None = None,
) -> ScoreBreakdown:
    """End-to-end scoring; returns decision, stage, and all sub-scores.

    ``stage`` carries the 9-valued lifecycle state (TREND/BASE_BUILDING/
    CONTRACTING/EARLY_READY/READY/BREAKOUT/EXTENDED/NONE/FAIL);
    ``decision`` is the projection via ``STATE_TO_DECISION``.
    """
    reasons: list[str] = []

    s1_fails = _stage1_hard(t)
    if s1_fails:
        return ScoreBreakdown(
            decision="REJECT", stage="FAIL",
            reasons=["stage1:" + ",".join(s1_fails)],
        )

    s2_fails = _stage2_hard(f)
    if s2_fails:
        return ScoreBreakdown(
            decision="REJECT", stage="FAIL",
            reasons=["stage2:" + ",".join(s2_fails)],
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

    state = _detect_state(t, vcp, parts)
    decision = STATE_TO_DECISION[state]

    reasons.append(f"state={state}")
    reasons.extend(f"{k}={v:.2f}" for k, v in parts.items())

    return ScoreBreakdown(
        decision=decision,
        stage=state,
        technical_score=tech,
        fundamental_score=fund,
        vcp_score=vcp,
        readiness_score=readiness,
        combined_score=combined,
        final_score=final,
        reasons=reasons,
    )
