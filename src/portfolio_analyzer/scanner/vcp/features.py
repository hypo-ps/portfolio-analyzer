"""Technical feature extraction for the VCP scanner.

All inputs are adjusted OHLCV arrays (numpy). All features are computed as
of the last bar in the input; callers are responsible for slicing the panel
to ``[... , as_of]`` before calling.

Conventions:
- ``return_*`` values are decimals (0.10 = +10%).
- ``distance_*`` values are decimals (0.05 = 5% away).
- Swings are 5-bar fractals: a high is a swing-high if it equals the max of
  the 11-bar window centered on it; symmetric for lows.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Minimum history so EMA200 and 1y return stabilize.
MIN_BARS = 252
SWING_FRACTAL_N = 5


@dataclass(frozen=True)
class TechnicalFeatures:
    close: float
    ema50: float | None
    ema200: float | None
    ema50_slope_20d: float | None
    atr14: float | None
    atr50: float | None
    atr5_recent: float | None
    atr30_trailing: float | None
    return_1y: float | None
    return_3m: float | None
    high_52w: float | None
    low_52w: float | None
    distance_from_52w_high: float | None
    avg_volume_20d: float | None
    avg_volume_50d: float | None
    avg_turnover_20d_cr: float | None
    range_20d: float | None
    pivot: float | None
    pivot_range: float | None
    distance_to_pivot: float | None
    volume_slope_20d: float | None
    # Last-3 swing structure (higher-lows, contracting ranges)
    swing_highs: tuple[tuple[int, float], ...]
    swing_lows: tuple[tuple[int, float], ...]


def _ema(values: np.ndarray, span: int) -> np.ndarray | None:
    """Standard EMA seeded with an SMA of the first ``span`` values.

    Returns an array of the same length as ``values`` (NaN before the seed).
    Returns ``None`` if there aren't enough bars.
    """
    if values.size < span:
        return None
    alpha = 2.0 / (span + 1)
    out = np.full(values.size, np.nan)
    seed = values[:span].mean()
    out[span - 1] = seed
    for i in range(span, values.size):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    return np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])


def _wilder(values: np.ndarray, n: int) -> float | None:
    """Wilder's smoothed mean over the final position of ``values``."""
    if values.size < n:
        return None
    smoothed = values[:n].mean()
    for v in values[n:]:
        smoothed = (smoothed * (n - 1) + v) / n
    return float(smoothed)


def _find_swings(
    highs: np.ndarray, lows: np.ndarray, n: int = SWING_FRACTAL_N,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    sh: list[tuple[int, float]] = []
    sl: list[tuple[int, float]] = []
    for i in range(n, len(highs) - n):
        window_hi = highs[i - n:i + n + 1]
        window_lo = lows[i - n:i + n + 1]
        if highs[i] == window_hi.max():
            sh.append((i, float(highs[i])))
        if lows[i] == window_lo.min():
            sl.append((i, float(lows[i])))
    return sh, sl


def _volume_slope(volume: np.ndarray, window: int = 20) -> float | None:
    if volume.size < window:
        return None
    v = volume[-window:].astype(float)
    v = np.where(v <= 0, 1.0, v)  # guard against log(0)
    y = np.log(v)
    x = np.arange(window, dtype=float)
    slope, _ = np.polyfit(x, y, 1)
    return float(slope)


def compute_technical_features(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
) -> TechnicalFeatures | None:
    """Compute all technical features as of the last bar in the input arrays.

    All inputs must be the same length, ordered oldest -> newest, and already
    corporate-action adjusted. Returns ``None`` if fewer than ``MIN_BARS``
    bars are supplied (so EMA200 / 1y return are meaningful).
    """
    n = closes.size
    if n < MIN_BARS:
        return None

    close = float(closes[-1])

    # Moving averages
    ema50_arr = _ema(closes, 50)
    ema200_arr = _ema(closes, 200)
    ema50 = float(ema50_arr[-1]) if ema50_arr is not None else None
    ema200 = float(ema200_arr[-1]) if ema200_arr is not None else None
    ema50_slope_20d: float | None = None
    if ema50_arr is not None and n >= 70:
        prior = ema50_arr[-21]
        if prior and not np.isnan(prior):
            ema50_slope_20d = float((ema50_arr[-1] - prior) / prior)

    # ATR family
    tr = _true_range(highs, lows, closes)
    atr14 = _wilder(tr, 14)
    atr50 = _wilder(tr, 50)
    atr5_recent = float(tr[-5:].mean()) if tr.size >= 5 else None
    atr30_trailing: float | None = None
    if tr.size >= 35:
        atr30_trailing = float(tr[-35:-5].mean())

    # Returns
    return_1y: float | None = None
    if n >= 252:
        base = float(closes[-252])
        if base > 0:
            return_1y = (close - base) / base
    return_3m: float | None = None
    if n >= 63:
        base = float(closes[-63])
        if base > 0:
            return_3m = (close - base) / base

    # 52-week band
    window_52w = closes[-252:]
    highs_52w = highs[-252:]
    lows_52w = lows[-252:]
    high_52w = float(highs_52w.max())
    low_52w = float(lows_52w.min())
    distance_from_52w_high = (
        (close - high_52w) / high_52w if high_52w > 0 else None
    )
    _ = window_52w  # kept for symmetry with review/debug

    # Volume / turnover
    avg_volume_20d = float(volumes[-20:].mean()) if n >= 20 else None
    avg_volume_50d = float(volumes[-50:].mean()) if n >= 50 else None
    avg_turnover_20d_cr: float | None = None
    if n >= 20:
        turnover = closes[-20:] * volumes[-20:]
        avg_turnover_20d_cr = float(turnover.mean() / 1e7)

    # 20-day normalized range
    range_20d: float | None = None
    if n >= 20:
        hh = float(highs[-20:].max())
        ll = float(lows[-20:].min())
        if close > 0:
            range_20d = (hh - ll) / close

    # Pivot = max close of last 10 bars
    pivot: float | None = None
    pivot_range: float | None = None
    distance_to_pivot: float | None = None
    if n >= 10:
        pivot = float(closes[-10:].max())
        hh10 = float(highs[-10:].max())
        ll10 = float(lows[-10:].min())
        if close > 0:
            pivot_range = (hh10 - ll10) / close
            distance_to_pivot = (close - pivot) / pivot if pivot > 0 else None

    volume_slope_20d = _volume_slope(volumes)

    # Swing structure — last 3 highs / 3 lows
    sh_all, sl_all = _find_swings(highs, lows)
    swing_highs = tuple(sh_all[-3:])
    swing_lows = tuple(sl_all[-3:])

    return TechnicalFeatures(
        close=close,
        ema50=ema50,
        ema200=ema200,
        ema50_slope_20d=ema50_slope_20d,
        atr14=atr14,
        atr50=atr50,
        atr5_recent=atr5_recent,
        atr30_trailing=atr30_trailing,
        return_1y=return_1y,
        return_3m=return_3m,
        high_52w=high_52w,
        low_52w=low_52w,
        distance_from_52w_high=distance_from_52w_high,
        avg_volume_20d=avg_volume_20d,
        avg_volume_50d=avg_volume_50d,
        avg_turnover_20d_cr=avg_turnover_20d_cr,
        range_20d=range_20d,
        pivot=pivot,
        pivot_range=pivot_range,
        distance_to_pivot=distance_to_pivot,
        volume_slope_20d=volume_slope_20d,
        swing_highs=swing_highs,
        swing_lows=swing_lows,
    )

