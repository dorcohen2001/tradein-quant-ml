"""RSI Divergence Engine (Accumulation / Distribution).

Mirrors the popular TradingView "Divergence Indicator" convention: pivots are
located on the RSI series itself (not raw price) using a left/right bar
lookback, then the price extreme at each RSI pivot bar is compared between
the two most recent qualifying pivots.

  Accumulation (Bullish/Regular divergence): price Low #2 < price Low #1
  while RSI Low #2 > RSI Low #1 (price makes a lower low, RSI makes a higher
  low -- selling momentum is fading).

  Distribution / Unloading (Bearish/Regular divergence): price High #2 >
  price High #1 while RSI High #2 < RSI High #1 (price makes a higher high,
  RSI makes a lower high -- buying momentum is fading).

Default parameters match the spec: RSI period 14, pivot lookback left 16 /
right 5.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import DIVERGENCE_MAX_BAR_GAP, DIVERGENCE_MIN_BAR_GAP, RSI_PERIOD, RSI_PIVOT_LEFT, RSI_PIVOT_RIGHT
from ta.ta_engine import rsi as compute_rsi


def pivot_low(series: pd.Series, left: int, right: int) -> list[int]:
    """Integer positions where `series` is strictly the minimum over the
    window [i-left, i+right]. A pivot at position i is only "confirmed" once
    `right` further bars exist, i.e. it cannot be known live until then."""
    vals = series.to_numpy()
    n = len(vals)
    out = []
    for i in range(left, n - right):
        window = vals[i - left: i + right + 1]
        if np.isnan(vals[i]) or np.isnan(window).any():
            continue
        if vals[i] == window.min() and np.count_nonzero(window == vals[i]) == 1:
            out.append(i)
    return out


def pivot_high(series: pd.Series, left: int, right: int) -> list[int]:
    vals = series.to_numpy()
    n = len(vals)
    out = []
    for i in range(left, n - right):
        window = vals[i - left: i + right + 1]
        if np.isnan(vals[i]) or np.isnan(window).any():
            continue
        if vals[i] == window.max() and np.count_nonzero(window == vals[i]) == 1:
            out.append(i)
    return out


@dataclass
class DivergenceResult:
    bullish: bool = False   # Accumulation
    bearish: bool = False   # Distribution / Unloading
    bullish_pivots: tuple[int, int] | None = None   # (older_idx, newer_idx) on RSI-low pivots
    bearish_pivots: tuple[int, int] | None = None   # (older_idx, newer_idx) on RSI-high pivots
    detail: dict = field(default_factory=dict)


def rsi_divergence(
    df: pd.DataFrame,
    rsi_period: int = RSI_PERIOD,
    left: int = RSI_PIVOT_LEFT,
    right: int = RSI_PIVOT_RIGHT,
    min_gap: int = DIVERGENCE_MIN_BAR_GAP,
    max_gap: int = DIVERGENCE_MAX_BAR_GAP,
) -> DivergenceResult:
    if len(df) < left + right + min_gap + 2:
        return DivergenceResult()

    rsi_series = compute_rsi(df["Close"], period=rsi_period)
    lows = pivot_low(rsi_series, left, right)
    highs = pivot_high(rsi_series, left, right)

    result = DivergenceResult()

    if len(lows) >= 2:
        i1, i2 = lows[-2], lows[-1]
        gap = i2 - i1
        if min_gap <= gap <= max_gap:
            rsi1, rsi2 = float(rsi_series.iloc[i1]), float(rsi_series.iloc[i2])
            price1, price2 = float(df["Low"].iloc[i1]), float(df["Low"].iloc[i2])
            if rsi2 > rsi1 and price2 < price1:
                result.bullish = True
                result.bullish_pivots = (i1, i2)
                result.detail["bullish"] = {
                    "rsi_low_1": round(rsi1, 2), "rsi_low_2": round(rsi2, 2),
                    "price_low_1": price1, "price_low_2": price2,
                }

    if len(highs) >= 2:
        i1, i2 = highs[-2], highs[-1]
        gap = i2 - i1
        if min_gap <= gap <= max_gap:
            rsi1, rsi2 = float(rsi_series.iloc[i1]), float(rsi_series.iloc[i2])
            price1, price2 = float(df["High"].iloc[i1]), float(df["High"].iloc[i2])
            if rsi2 < rsi1 and price2 > price1:
                result.bearish = True
                result.bearish_pivots = (i1, i2)
                result.detail["bearish"] = {
                    "rsi_high_1": round(rsi1, 2), "rsi_high_2": round(rsi2, 2),
                    "price_high_1": price1, "price_high_2": price2,
                }

    return result
