"""Technical Analysis Engine: moving averages, Fibonacci levels, structure, momentum."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import FIB_ZONE, MA_PROXIMITY_PCT, VOLUME_SPIKE_MULT


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------
def add_moving_averages(df: pd.DataFrame, periods=(20, 50, 200)) -> pd.DataFrame:
    out = df.copy()
    for p in periods:
        out[f"EMA{p}"] = out["Close"].ewm(span=p, adjust=False).mean()
        out[f"SMA{p}"] = out["Close"].rolling(p).mean()
    return out


def ma_confluence(price: float, df: pd.DataFrame, tolerance_pct: float = MA_PROXIMITY_PCT) -> list[dict]:
    """Return list of MAs (50/200 EMA) the price is currently hugging within tolerance."""
    hits = []
    last = df.iloc[-1]
    for p in (50, 200):
        col = f"EMA{p}"
        if col not in df.columns or pd.isna(last[col]):
            continue
        ma_val = float(last[col])
        dist_pct = (price - ma_val) / ma_val
        if abs(dist_pct) <= tolerance_pct:
            hits.append({"ma": col, "value": round(ma_val, 4), "distance_pct": round(dist_pct * 100, 2)})
    return hits


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


def in_golden_zone(price: float, fib: dict[str, float], zone: tuple[float, float] = FIB_ZONE) -> bool:
    lower, upper = sorted((fib[str(zone[0])], fib[str(zone[1])]))
    return lower <= price <= upper


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


def volume_spike(df: pd.DataFrame, mult: float = VOLUME_SPIKE_MULT, lookback: int = 20) -> tuple[bool, float]:
    avg_vol = df["Volume"].tail(lookback + 1).iloc[:-1].mean()
    last_vol = df["Volume"].iloc[-1]
    if avg_vol == 0 or pd.isna(avg_vol):
        return False, 0.0
    ratio = last_vol / avg_vol
    return ratio >= mult, round(ratio, 2)


# ---------------------------------------------------------------------------
# Aggregate result
# ---------------------------------------------------------------------------
@dataclass
class TAResult:
    ticker: str
    price: float
    trend_1d: str
    ma_hits: list = field(default_factory=list)
    fib_levels: dict = field(default_factory=dict)
    in_golden_zone: bool = False
    swing_high: float = 0.0
    swing_low: float = 0.0
    levels: dict = field(default_factory=dict)
    rsi_value: float = 50.0
    macd_bullish: bool = False
    vol_spike: bool = False
    vol_ratio: float = 0.0
    direction_bias: str = "NEUTRAL"  # LONG / SHORT / NEUTRAL
    thesis: str = ""


def determine_trend(df: pd.DataFrame) -> str:
    last = df.iloc[-1]
    if pd.isna(last.get("EMA50")) or pd.isna(last.get("EMA200")):
        return "UNKNOWN"
    if last["Close"] > last["EMA50"] > last["EMA200"]:
        return "UPTREND"
    if last["Close"] < last["EMA50"] < last["EMA200"]:
        return "DOWNTREND"
    return "MIXED"


def analyze(ticker: str, df_1d: pd.DataFrame, swing_lookback: int = 60) -> TAResult:
    df_1d = add_moving_averages(df_1d)
    price = float(df_1d["Close"].iloc[-1])
    trend = determine_trend(df_1d)
    hits = ma_confluence(price, df_1d)
    hi, lo, _, _ = find_swing_high_low(df_1d, lookback=swing_lookback)
    fib = fibonacci_levels(hi, lo)
    golden = in_golden_zone(price, fib)
    levels = pivot_levels(df_1d)
    near = nearest_levels(price, levels)
    rsi_series = rsi(df_1d["Close"])
    rsi_val = float(rsi_series.iloc[-1]) if not pd.isna(rsi_series.iloc[-1]) else 50.0
    macd_df = macd(df_1d["Close"])
    macd_bull = bool(macd_df["hist"].iloc[-1] > 0)
    spike, ratio = volume_spike(df_1d)

    direction = "NEUTRAL"
    thesis_bits = []
    if trend == "UPTREND" and (hits or golden):
        direction = "LONG"
    elif trend == "DOWNTREND" and (hits or golden):
        direction = "SHORT"

    if hits:
        thesis_bits.append(
            "Price hugging " + ", ".join(f"{h['ma']} ({h['distance_pct']:+.2f}%)" for h in hits)
        )
    if golden:
        thesis_bits.append(f"Inside Fibonacci golden zone ({fib['0.618']:.4g}-{fib['0.5']:.4g})")
    if near.get("nearest_support"):
        thesis_bits.append(f"Support near {near['nearest_support']:.4g}")
    if near.get("nearest_resistance"):
        thesis_bits.append(f"Resistance near {near['nearest_resistance']:.4g}")
    thesis_bits.append(f"Trend: {trend}, RSI {rsi_val:.1f}, MACD {'bullish' if macd_bull else 'bearish'}")
    if spike:
        thesis_bits.append(f"Volume spike {ratio}x 20-day avg")

    return TAResult(
        ticker=ticker,
        price=price,
        trend_1d=trend,
        ma_hits=hits,
        fib_levels=fib,
        in_golden_zone=golden,
        swing_high=hi,
        swing_low=lo,
        levels={**levels, **near},
        rsi_value=rsi_val,
        macd_bullish=macd_bull,
        vol_spike=spike,
        vol_ratio=ratio,
        direction_bias=direction,
        thesis="; ".join(thesis_bits),
    )
