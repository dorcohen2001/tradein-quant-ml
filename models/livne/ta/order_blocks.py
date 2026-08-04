"""Order Block (OB) detection on HTF candles.

Heuristic (standard order-flow definition): an Order Block is the last
opposite-colored candle immediately preceding an impulsive candle that (a)
moves at least `impulse_atr_mult` * ATR and (b) closes beyond the recent
swing high/low, i.e. actually breaks market structure rather than just being
a big-range bar inside a range.

This is a heuristic approximation, not a proprietary indicator's exact
algorithm — there is no single canonical OB definition.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config import OB_IMPULSE_ATR_MULT, OB_LOOKBACK_BARS, OB_STRUCTURE_LOOKBACK
from ta.ta_engine import atr as compute_atr


@dataclass
class OrderBlock:
    type: str          # "bullish" or "bearish"
    top: float
    bottom: float
    formed_at: pd.Timestamp
    impulse_at: pd.Timestamp
    mitigated: bool


def _find_base_candle(df: pd.DataFrame, impulse_idx: int, impulse_bullish: bool) -> int | None:
    """Walk back from the impulse candle to the nearest candle of the
    opposite color -- that candle is the order block's base."""
    j = impulse_idx - 1
    while j >= 0:
        row = df.iloc[j]
        is_bearish = float(row["Close"]) < float(row["Open"])
        if impulse_bullish and is_bearish:
            return j
        if not impulse_bullish and not is_bearish:
            return j
        j -= 1
        if impulse_idx - j > 5:  # base candle should be adjacent, not deep in the past
            return None
    return None


def _is_mitigated(df: pd.DataFrame, start_idx: int, top: float, bottom: float) -> bool:
    """True if any bar after the OB formed has traded back into [bottom, top]."""
    if start_idx + 1 >= len(df):
        return False
    future = df.iloc[start_idx + 1:]
    return bool(((future["Low"] <= top) & (future["High"] >= bottom)).any())


def find_order_blocks(
    df: pd.DataFrame,
    lookback: int = OB_LOOKBACK_BARS,
    impulse_atr_mult: float = OB_IMPULSE_ATR_MULT,
    structure_lookback: int = OB_STRUCTURE_LOOKBACK,
    max_zones: int = 5,
) -> list[OrderBlock]:
    if len(df) < structure_lookback + 10:
        return []

    window_start = max(structure_lookback, len(df) - lookback)
    atr_series = compute_atr(df)

    zones: list[OrderBlock] = []
    for i in range(window_start, len(df)):
        row = df.iloc[i]
        atr_val = float(atr_series.iloc[i])
        if pd.isna(atr_val) or atr_val <= 0:
            continue

        body = float(row["Close"]) - float(row["Open"])
        if abs(body) < impulse_atr_mult * atr_val:
            continue

        impulse_bullish = body > 0
        prior = df.iloc[i - structure_lookback:i]
        if impulse_bullish:
            structure_broken = float(row["Close"]) > float(prior["High"].max())
        else:
            structure_broken = float(row["Close"]) < float(prior["Low"].min())
        if not structure_broken:
            continue

        base_idx = _find_base_candle(df, i, impulse_bullish)
        if base_idx is None:
            continue

        base = df.iloc[base_idx]
        top, bottom = float(base["High"]), float(base["Low"])
        if top <= bottom:
            continue

        mitigated = _is_mitigated(df, i, top, bottom)
        zones.append(OrderBlock(
            type="bullish" if impulse_bullish else "bearish",
            top=top, bottom=bottom,
            formed_at=df.index[base_idx], impulse_at=df.index[i],
            mitigated=mitigated,
        ))

    zones.sort(key=lambda z: z.impulse_at, reverse=True)
    return zones[:max_zones]


def nearest_unmitigated(zones: list[OrderBlock], price: float, direction: str) -> OrderBlock | None:
    """Nearest unmitigated OB below price (for LONG structural support) or
    above price (for SHORT structural resistance)."""
    ob_type = "bullish" if direction == "LONG" else "bearish"
    candidates = [z for z in zones if z.type == ob_type and not z.mitigated]
    if direction == "LONG":
        candidates = [z for z in candidates if z.top <= price]
        return max(candidates, key=lambda z: z.top) if candidates else None
    candidates = [z for z in candidates if z.bottom >= price]
    return min(candidates, key=lambda z: z.bottom) if candidates else None
