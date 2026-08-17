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
    emergency_volume_mult,
    risk_params,
    ILLUSTRATIVE_ACCOUNT_SIZE,
    MIN_RISK_REWARD,
    STOP_ATR_BUFFER_MULT,
    STOP_MAX_ATR_MULT,
    STOP_MIN_TICK_PCT,
    TP1_MAX_RR,
    TP1_MIN_RR,
    TP2_MAX_RR,
    TP2_TARGET_RR,
    TP_MIN_SEPARATION_RR,
)
from ta.candles import is_bearish_engulfing, is_bullish_engulfing
from ta.mtfa import MTFASetup
from ta.order_blocks import nearest_unmitigated
from ta.ta_engine import volume_spike


def liquidity_sweep_buffer(price: float, atr_value: float | None,
                           asset_class: str | None = None) -> float:
    """Dynamic cushion placed BEYOND the structural stop level, sized to the
    noise actually present on the trigger timeframe:

        buffer = max(STOP_ATR_BUFFER_MULT * ATR(15m), price * STOP_MIN_TICK_PCT)

    Stops resting exactly on an obvious swing low / order-block edge are the
    liquidity that gets swept: price pokes through, fills the resting stops,
    and reverses. A cushion proportional to current ATR keeps the stop just
    outside that noise band without inflating risk when the market is quiet.

    This replaces a flat 0.5% cushion, which was wildly oversized once the
    model moved intraday -- on a $200 stock it sat a full dollar beyond
    structure while a typical 15m ATR is ~$0.15, so the buffer alone exceeded
    any realistic intraday target and forced every setup below the R:R floor.

    The tick floor keeps the buffer meaningful when ATR is missing or the
    instrument is unusually quiet.
    """
    tick_floor = abs(price) * STOP_MIN_TICK_PCT
    if not atr_value or atr_value <= 0:
        return tick_floor
    # HORIZON: the multiplier is per asset class. Crypto wicks far further
    # inside one 15m bar than equities do, so an equity-sized cushion sits
    # inside crypto's ordinary noise band and gets swept by moves that were
    # never a reversal. `asset_class=None` keeps the module-level default, so
    # any caller not yet passing one behaves exactly as before.
    mult = (risk_params(asset_class)["stop_atr_buffer_mult"]
            if asset_class else STOP_ATR_BUFFER_MULT)
    return max(atr_value * mult, tick_floor)


def _apply_atr_distance_cap(entry_price: float, raw_stop: float, atr_value: float | None,
                            direction: str, asset_class: str | None = None) -> float:
    """Bound the structural stop distance to `STOP_MAX_ATR_MULT` * ATR. A
    stale/extreme structural level -- e.g. a 52-week high sitting 2-3x above
    current price on a coin that's since fallen hard -- would otherwise blow
    the risk distance out to something unrealistic, dragging TP1/TP2 negative
    for SHORT setups (or absurdly far for LONG). This caps it to a
    volatility-scaled maximum instead of trusting the raw structural level
    unconditionally."""
    if not atr_value or atr_value <= 0:
        return raw_stop
    # HORIZON: per asset class. The cap has to widen alongside the buffer, or
    # it silently undoes the wider stop it exists to permit.
    cap_mult = (risk_params(asset_class)["stop_max_atr_mult"]
                if asset_class else STOP_MAX_ATR_MULT)
    max_distance = atr_value * cap_mult
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
        raw_stop = _apply_atr_distance_cap(entry_price, raw_stop, setup.itf_atr, direction,
                                           setup.asset_class)
        stop = raw_stop - liquidity_sweep_buffer(entry_price, setup.itf_atr, setup.asset_class)
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
        raw_stop = _apply_atr_distance_cap(entry_price, raw_stop, setup.itf_atr, direction,
                                           setup.asset_class)
        stop = raw_stop + liquidity_sweep_buffer(entry_price, setup.itf_atr, setup.asset_class)
        return stop if stop > entry_price else None


def _intraday_target2(direction: str, setup: MTFASetup, entry_price: float, risk: float, target1: float) -> float:
    """TP2 from the TRIGGER timeframe's swing extreme, clamped into
    [TP1_MAX_RR, TP2_MAX_RR].

    The swing-timeframe extreme (`setup.swing_high`/`swing_low`) is deliberately
    NOT used here any more. Those come from the zone-DISCOVERY frames
    (weekly/daily/12h), so on an intraday trade they sit an entire multi-month
    leg away -- live examples priced TP2 at 7.5R on ETH and 14.3R on BTC, which
    a position meant to live minutes to hours can never reach. The trade would
    have run to the breakeven stop every time and TP2 would be decorative.

    The 15m swing is the reachable structural objective; the clamp then keeps
    TP2 sane when even that is unhelpfully close or far.

    TP2 is additionally guaranteed to sit at least `TP_MIN_SEPARATION_RR`
    beyond TP1. Previously it was floored at `max(MIN_RISK_REWARD,
    TP2_TARGET_RR)` = 2.0R, which collided exactly with a TP1 that had itself
    been clamped to TP1_MAX_RR = 2.0R -- producing two identical target prices
    (seen live on LRCX and SMH). The floor is now relative to wherever TP1
    actually landed, so the two tiers can never coincide.
    """
    itf_extreme = setup.itf_swing_high if direction == "LONG" else setup.itf_swing_low

    # Minimum acceptable TP2, in R -- always strictly beyond TP1.
    tp1_rr = abs(target1 - entry_price) / risk
    min_tp2_rr = max(tp1_rr + TP_MIN_SEPARATION_RR, TP2_TARGET_RR, MIN_RISK_REWARD)
    # The ceiling must never be pulled back below that floor, or the clamp
    # would silently re-collapse TP2 onto TP1 for a very wide TP1.
    max_tp2_rr = max(TP2_MAX_RR, min_tp2_rr)

    if direction == "LONG":
        floor_price = entry_price + risk * min_tp2_rr
        ceil_price = entry_price + risk * max_tp2_rr
        candidate = itf_extreme if itf_extreme and itf_extreme > floor_price else floor_price
        return min(candidate, ceil_price)

    floor_price = entry_price - risk * min_tp2_rr
    ceil_price = entry_price - risk * max_tp2_rr
    candidate = itf_extreme if itf_extreme and 0 < itf_extreme < floor_price else floor_price
    return max(candidate, ceil_price)


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

    # HORIZON: crypto's TP1 band is pulled in (1.0-2.0R -> 0.9-1.6R) because
    # its stop is now wider. Left unchanged, a wider stop with the same target
    # band pushes R:R under the floor and starves the funnel -- so the target
    # moves to where momentum can still reach it before it fades.
    _profile = risk_params(setup.asset_class)
    tp1_min_rr = _profile["tp1_min_rr"]
    tp1_max_rr = _profile["tp1_max_rr"]

    if direction == "LONG":
        structural_tp1 = setup.nearest_resistance
        rr1 = (structural_tp1 - entry_price) / risk if structural_tp1 else 0.0
        if not structural_tp1 or rr1 < tp1_min_rr:
            target1 = entry_price + risk * tp1_min_rr
        elif rr1 > tp1_max_rr:
            target1 = entry_price + risk * tp1_max_rr
        else:
            target1 = structural_tp1

        target2 = _intraday_target2("LONG", setup, entry_price, risk, target1)
    else:
        structural_tp1 = setup.nearest_support
        rr1 = (entry_price - structural_tp1) / risk if structural_tp1 else 0.0
        if not structural_tp1 or rr1 < tp1_min_rr:
            target1 = entry_price - risk * tp1_min_rr
        elif rr1 > tp1_max_rr:
            target1 = entry_price - risk * tp1_max_rr
        else:
            target1 = structural_tp1

        target2 = _intraday_target2("SHORT", setup, entry_price, risk, target1)

    # Invariant: the two exit tiers must be genuinely distinct and correctly
    # ordered. `_intraday_target2` already guarantees this; the check is
    # repeated here as a last line of defence because a TP1 == TP2 alert is
    # silently wrong rather than loudly broken -- validate_min_rr passes it,
    # the message renders fine, and the tracker books a TP1 partial only to
    # close the remainder at the identical price.
    ordered = target2 > target1 if direction == "LONG" else target2 < target1
    if not ordered:
        step = risk * TP_MIN_SEPARATION_RR
        target2 = target1 + step if direction == "LONG" else target1 - step

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


def detect_emergency_reversal(direction: str, recent_df: pd.DataFrame,
                              asset_class: str | None = None) -> tuple[bool, str | None]:
    """Extreme opposing-volume surge + strong opposing engulfing candle on
    recent intraday bars -> force-close signal for an open position.

    HORIZON: the volume multiple is per asset class. It is a multiple of the
    RECENT AVERAGE, and the two venues have different baselines -- crypto is
    spiky around the clock, so a shared 3.0x is reached inside healthy trends
    and cuts winners early, while equities concentrate volume into the open and
    close where the same ratio is rarer and more meaningful.
    """
    if recent_df is None or len(recent_df) < 2:
        return False, None

    mult = emergency_volume_mult(asset_class) if asset_class else EMERGENCY_VOLUME_MULT
    spike_ok, ratio = volume_spike(recent_df, mult=mult, lookback=EMERGENCY_CANDLE_LOOKBACK)
    if not spike_ok:
        return False, None

    prev, curr = recent_df.iloc[-2], recent_df.iloc[-1]
    if direction == "LONG" and is_bearish_engulfing(prev, curr):
        return True, f"Bearish engulfing on {ratio}x volume surge"
    if direction == "SHORT" and is_bullish_engulfing(prev, curr):
        return True, f"Bullish engulfing on {ratio}x volume surge"
    return False, None
