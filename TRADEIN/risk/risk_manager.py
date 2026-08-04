"""RiskManager: structural stop placement, multi-tier take-profit, R:R
validation, illustrative position sizing, and emergency-reversal exit
detection.

Consumes an `ta.mtfa.MTFASetup` plus the confirmed entry price -- never
fabricates a level that isn't backed by real market structure.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config import (
    ACCOUNT_RISK_PCT,
    EMERGENCY_CANDLE_LOOKBACK,
    EMERGENCY_VOLUME_MULT,
    ILLUSTRATIVE_ACCOUNT_SIZE,
    MIN_RISK_REWARD,
    STOP_MAX_ATR_MULT,
    STOP_STRUCTURAL_BUFFER_PCT,
    TP1_MAX_RR,
    TP1_MIN_RR,
)
from ta.candles import is_bearish_engulfing, is_bullish_engulfing
from ta.mtfa import MTFASetup
from ta.order_blocks import nearest_unmitigated
from ta.ta_engine import volume_spike


def _apply_atr_distance_cap(entry_price: float, raw_stop: float, atr_value: float | None, direction: str) -> float:
    """Bound the structural stop distance to `STOP_MAX_ATR_MULT` * ATR. A
    stale/extreme structural level -- e.g. a 52-week high sitting 2-3x above
    current price on a coin that's since fallen hard -- would otherwise blow
    the risk distance out to something unrealistic, dragging TP1/TP2 negative
    for SHORT setups (or absurdly far for LONG). This caps it to a
    volatility-scaled maximum instead of trusting the raw structural level
    unconditionally."""
    if not atr_value or atr_value <= 0:
        return raw_stop
    max_distance = atr_value * STOP_MAX_ATR_MULT
    if direction == "LONG":
        return max(raw_stop, entry_price - max_distance)
    return min(raw_stop, entry_price + max_distance)


def compute_stop(direction: str, setup: MTFASetup, entry_price: float) -> float | None:
    """SL placed strictly beyond the safest (furthest, most protective) of the
    nearest structural support/resistance and order-block edge, or the
    trigger-timeframe 200MA/swing extreme as a last-resort fallback when no
    closer structural level exists -- whichever of those gives the widest
    structural buffer, capped to `STOP_MAX_ATR_MULT` * ATR so a distant
    historical extreme can't blow the risk distance out unrealistically.
    Returns None if no structural level is available at all, or if the
    resulting stop ends up on the wrong side of entry."""
    candidates: list[float] = []

    if direction == "LONG":
        if setup.nearest_support:
            candidates.append(setup.nearest_support)
        ob = nearest_unmitigated(setup.order_blocks, entry_price, "LONG")
        if ob is not None:
            candidates.append(ob.bottom)
        if setup.itf_ema200:
            candidates.append(setup.itf_ema200)
        if not candidates:
            # Last-resort fallback only -- the HTF swing low can sit far below
            # price, so it's never preferred over a closer structural level.
            candidates.append(setup.swing_low)
        raw_stop = min(candidates)
        raw_stop = _apply_atr_distance_cap(entry_price, raw_stop, setup.itf_atr, direction)
        stop = raw_stop * (1 - STOP_STRUCTURAL_BUFFER_PCT)
        return stop if stop < entry_price else None
    else:
        if setup.nearest_resistance:
            candidates.append(setup.nearest_resistance)
        ob = nearest_unmitigated(setup.order_blocks, entry_price, "SHORT")
        if ob is not None:
            candidates.append(ob.top)
        if setup.itf_ema200:
            candidates.append(setup.itf_ema200)
        if not candidates:
            # Last-resort fallback only -- mirrors the LONG branch above. The
            # HTF swing high can sit far above price (e.g. a stale 52-week
            # high), so it must never be an unconditional/mandatory candidate:
            # max() would pick it almost every time and blow out the risk
            # distance (this was the root cause of negative SHORT TPs).
            candidates.append(setup.swing_high)
        raw_stop = max(candidates)
        raw_stop = _apply_atr_distance_cap(entry_price, raw_stop, setup.itf_atr, direction)
        stop = raw_stop * (1 + STOP_STRUCTURAL_BUFFER_PCT)
        return stop if stop > entry_price else None


def compute_targets(direction: str, setup: MTFASetup, entry_price: float, stop: float) -> dict:
    """TP1: nearest structural level, pinned into the `TP1_MIN_RR`-`TP1_MAX_RR`
    band. TP2: the HTF swing extreme (major liquidity zone / key level).

    Both targets are validated to be strictly positive prices before being
    returned -- a SHORT target computed as `entry - risk * multiplier` can go
    negative if `risk` (the stop distance) is disproportionately large
    relative to entry, which `compute_stop`'s ATR distance cap makes rare but
    not provably impossible for all data. Returns `{"target1": None,
    "target2": None}` rather than ever handing back a non-positive price."""
    risk = abs(entry_price - stop)
    if risk <= 0:
        return {"target1": None, "target2": None}

    if direction == "LONG":
        structural_tp1 = setup.nearest_resistance
        rr1 = (structural_tp1 - entry_price) / risk if structural_tp1 else 0.0
        if not structural_tp1 or rr1 < TP1_MIN_RR:
            target1 = entry_price + risk * TP1_MIN_RR
        elif rr1 > TP1_MAX_RR:
            target1 = entry_price + risk * TP1_MAX_RR
        else:
            target1 = structural_tp1

        target2 = setup.swing_high
        if target2 <= target1:
            target2 = entry_price + risk * max(MIN_RISK_REWARD, TP1_MAX_RR * 1.5)
    else:
        structural_tp1 = setup.nearest_support
        rr1 = (entry_price - structural_tp1) / risk if structural_tp1 else 0.0
        if not structural_tp1 or rr1 < TP1_MIN_RR:
            target1 = entry_price - risk * TP1_MIN_RR
        elif rr1 > TP1_MAX_RR:
            target1 = entry_price - risk * TP1_MAX_RR
        else:
            target1 = structural_tp1

        target2 = setup.swing_low
        if target2 >= target1:
            target2 = entry_price - risk * max(MIN_RISK_REWARD, TP1_MAX_RR * 1.5)

    if target1 <= 0 or target2 <= 0:
        return {"target1": None, "target2": None}
    return {"target1": target1, "target2": target2}


def validate_min_rr(
    entry_price: float, stop: float, target1: float, target2: float,
    min_tp1_rr: float = TP1_MIN_RR, min_final_rr: float = MIN_RISK_REWARD,
) -> tuple[bool, float, float]:
    risk = abs(entry_price - stop)
    if risk <= 0:
        return False, 0.0, 0.0
    rr1 = abs(target1 - entry_price) / risk
    rr2 = abs(target2 - entry_price) / risk
    return (rr1 >= min_tp1_rr and rr2 >= min_final_rr), round(rr1, 2), round(rr2, 2)


@dataclass
class PositionSizeEstimate:
    account_size: float
    risk_pct: float
    risk_amount: float
    risk_per_unit: float
    units: float


def position_size_estimate(
    entry_price: float, stop: float,
    account_size: float = ILLUSTRATIVE_ACCOUNT_SIZE, risk_pct: float = ACCOUNT_RISK_PCT,
) -> PositionSizeEstimate:
    """Illustrative-only sizing for the alert text -- this system tracks
    paper-trading signals, not real allocated capital."""
    risk_per_unit = abs(entry_price - stop)
    risk_amount = account_size * risk_pct
    units = risk_amount / risk_per_unit if risk_per_unit > 0 else 0.0
    return PositionSizeEstimate(account_size, risk_pct, risk_amount, risk_per_unit, units)


def detect_emergency_reversal(direction: str, recent_df: pd.DataFrame) -> tuple[bool, str | None]:
    """Extreme opposing-volume surge + strong opposing engulfing candle on
    recent intraday bars -> force-close signal for an open position."""
    if recent_df is None or len(recent_df) < 2:
        return False, None

    spike_ok, ratio = volume_spike(recent_df, mult=EMERGENCY_VOLUME_MULT, lookback=EMERGENCY_CANDLE_LOOKBACK)
    if not spike_ok:
        return False, None

    prev, curr = recent_df.iloc[-2], recent_df.iloc[-1]
    if direction == "LONG" and is_bearish_engulfing(prev, curr):
        return True, f"Bearish engulfing on {ratio}x volume surge"
    if direction == "SHORT" and is_bullish_engulfing(prev, curr):
        return True, f"Bullish engulfing on {ratio}x volume surge"
    return False, None
