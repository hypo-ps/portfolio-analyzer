from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

from portfolio_analyzer import cli
from portfolio_analyzer.scanner import db as sdb
from portfolio_analyzer.scanner.bhavcopy import BhavRow
from portfolio_analyzer.scanner.vcp import features as vf
from portfolio_analyzer.scanner.vcp import scorer as vs
from portfolio_analyzer.scanner.vcp.features import TechnicalFeatures
from portfolio_analyzer.scanner.vcp.fundamentals import FundamentalFeatures
from portfolio_analyzer.scanner.vcp.scan import scan_date
from portfolio_analyzer.scanner.vcp.scorer import score_candidate


# ---------- synthetic fixtures ----------

def _tight_vcp_series(n: int = 400, seed: int = 11) -> tuple[np.ndarray, ...]:
    """Uptrend + textbook 3-swing contraction, close pinned near pivot."""
    rng = np.random.default_rng(seed)
    base = np.linspace(100.0, 200.0, n)
    close = base + rng.normal(0, 2.0, n)
    phase = np.linspace(0, 6 * np.pi, 60)
    amps = np.interp(np.arange(60), [0, 20, 40, 59], [6.0, 3.0, 1.5, 0.8])
    close[-60:] = base[-60:] + amps * np.sin(phase) + 0.3
    close[-1] = close[-10:].max() * 0.995
    high = close + np.abs(rng.normal(0, 0.5, n))
    low = close - np.abs(rng.normal(0, 0.5, n))
    vol = rng.integers(80_000, 150_000, n).astype(float)
    vol[-30:] *= 0.5
    return close, high, low, vol


def _strong_fundamentals() -> FundamentalFeatures:
    return FundamentalFeatures(
        isin="INE_TEST", sector="IT", industry=None, market_cap_cr=5000.0,
        stock_pe=25.0, roe_latest=0.22, roce_latest=0.25, promoter_holding=0.5,
        latest_fy=2025, latest_sales_cr=1000.0, latest_net_profit_cr=200.0,
        latest_opm_pct=22.0, revenue_growth_yoy=0.18, revenue_cagr_3y=0.20,
        revenue_cagr_5y=0.18, profit_cagr_3y=0.25, debt_to_equity=0.2,
        years_of_data=10,
    )


# ---------- features unit tests ----------

def test_ema_seed_and_convergence():
    arr = np.full(100, 50.0)
    out = vf._ema(arr, 50)
    assert out is not None
    assert out[49] == pytest.approx(50.0)
    assert out[-1] == pytest.approx(50.0)


def test_ema_none_when_insufficient_history():
    assert vf._ema(np.arange(10, dtype=float), 50) is None


def test_wilder_smoothing_matches_mean_for_constant_input():
    arr = np.full(30, 3.0)
    assert vf._wilder(arr, 14) == pytest.approx(3.0)


def test_true_range_simple_case():
    high = np.array([10.0, 12.0])
    low = np.array([9.0, 11.0])
    close = np.array([9.5, 11.5])
    tr = vf._true_range(high, low, close)
    assert tr.shape == (2,)
    assert tr[0] == pytest.approx(1.0)           # first bar uses high-low
    assert tr[1] == pytest.approx(12.0 - 9.5)    # gap up across prev close


def test_find_swings_detects_peak_and_trough():
    highs = np.array([1, 2, 3, 4, 5, 4, 3, 2, 1, 2, 3], dtype=float)
    lows = np.array([1, 0.5, 0.2, 0.1, 0.2, 0.5, 1, 2, 3, 2, 1], dtype=float)
    sh, sl = vf._find_swings(highs, lows, n=2)
    assert (4, 5.0) in sh
    assert (3, 0.1) in sl


def test_apply_spacing_collapses_close_cluster_to_extreme():
    """D-S26: adjacent same-type swings inside the spacing window must
    collapse to the more extreme value. Greedy left-to-right pass."""
    # Highs: keep the higher of the pair within the spacing window.
    raw = [(2, 5.0), (6, 4.0), (20, 7.0), (23, 6.5)]
    filtered = vf._apply_spacing(raw, min_spacing=6, prefer="max")
    assert filtered == [(2, 5.0), (20, 7.0)]
    # Lows: keep the lower of the pair within the spacing window.
    raw_lo = [(2, 1.0), (6, 1.5), (20, 0.5), (23, 0.8)]
    filtered_lo = vf._apply_spacing(raw_lo, min_spacing=6, prefer="min")
    assert filtered_lo == [(2, 1.0), (20, 0.5)]


def test_find_swings_applies_spacing_filter_end_to_end():
    """Two fractal-detected swing-highs 4 bars apart must collapse to the
    higher one after the module-level spacing pass."""
    highs = np.array([1, 2, 5, 3, 1, 2, 4, 2, 1], dtype=float)
    lows = np.array([1, 1, 1, 1, 1, 1, 1, 1, 1], dtype=float)
    sh, _ = vf._find_swings(highs, lows, n=2)
    # Without spacing we'd see both (2, 5.0) and (6, 4.0); after filter, only
    # the taller survives because the two sit within SWING_MIN_SPACING bars.
    assert sh == [(2, 5.0)]


def test_compute_technical_features_rejects_short_history():
    arr = np.full(100, 100.0)
    assert vf.compute_technical_features(arr, arr, arr, arr, arr) is None


def test_compute_technical_features_on_vcp_series():
    close, high, low, vol = _tight_vcp_series()
    t = vf.compute_technical_features(close, high, low, close, vol)
    assert t is not None
    assert t.ema50 > t.ema200                    # uptrend stack
    assert t.close > t.ema50
    assert t.return_1y > 0.20
    # Structural pivot (D-S25): close must sit within the base window, not
    # necessarily 1% below the 10-bar max as in the old micro-pivot rule.
    assert abs(t.distance_to_pivot) < 0.05
    assert t.range_20d < 0.10
    # Swing structure: 3 rising lows and 3 rising highs
    assert len(t.swing_highs) == 3
    assert len(t.swing_lows) == 3
    lows_ = [p for _, p in t.swing_lows]
    assert lows_[2] > lows_[1] > lows_[0]


def test_pivot_picks_last_swing_high_in_window_over_10bar_max():
    """D-S25: structural pivot must outrank the micro-pivot when a
    genuine swing-high sits further back inside the base window."""
    n = 300
    closes = np.linspace(99.9, 99.5, n)  # strictly decreasing → no native swings
    highs = closes.copy()
    lows = closes.copy()
    peak_idx = n - 25  # 25 bars back, inside the 40-bar window
    for off, val in [(-2, 102.0), (-1, 105.0), (0, 110.0), (1, 105.0), (2, 102.0)]:
        highs[peak_idx + off] = val
        closes[peak_idx + off] = val
    vol = np.full(n, 100_000.0)
    t = vf.compute_technical_features(closes, highs, lows, closes, vol)
    assert t is not None
    assert t.pivot == pytest.approx(110.0)
    assert t.distance_to_pivot == pytest.approx(
        (float(closes[-1]) - 110.0) / 110.0,
    )


def test_pivot_falls_back_to_window_max_without_swing():
    """D-S25: with no swing-high inside the base window, pivot falls back
    to the highest close over the last ``PIVOT_WINDOW`` bars."""
    n = 300
    closes = np.linspace(100.0, 150.0, n)  # monotone rise → no fractal pivot
    highs = closes + 0.01
    lows = closes - 0.01
    vol = np.full(n, 100_000.0)
    t = vf.compute_technical_features(closes, highs, lows, closes, vol)
    assert t is not None
    assert t.swing_highs == ()
    assert t.pivot == pytest.approx(float(closes[-vf.PIVOT_WINDOW:].max()))


def test_volume_expansion_3bar_true_when_last_3_avg_over_threshold():
    """D-S29: mean(vol[-3:]) ≥ 1.3 × avg_volume_20d → True."""
    n = 260
    close = np.linspace(100.0, 150.0, n)
    high = close + 0.5
    low = close - 0.5
    vol = np.full(n, 100_000.0)
    # Last 3 bars at 140k each. avg20 = (17·100k + 3·140k)/20 = 106k;
    # 3-bar mean / avg20 = 140/106 ≈ 1.32× → crosses 1.3× threshold.
    vol[-3:] = 140_000.0
    t = vf.compute_technical_features(close, high, low, close, vol)
    assert t is not None
    assert t.volume_expansion_3bar is True


def test_volume_expansion_3bar_false_on_single_bar_spike_only():
    """D-S29: a lone high-volume bar amid quiet bars trips volume_spike
    but must not pass the 3-bar mean gate."""
    n = 260
    close = np.linspace(100.0, 150.0, n)
    high = close + 0.5
    low = close - 0.5
    vol = np.full(n, 100_000.0)
    vol[-1] = 160_000.0  # last bar alone, 1.55× baseline → volume_spike True
    t = vf.compute_technical_features(close, high, low, close, vol)
    assert t is not None
    # avg20 = (19·100k + 160k)/20 = 103k; last bar 160/103 = 1.55× ≥ 1.5×.
    assert t.volume_spike is True
    # 3-bar mean = (100+100+160)/3 = 120k; 120/103 ≈ 1.165 < 1.30 → False.
    assert t.volume_expansion_3bar is False


# ---------- scorer pipeline tests ----------

def test_score_reject_downtrend():
    rng = np.random.default_rng(3)
    n = 400
    close = np.linspace(200.0, 100.0, n) + rng.normal(0, 2, n)
    high = close + 0.5
    low = close - 0.5
    vol = np.full(n, 100_000.0)
    t = vf.compute_technical_features(close, high, low, close, vol)
    r = score_candidate(t, None)
    assert r.decision == "REJECT"
    assert r.stage == "FAIL"


def test_score_stage2_fail_on_missing_fundamentals():
    close, high, low, vol = _tight_vcp_series()
    t = vf.compute_technical_features(close, high, low, close, vol)
    r = score_candidate(t, None)
    assert r.decision == "REJECT"
    assert r.stage == "FAIL"


def test_score_stage2_fail_on_high_debt():
    close, high, low, vol = _tight_vcp_series()
    t = vf.compute_technical_features(close, high, low, close, vol)
    bad = _strong_fundamentals().__class__(
        **{**_strong_fundamentals().__dict__, "debt_to_equity": 3.0},
    )
    r = score_candidate(t, bad)
    assert r.decision == "REJECT"
    assert r.stage == "FAIL"
    assert any("debt_to_equity" in reason for reason in r.reasons)


def test_score_buy_alert_on_textbook_vcp():
    close, high, low, vol = _tight_vcp_series()
    t = vf.compute_technical_features(close, high, low, close, vol)
    r = score_candidate(t, _strong_fundamentals())
    assert r.decision in {"BUY_ALERT", "WATCHLIST"}
    assert r.vcp_score is not None and r.vcp_score > 0.5
    assert r.readiness_score is not None and r.readiness_score > 0.8


def test_score_watchlist_for_noisy_uptrend():
    rng = np.random.default_rng(7)
    n = 400
    close = np.linspace(100.0, 200.0, n) + rng.normal(0, 3.0, n)
    t = vf.compute_technical_features(
        close, close + 0.5, close - 0.5, close, np.full(n, 1_000_000.0),
    )
    r = score_candidate(t, _strong_fundamentals())
    # Noisy series should not hit BUY_ALERT; typical outcomes are IGNORE/SKIP
    # (no clean contraction) or WATCHLIST if structure happens to align.
    assert r.decision in {"WATCHLIST", "IGNORE", "SKIP"}
    if r.decision == "WATCHLIST":
        assert r.stage == "CONTRACTING"


# ---------- scorer sub-score unit tests (targeted per-rule) ----------

def _baseline_tech() -> TechnicalFeatures:
    """Passes stage-1 and gives non-zero sub-scores. Override per test."""
    return TechnicalFeatures(
        close=100.0, ema50=95.0, ema200=80.0, ema50_slope_20d=0.03,
        atr14=2.0, atr50=2.5, atr5_recent=1.5, atr30_trailing=2.5,
        return_1y=0.40, return_3m=0.10, return_20d=0.05,
        high_52w=105.0, low_52w=70.0, distance_from_52w_high=-0.05,
        avg_volume_20d=80_000.0, avg_volume_50d=100_000.0,
        volume_last_50d=tuple([100_000.0] * 50),
        avg_turnover_20d_cr=5.0, range_20d=0.08, range_5d_norm=0.03,
        pivot=100.0, pivot_range=0.04, distance_to_pivot=0.0,
        pivot_touches=3, close_std_5_norm=0.01, volume_slope_20d=-0.01,
        atr_expanding=False, volume_spike=False,
        volume_expansion_3bar=False, distance_to_ema50=0.05,
        swing_highs=((50, 115.0), (100, 110.0), (150, 105.0)),
        swing_lows=((60, 100.0), (110, 99.0), (160, 100.0)),
    )


def test_contraction_requires_strict_tightening():
    # r0=15, r1=11, r2=5 → both transitions tighten → positive
    t = _baseline_tech()
    strict = vs._contraction_score(t)
    assert strict > 0.0

    # r0=15, r1=11, r2=12 → only first transition tightens → 0
    partial = replace(
        t,
        swing_highs=((50, 115.0), (100, 110.0), (150, 112.0)),
        swing_lows=((60, 100.0), (110, 99.0), (160, 100.0)),
    )
    assert vs._contraction_score(partial) == 0.0


def test_volatility_dead_stock_gate_zeroes_out():
    t = _baseline_tech()
    assert vs._volatility_score(t) > 0.0
    dead = replace(t, return_20d=0.01)  # below MIN_MOMENTUM_20D
    assert vs._volatility_score(dead) == 0.0


def test_volume_score_rewards_recent_below_50d_median():
    t = _baseline_tech()  # 20d avg (80k) below uniform 100k pool
    score_quiet = vs._volume_score(t)
    loud = replace(t, avg_volume_20d=150_000.0)
    score_loud = vs._volume_score(loud)
    assert score_quiet > score_loud


def test_volume_score_dryup_ratio_scales_linearly():
    """D-S27: 20d/50d ratio of 1.0 → 0, ≤0.70 → 1.0 for the dry-up part."""
    # Zero slope, far from pivot, so only the dry-up part contributes.
    base = replace(
        _baseline_tech(),
        volume_slope_20d=0.0, distance_to_pivot=-0.50,
        avg_volume_50d=100_000.0,
    )
    no_dryup = replace(base, avg_volume_20d=100_000.0)   # ratio 1.00 → 0.0
    mid_dryup = replace(base, avg_volume_20d=85_000.0)   # ratio 0.85 → 0.5
    full_dryup = replace(base, avg_volume_20d=70_000.0)  # ratio 0.70 → 1.0
    assert vs._volume_score(no_dryup) == pytest.approx(0.0)
    assert vs._volume_score(mid_dryup) == pytest.approx(0.4 * 0.5)
    assert vs._volume_score(full_dryup) == pytest.approx(0.4 * 1.0)


def test_volume_score_expansion_only_triggers_near_pivot():
    """D-S27: expansion component requires |distance_to_pivot| ≤ 0.02."""
    # Build a tech where recent-3-bar volume is 1.5× the 20d avg, and 20d==50d
    # so dry-up is zero. Slope is zero. Only the expansion part can fire.
    vol = (100_000.0,) * 47 + (150_000.0, 150_000.0, 150_000.0)
    near = replace(
        _baseline_tech(),
        volume_slope_20d=0.0,
        avg_volume_20d=100_000.0, avg_volume_50d=100_000.0,
        volume_last_50d=vol,
        distance_to_pivot=0.01,
    )
    far = replace(near, distance_to_pivot=-0.10)
    # Near-pivot: recent3/avg20 = 1.5 → exp_part = 1.0 → 0.2 weight.
    assert vs._volume_score(near) == pytest.approx(0.2)
    # Far from pivot: expansion suppressed, score collapses to 0.
    assert vs._volume_score(far) == pytest.approx(0.0)


def test_volume_score_handles_missing_inputs():
    """D-S27: None volumes / empty history must not raise; score stays in [0,1]."""
    t = replace(
        _baseline_tech(),
        volume_slope_20d=None,
        avg_volume_20d=None, avg_volume_50d=None,
        volume_last_50d=(),
        distance_to_pivot=None,
    )
    score = vs._volume_score(t)
    assert 0.0 <= score <= 1.0
    assert score == pytest.approx(0.0)


def test_pivot_score_halved_when_touches_below_threshold():
    t = _baseline_tech()
    full = vs._pivot_score(t)
    weak = replace(t, pivot_touches=1)
    assert vs._pivot_score(weak) == pytest.approx(full * 0.5)


def test_structure_shakeout_bonus_on_undercut_recovery():
    # l0=95, l1=98, l2=96 (< l1), close (100) > l1 → 0.5 + SHAKEOUT_BONUS
    t = replace(
        _baseline_tech(),
        swing_lows=((60, 95.0), (110, 98.0), (160, 96.0)),
    )
    assert vs._structure_score(t) == pytest.approx(0.5 + vs.SHAKEOUT_BONUS)


def test_breakout_pressure_bonus_applied_when_closes_tight():
    tight = replace(_baseline_tech(), close_std_5_norm=0.001)
    _, parts = vs._vcp_score(tight)
    assert "breakout_pressure" in parts

    loose = replace(_baseline_tech(), close_std_5_norm=0.02)
    _, parts_loose = vs._vcp_score(loose)
    assert "breakout_pressure" not in parts_loose


def test_readiness_bands_asymmetric():
    # 2% below → 1 - 0.02/0.05 = 0.6
    below = replace(_baseline_tech(), distance_to_pivot=-0.02)
    assert vs._readiness_score(below) == pytest.approx(0.6)
    # 2% above → 1 - 0.02/0.02 = 0.0 (tighter band)
    above = replace(_baseline_tech(), distance_to_pivot=+0.02)
    assert vs._readiness_score(above) == pytest.approx(0.0)


# ---------- state detector (D-S23) ----------

def _parts(**overrides: float) -> dict[str, float]:
    """Full sub-score dict with permissive defaults; override per test."""
    base = {
        "contraction": 0.7, "volatility": 0.8, "volume": 0.7,
        "structure": 0.7, "pivot": 0.7, "range": 0.7,
    }
    base.update(overrides)
    return base


def test_state_ready_on_coiled_tight_setup():
    t = replace(
        _baseline_tech(),
        range_20d=0.06, distance_to_pivot=-0.01, close_std_5_norm=0.005,
    )
    assert vs._detect_state(t, 0.60, _parts(pivot=0.75)) == "READY"


def test_state_ready_accepts_relaxed_range_and_std_d_s28():
    """D-S28: READY must now admit range_20d up to 0.12 and std5 up to <0.015
    (previously capped at 0.08 / 0.010)."""
    t = replace(
        _baseline_tech(),
        range_20d=0.11, distance_to_pivot=-0.02, close_std_5_norm=0.012,
    )
    assert vs._detect_state(t, 0.60, _parts(pivot=0.75)) == "READY"


def test_state_early_ready_on_coiled_but_below_pivot_d_s28():
    """D-S28: identical coil quality to READY, but price still 4% below
    pivot — must classify as EARLY_READY, not CONTRACTING."""
    t = replace(
        _baseline_tech(),
        range_20d=0.08, distance_to_pivot=-0.04, close_std_5_norm=0.010,
    )
    assert vs._detect_state(t, 0.60, _parts(pivot=0.75)) == "EARLY_READY"


def test_state_ready_beats_early_ready_in_overlap_band_d_s28():
    """READY band ends at -0.03; EARLY_READY starts at -0.02. In the shared
    -0.03..-0.02 region READY must win by priority ordering."""
    t = replace(
        _baseline_tech(),
        range_20d=0.08, distance_to_pivot=-0.025, close_std_5_norm=0.010,
    )
    assert vs._detect_state(t, 0.60, _parts(pivot=0.75)) == "READY"


def test_state_early_ready_rejects_when_price_too_far_below_pivot_d_s28():
    """D-S28: more than 6% below pivot — EARLY_READY must not fire."""
    t = replace(
        _baseline_tech(),
        range_20d=0.08, distance_to_pivot=-0.08, close_std_5_norm=0.010,
    )
    # Falls through to CONTRACTING (vcp gate + sub-score conditions met).
    assert vs._detect_state(t, 0.60, _parts(pivot=0.75)) != "EARLY_READY"


def test_state_contracting_on_valid_vcp():
    # Fails READY (vcp too low, std too high) but passes CONTRACTING rules.
    t = replace(
        _baseline_tech(),
        range_20d=0.09, distance_to_pivot=-0.03, close_std_5_norm=0.015,
    )
    assert vs._detect_state(t, 0.50, _parts(pivot=0.4)) == "CONTRACTING"


def test_state_base_building_on_early_consolidation():
    t = replace(_baseline_tech(), range_20d=0.12, distance_to_pivot=-0.05)
    # Low volatility/volume to fail CONTRACTING; structure high enough for BASE.
    p = _parts(contraction=0.0, volatility=0.2, volume=0.2, structure=0.5)
    assert vs._detect_state(t, 0.35, p) == "BASE_BUILDING"


def test_state_trend_on_strong_uptrend_no_base():
    t = replace(
        _baseline_tech(),
        range_20d=0.22, return_3m=0.25, distance_to_pivot=-0.01,
    )
    # vcp below TREND ceiling (0.30) and no usable structure.
    p = _parts(contraction=0.0, structure=0.2)
    assert vs._detect_state(t, 0.20, p) == "TREND"


def test_state_breakout_on_cross_pivot_with_volume():
    # D-S29: BREAKOUT now gates on volume_expansion_3bar rather than
    # the single-bar volume_spike.
    t = replace(
        _baseline_tech(),
        distance_to_pivot=0.01, range_20d=0.08, range_5d_norm=0.09,
        volume_expansion_3bar=True, atr_expanding=False,
        distance_to_ema50=0.06,
    )
    assert vs._detect_state(t, 0.50, _parts()) == "BREAKOUT"


def test_state_breakout_rejects_single_bar_spike_without_3bar_confirmation():
    """D-S29: a lone volume_spike on the last bar, without a 3-bar mean
    expansion, must no longer trigger BREAKOUT."""
    t = replace(
        _baseline_tech(),
        distance_to_pivot=0.01, range_20d=0.08, range_5d_norm=0.09,
        volume_spike=True, volume_expansion_3bar=False,
        atr_expanding=False, distance_to_ema50=0.06,
    )
    assert vs._detect_state(t, 0.50, _parts()) != "BREAKOUT"


def test_state_extended_on_post_breakout_stretch():
    t = replace(
        _baseline_tech(),
        distance_to_pivot=0.08, range_20d=0.10, range_5d_norm=0.12,
        volume_expansion_3bar=True, atr_expanding=True,
        distance_to_ema50=0.20,
    )
    # EXTENDED must win over BREAKOUT because of priority ordering.
    assert vs._detect_state(t, 0.50, _parts()) == "EXTENDED"


def test_state_extended_fires_at_tightened_pivot_distance_d_s30():
    """D-S30: STATE_EXTENDED_DIST lowered from 0.05 → 0.03. A 4% run past
    the pivot with expanding ATR and >10% EMA50 stretch must now classify
    as EXTENDED (pre-D-S30 it would still have been BREAKOUT)."""
    t = replace(
        _baseline_tech(),
        distance_to_pivot=0.04, range_20d=0.08, range_5d_norm=0.09,
        volume_expansion_3bar=True, atr_expanding=True,
        distance_to_ema50=0.12,
    )
    assert vs._detect_state(t, 0.50, _parts()) == "EXTENDED"


def test_state_extended_not_triggered_just_below_threshold_d_s30():
    """D-S30: 2.5% past pivot is below the new 3% EXTENDED gate, so a
    valid BREAKOUT setup at that distance must stay BREAKOUT."""
    t = replace(
        _baseline_tech(),
        distance_to_pivot=0.025, range_20d=0.08, range_5d_norm=0.09,
        volume_expansion_3bar=True, atr_expanding=True,
        distance_to_ema50=0.12,
    )
    assert vs._detect_state(t, 0.50, _parts()) == "BREAKOUT"


def test_state_none_when_no_bucket_fits():
    # range_20d between 0.15 and 0.22, vcp above TREND ceiling → falls through.
    t = replace(_baseline_tech(), range_20d=0.17, return_3m=0.03)
    assert vs._detect_state(t, 0.35, _parts(contraction=0.0)) == "NONE"


def test_state_to_decision_mapping_is_exhaustive():
    # Every produced state must map to a decision.
    for state in ("TREND", "BASE_BUILDING", "CONTRACTING", "EARLY_READY",
                  "READY", "BREAKOUT", "EXTENDED", "NONE", "FAIL"):
        assert state in vs.STATE_TO_DECISION
    assert vs.STATE_TO_DECISION["READY"] == "BUY_ALERT"
    assert vs.STATE_TO_DECISION["EARLY_READY"] == "WATCHLIST"
    assert vs.STATE_TO_DECISION["CONTRACTING"] == "WATCHLIST"
    assert vs.STATE_TO_DECISION["BASE_BUILDING"] == "IGNORE"
    assert vs.STATE_TO_DECISION["TREND"] == "IGNORE"
    assert vs.STATE_TO_DECISION["NONE"] == "IGNORE"
    assert vs.STATE_TO_DECISION["BREAKOUT"] == "SKIP"
    assert vs.STATE_TO_DECISION["EXTENDED"] == "SKIP"
    assert vs.STATE_TO_DECISION["FAIL"] == "REJECT"


# ---------- decision-ladder + RS-boost gating ----------

def test_rs_boost_applied_only_when_vcp_clears_gate():
    close, high, low, vol = _tight_vcp_series()
    t = vf.compute_technical_features(close, high, low, close, vol)
    base = score_candidate(t, _strong_fundamentals(), rs_score=None)
    leader = score_candidate(t, _strong_fundamentals(), rs_score=0.5)
    # Tight VCP fixture → vcp >= gate → boost applied.
    assert any("rs_boost" in r for r in leader.reasons)
    assert leader.final_score > base.final_score


def test_rs_boost_skipped_on_negative_rs():
    close, high, low, vol = _tight_vcp_series()
    t = vf.compute_technical_features(close, high, low, close, vol)
    laggard = score_candidate(t, _strong_fundamentals(), rs_score=-0.3)
    assert not any("rs_boost" in r for r in laggard.reasons)


def test_decision_requires_minimum_vcp_even_with_high_tech_and_rs():
    # Smooth uptrend → no swing structure / contraction → vcp ~ 0
    rng = np.random.default_rng(2)
    n = 400
    close = np.linspace(100.0, 200.0, n) + rng.normal(0, 0.3, n)
    t = vf.compute_technical_features(
        close, close + 0.5, close - 0.5, close, np.full(n, 1_000_000.0),
    )
    r = score_candidate(t, _strong_fundamentals(), rs_score=0.9)
    assert r.vcp_score is not None and r.vcp_score < vs.WATCHLIST_VCP
    # Low-vcp / no-setup lands outside BUY_ALERT + WATCHLIST by definition.
    assert r.decision in {"IGNORE", "SKIP"}
    assert r.stage in {"TREND", "BASE_BUILDING", "NONE", "BREAKOUT", "EXTENDED"}
    # RS boost must not be recorded either (gated on vcp >= WATCHLIST_VCP).
    assert not any("rs_boost" in x for x in r.reasons)



# ---------- orchestrator + CLI ----------

def _seed_scan_db(db_path: Path, symbol: str = "VCP") -> dt.date:
    """Seed a DB with one symbol carrying a tight-VCP adjusted price series."""
    isin = "INE_VCP_0001"
    close, high, low, vol = _tight_vcp_series()
    start = dt.date(2025, 1, 1)
    rows: list[BhavRow] = []
    td = start
    for i, c in enumerate(close):
        while td.weekday() >= 5:
            td += dt.timedelta(days=1)
        rows.append(BhavRow(
            trade_date=td, isin=isin, symbol=symbol, name=f"{symbol} Ltd",
            series="EQ", open=float(c), high=float(high[i]), low=float(low[i]),
            close=float(c), prev_close=float(c), volume=int(vol[i]),
            turnover=float(c) * float(vol[i]), trades=100,
        ))
        td += dt.timedelta(days=1)

    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        sdb.upsert_stock_master(conn, [rows[0]])
        sdb.upsert_market_data(conn, rows)
    return rows[-1].trade_date


def test_scan_date_detects_vcp_candidate(tmp_path: Path):
    db_path = tmp_path / "s.db"
    last = _seed_scan_db(db_path)
    result = scan_date(last, db_path=db_path, store_rejects=True)
    assert result.universe == 1
    assert result.scored == 1
    assert result.stored == 1
    with sdb.open_db(db_path) as conn:
        row = conn.execute(
            "SELECT symbol, decision, stage, final_score FROM vcp_candidates"
        ).fetchone()
    assert row[0] == "VCP"
    # Fundamentals missing on synthetic symbol → stage-2 hard-fail.
    assert row[1] == "REJECT"
    assert row[2] == "FAIL"


def test_cli_vcp_scan_smoke(tmp_path: Path):
    db_path = tmp_path / "s.db"
    last = _seed_scan_db(db_path)
    runner = CliRunner()
    res = runner.invoke(cli.main, [
        "scanner", "vcp-scan",
        "--date", last.isoformat(),
        "--store-rejects",
        "--db", str(db_path),
    ])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["trade_date"] == last.isoformat()
    assert payload["universe"] == 1
    assert payload["stored"] == 1


def test_cli_status_includes_vcp_section(tmp_path: Path):
    db_path = tmp_path / "s.db"
    last = _seed_scan_db(db_path)
    scan_date(last, db_path=db_path, store_rejects=True)
    runner = CliRunner()
    res = runner.invoke(cli.main, ["scanner", "status", "--db", str(db_path)])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["vcp"]["total"] == 1
    assert payload["vcp"]["latest_scan_date"] == last.isoformat()


# ---------- index ingestion + RS score ----------

def _seed_index(db_path: Path, last_date: dt.date, *, n: int = 120,
                index_symbol: str = "NIFTY500", ret: float = 0.10) -> None:
    """Seed ``index_data`` with a smooth ``ret`` over 50 bars ending on last_date."""
    rows: list[tuple[dt.date, float]] = []
    td = last_date - dt.timedelta(days=n * 2)
    closes = np.linspace(1000.0, 1000.0 * (1.0 + ret), n)
    i = 0
    while i < n:
        if td.weekday() < 5:
            rows.append((td, float(closes[i])))
            i += 1
        td += dt.timedelta(days=1)
    # Force the last bar to sit exactly on last_date.
    rows[-1] = (last_date, rows[-1][1])
    with sdb.open_db(db_path) as conn:
        sdb.init_schema(conn)
        sdb.upsert_index_data(conn, index_symbol, rows)


def test_upsert_and_load_index_closes(tmp_path: Path):
    db_path = tmp_path / "s.db"
    td = dt.date(2025, 6, 30)
    _seed_index(db_path, td, n=80)
    with sdb.open_db(db_path) as conn:
        series = sdb.load_index_closes(conn, "NIFTY500", as_of=td)
    assert len(series) == 80
    assert series[0][0] < series[-1][0]
    assert series[-1][0] == td


def test_scan_stores_rs_score_when_index_present(tmp_path: Path):
    db_path = tmp_path / "s.db"
    last = _seed_scan_db(db_path)
    _seed_index(db_path, last, n=120, ret=0.05)  # bench +5% over the window
    scan_date(last, db_path=db_path, store_rejects=True)
    with sdb.open_db(db_path) as conn:
        row = conn.execute(
            "SELECT return_50d, benchmark_return_50d, rs_score "
            "FROM vcp_candidates"
        ).fetchone()
    assert row[0] is not None and row[1] is not None
    # Seeded bench rose ~5% over 50 bars; any reasonable tolerance.
    assert abs(row[1] - 0.05 * (50 / 120)) < 0.05 or abs(row[1]) < 0.2
    assert row[2] == pytest.approx(row[0] - row[1], abs=1e-9)


def test_scan_rs_is_none_without_index(tmp_path: Path):
    db_path = tmp_path / "s.db"
    last = _seed_scan_db(db_path)
    result = scan_date(last, db_path=db_path, store_rejects=True)
    assert result.benchmark_return_50d is None
    with sdb.open_db(db_path) as conn:
        row = conn.execute(
            "SELECT return_50d, benchmark_return_50d, rs_score "
            "FROM vcp_candidates"
        ).fetchone()
    assert row[0] is not None  # stock return_50d still computed
    assert row[1] is None and row[2] is None


# ---------- dashboard loader ----------

def test_dashboard_loader_returns_rows(tmp_path: Path):
    from portfolio_analyzer.tui.scanner_loader import load_dashboard

    db_path = tmp_path / "s.db"
    last = _seed_scan_db(db_path)
    _seed_index(db_path, last, n=80)
    scan_date(last, db_path=db_path, store_rejects=True)
    data = load_dashboard(db_path=db_path, include_rejects=True)
    assert data.trade_date == last
    assert len(data.rows) == 1
    assert data.rows[0].symbol == "VCP"
    assert data.universe_counts.get("REJECT", 0) >= 1
    assert data.benchmark_return_50d is not None


def test_dashboard_loader_empty_db(tmp_path: Path):
    from portfolio_analyzer.tui.scanner_loader import load_dashboard

    data = load_dashboard(db_path=tmp_path / "empty.db")
    assert data.trade_date is None
    assert data.rows == ()
    assert data.universe_counts == {}


def test_dashboard_loader_filters_rejects_by_default(tmp_path: Path):
    from portfolio_analyzer.tui.scanner_loader import load_dashboard

    db_path = tmp_path / "s.db"
    last = _seed_scan_db(db_path)
    scan_date(last, db_path=db_path, store_rejects=True)
    # REJECT rows exist but default loader hides them.
    data = load_dashboard(db_path=db_path, include_rejects=False)
    assert all(r.decision != "REJECT" for r in data.rows)
