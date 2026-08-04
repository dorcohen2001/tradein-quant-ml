"""Candlestick reversal/exhaustion pattern detection for ITF trigger confirmation.

Pure functions over OHLC rows (pandas Series with Open/High/Low/Close) — no
indicator state, so they compose cleanly with the volume/MA/Fib checks in
`ta.mtfa`.
"""
from __future__ import annotations

import pandas as pd

from config import BRAKING_PIN_BAR_WICK_RATIO, BRAKING_SMALL_BODY_RATIO, BRAKING_VOLUME_CLIMAX_MULT
from ta.ta_engine import volume_spike


def _body(row: pd.Series) -> float:
    return abs(float(row["Close"]) - float(row["Open"]))


def _range(row: pd.Series) -> float:
    return float(row["High"]) - float(row["Low"])


def _upper_wick(row: pd.Series) -> float:
    return float(row["High"]) - max(float(row["Open"]), float(row["Close"]))


def _lower_wick(row: pd.Series) -> float:
    return min(float(row["Open"]), float(row["Close"])) - float(row["Low"])


def is_doji(row: pd.Series, body_ratio_max: float = 0.1) -> bool:
    rng = _range(row)
    if rng <= 0:
        return False
    return (_body(row) / rng) <= body_ratio_max


def is_hammer(row: pd.Series, body_ratio_max: float = 0.35, wick_mult: float = 2.0) -> bool:
    """Small body near the top of the range, long lower wick, short upper
    wick — bullish exhaustion/reversal candle."""
    rng = _range(row)
    body = _body(row)
    if rng <= 0 or body <= 0:
        return False
    lower, upper = _lower_wick(row), _upper_wick(row)
    return (body / rng) <= body_ratio_max and lower >= wick_mult * body and upper <= body


def is_shooting_star(row: pd.Series, body_ratio_max: float = 0.35, wick_mult: float = 2.0) -> bool:
    """Mirror of `is_hammer` — small body near the bottom, long upper wick.
    Bearish exhaustion/reversal candle."""
    rng = _range(row)
    body = _body(row)
    if rng <= 0 or body <= 0:
        return False
    lower, upper = _lower_wick(row), _upper_wick(row)
    return (body / rng) <= body_ratio_max and upper >= wick_mult * body and lower <= body
def is_bullish_engulfing(prev: pd.Series, curr: pd.Series) -> bool:
    prev_bearish = float(prev["Close"]) < float(prev["Open"])
    curr_bullish = float(curr["Close"]) > float(curr["Open"])
    if not (prev_bearish and curr_bullish):
        return False
    return float(curr["Open"]) <= float(prev["Close"]) and float(curr["Close"]) >= float(prev["Open"])


def is_bearish_engulfing(prev: pd.Series, curr: pd.Series) -> bool:
    prev_bullish = float(prev["Close"]) > float(prev["Open"])
    curr_bearish = float(curr["Close"]) < float(curr["Open"])
    if not (prev_bullish and curr_bearish):
        return False
    return float(curr["Open"]) >= float(prev["Close"]) and float(curr["Close"]) <= float(prev["Open"])


def classify_reversal_candle(df: pd.DataFrame) -> dict:
    """Inspect the last two rows of `df` for an exhaustion/reversal pattern.
    Returns {"is_reversal": bool, "pattern": str|None, "direction": "bullish"|"bearish"|None}."""
    if len(df) < 2:
        return {"is_reversal": False, "pattern": None, "direction": None}

    prev, curr = df.iloc[-2], df.iloc[-1]

    if is_bullish_engulfing(prev, curr):
        return {"is_reversal": True, "pattern": "bullish_engulfing", "direction": "bullish"}
    if is_bearish_engulfing(prev, curr):
        return {"is_reversal": True, "pattern": "bearish_engulfing", "direction": "bearish"}
    if is_hammer(curr):
        return {"is_reversal": True, "pattern": "hammer", "direction": "bullish"}
    if is_shooting_star(curr):
        return {"is_reversal": True, "pattern": "shooting_star", "direction": "bearish"}
    if is_doji(curr):
        return {"is_reversal": True, "pattern": "doji", "direction": None}

    return {"is_reversal": False, "pattern": None, "direction": None}


def is_pin_bar_rejection(row: pd.Series, direction: str, wick_ratio: float = BRAKING_PIN_BAR_WICK_RATIO) -> bool:
    """Looser rejection-candle check than `is_hammer`/`is_shooting_star`: the
    wick in `direction`'s favor just has to occupy at least `wick_ratio` of
    the candle's total range -- no constraint on body size or the opposite
    wick. `direction="LONG"` checks the lower wick (bullish rejection off a
    low), `direction="SHORT"` the upper wick (bearish rejection off a high)."""
    rng = _range(row)
    if rng <= 0:
        return False
    wick = _lower_wick(row) if direction == "LONG" else _upper_wick(row)
    return (wick / rng) >= wick_ratio


def is_volume_climax(
    df: pd.DataFrame, body_ratio_max: float = BRAKING_SMALL_BODY_RATIO,
    vol_mult: float = BRAKING_VOLUME_CLIMAX_MULT, lookback: int = 20,
) -> bool:
    """Volume climax / absorption: the current bar's volume is well above its
    own N-bar average while the candle's body stays small -- high participation
    without much net price movement, the signature of large size being
    absorbed rather than driving price further. Direction-agnostic: exhaustion
    of the prior move is the signal, regardless of which way the tiny body leans."""
    if df.empty:
        return False
    curr = df.iloc[-1]
    rng = _range(curr)
    if rng <= 0:
        return False
    small_body = (_body(curr) / rng) <= body_ratio_max
    if not small_body:
        return False
    vol_ok, _ratio = volume_spike(df, mult=vol_mult, lookback=lookback)
    return vol_ok


def is_structure_shift(df: pd.DataFrame, direction: str, lookback: int) -> bool:
    """Market Structure Shift: the current bar breaks the prior `lookback`
    bars' swing high (direction="LONG", a bullish MSS confirming the reversal
    up) or swing low (direction="SHORT", bearish MSS confirming the reversal
    down)."""
    if len(df) <= lookback:
        return False
    recent = df.iloc[-(lookback + 1):-1]
    curr = df.iloc[-1]
    if direction == "LONG":
        return float(curr["High"]) > float(recent["High"].max())
    return float(curr["Low"]) < float(recent["Low"].min())
