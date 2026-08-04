"""Technical Analysis Engine: moving averages, Fibonacci levels, structure, momentum."""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import PRIMARY_MA_PERIOD, SECONDARY_MA_PERIOD, VOLUME_SPIKE_MULT


# ---------------------------------------------------------------------------
# Moving averages -- EMA20/EMA50 are deprecated as trend inputs; PRIMARY_MA_PERIOD
# (200) is the absolute primary trend benchmark, SECONDARY_MA_PERIOD (150) the
# secondary filter. Both come from config.py as the single source of truth.
# ---------------------------------------------------------------------------
def add_moving_averages(df: pd.DataFrame, periods=(SECONDARY_MA_PERIOD, PRIMARY_MA_PERIOD)) -> pd.DataFrame:
    out = df.copy()
    for p in periods:
        out[f"EMA{p}"] = out["Close"].ewm(span=p, adjust=False).mean()
        out[f"SMA{p}"] = out["Close"].rolling(p).mean()
    return out


# ---------------------------------------------------------------------------
# Swing structure / Fibonacci
# ---------------------------------------------------------------------------
def find_swing_high_low(df: pd.DataFrame, lookback: int = 60) -> tuple[float, float, pd.Timestamp, pd.Timestamp]:
    window = df.tail(lookback)
    hi_idx = window["High"].idxmax()
    lo_idx = window["Low"].idxmin()
    return float(window.loc[hi_idx, "High"]), float(window.loc[lo_idx, "Low"]), hi_idx, lo_idx


def fibonacci_levels(swing_high: float, swing_low: float) -> dict[str, float]:
    diff = swing_high - swing_low
    return {
        "0.0": swing_high,
        "0.382": swing_high - 0.382 * diff,
        "0.5": swing_high - 0.5 * diff,
        "0.618": swing_high - 0.618 * diff,
        "0.786": swing_high - 0.786 * diff,
        "1.0": swing_low,
    }


# ---------------------------------------------------------------------------
# Support / Resistance via simple pivot detection
# ---------------------------------------------------------------------------
def pivot_levels(df: pd.DataFrame, window: int = 5, lookback: int = 120) -> dict[str, list[float]]:
    d = df.tail(lookback)
    highs, lows = [], []
    h, l = d["High"].values, d["Low"].values
    for i in range(window, len(d) - window):
        if h[i] == max(h[i - window:i + window + 1]):
            highs.append(float(h[i]))
        if l[i] == min(l[i - window:i + window + 1]):
            lows.append(float(l[i]))
    return {"resistance": sorted(set(round(x, 4) for x in highs), reverse=True)[:5],
            "support": sorted(set(round(x, 4) for x in lows))[-5:]}


def pivot_points_indexed(df: pd.DataFrame, window: int = 3, lookback: int = 104) -> tuple[list, list]:
    """Same fractal pivot-high/low detection as `pivot_levels`, but keeping
    each pivot's integer bar position (not just its price) so a caller can
    fit a line through them -- used by `ta.mtfa`'s diagonal trendline zone
    detection, which needs two pivots' relative *positions* to compute a
    slope, not just their price levels. Returns (highs, lows), each a list
    of (position, price) tuples in chronological order, position being the
    row index within `df.tail(lookback)` (0 = oldest bar in that window)."""
    d = df.tail(lookback).reset_index(drop=True)
    h, l = d["High"].values, d["Low"].values
    highs, lows = [], []
    for i in range(window, len(d) - window):
        if h[i] == max(h[i - window:i + window + 1]):
            highs.append((i, float(h[i])))
        if l[i] == min(l[i - window:i + window + 1]):
            lows.append((i, float(l[i])))
    return highs, lows


def nearest_levels(price: float, levels: dict[str, list[float]]) -> dict:
    supports_below = [s for s in levels["support"] if s < price]
    resistances_above = [r for r in levels["resistance"] if r > price]
    return {
        "nearest_support": max(supports_below) if supports_below else None,
        "nearest_resistance": min(resistances_above) if resistances_above else None,
    }


# ---------------------------------------------------------------------------
# Momentum: RSI / MACD
# ---------------------------------------------------------------------------
def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": macd_line - signal_line})


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing), used for order-block impulse
    strength and stop-buffer sizing."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def volume_spike(df: pd.DataFrame, mult: float = VOLUME_SPIKE_MULT, lookback: int = 20) -> tuple[bool, float]:
    avg_vol = df["Volume"].tail(lookback + 1).iloc[:-1].mean()
    last_vol = df["Volume"].iloc[-1]
    if avg_vol == 0 or pd.isna(avg_vol):
        return False, 0.0
    ratio = last_vol / avg_vol
    return ratio >= mult, round(ratio, 2)


# ---------------------------------------------------------------------------
# Trend classification
# ---------------------------------------------------------------------------
def determine_trend(df: pd.DataFrame) -> str:
    """UPTREND/DOWNTREND requires Close and the secondary EMA to both be on
    the same side of the primary EMA (150/200 by default) -- the same
    fast-above-slow structure the old 50/200 pairing used, just with
    EMA20/EMA50 deprecated as trend inputs per config.py."""
    secondary_col, primary_col = f"EMA{SECONDARY_MA_PERIOD}", f"EMA{PRIMARY_MA_PERIOD}"
    last = df.iloc[-1]
    if pd.isna(last.get(secondary_col)) or pd.isna(last.get(primary_col)):
        return "UNKNOWN"
    if last["Close"] > last[secondary_col] > last[primary_col]:
        return "UPTREND"
    if last["Close"] < last[secondary_col] < last[primary_col]:
        return "DOWNTREND"
    return "MIXED"
