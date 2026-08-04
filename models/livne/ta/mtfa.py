"""Top-Down Multi-Timeframe Analysis (MTFA).

Stage A -- HTF (Weekly, with Monthly as bias context) -- maps market
structure (pivot support/resistance, order blocks, a Fibonacci golden zone,
diagonal trendlines through swing pivots, and major weekly EMA/SMA levels)
and proposes a directional bias when price is currently AT one of those HTF
zones. Being at a real zone is the ONLY hard requirement. Weekly trend alignment, RSI
divergence, and weekly volume confluence are NOT rejection gates -- they only
feed downstream confluence *scoring* (`screener._score_candidate`), same as
monthly structure (`monthly_trend`). Volume in particular is deliberately not
a veto here: institutional absorption at real structure often prints on
near-average or even red weekly bars (a "low-volume retest"); directional
confirmation and volume triggers belong to Stage B.

Stage B -- ITF (12H/8H/4H drive the PRIMARY_MA_PERIOD alignment vote; 4H and
1H are zoomed into for the trigger itself) -- requires: a "braking/reversal
event" on either the 4H or 1H frame (`detect_braking_event`, scanning the
last several bars for a pin-bar rejection, a classic candlestick pattern,
volume-climax/absorption, or a Market Structure Shift -- any one qualifies),
above-average volume on the 4H trigger candle, majority MA alignment across
12H/8H/4H, and price either inside the ITF swing's Fibonacci golden zone OR
showing a clear support/resistance reaction at the 0.5/0.618 level.

`analyze_mtfa()` returns None (rather than a NEUTRAL result) whenever any
required confirmation is missing -- callers should treat a None as "no
qualifying setup," not attempt to interpret a partial result.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from config import (
    BRAKING_STRUCTURE_LOOKBACK,
    BRAKING_WINDOW_BARS,
    HTF_INTERVALS,
    HTF_VOLUME_CONFLUENCE_LOOKBACK,
    HTF_VOLUME_CONFLUENCE_MULT,
    HTF_ZONE_PROXIMITY_PCT,
    FIB_REACTION_LOOKBACK_BARS,
    FIB_REACTION_TOLERANCE_PCT,
    ITF_INTERVALS_CRYPTO,
    ITF_MA200_MIN_ALIGNED,
    ITF_RESAMPLE_RULES_STOCKS,
    ITF_VOLUME_LOOKBACK,
    ITF_VOLUME_SPIKE_MULT,
    PRIMARY_MA_PERIOD,
    SECONDARY_MA_PERIOD,
)
from ta.candles import classify_reversal_candle, is_pin_bar_rejection, is_structure_shift, is_volume_climax
from ta.divergence import DivergenceResult, rsi_divergence
from ta.order_blocks import OrderBlock, find_order_blocks, nearest_unmitigated
from ta.ta_engine import (
    add_moving_averages,
    atr,
    determine_trend,
    fibonacci_levels,
    find_swing_high_low,
    nearest_levels,
    pivot_levels,
    pivot_points_indexed,
    volume_spike,
)
from utils.data_fetch import get_binance_klines, get_stock_history, get_stock_intraday_1h, resample_ohlc


def _within(price: float, a: float, b: float) -> bool:
    lo, hi = sorted((a, b))
    return lo <= price <= hi


def htf_volume_ratio(
    df: pd.DataFrame,
    mult: float = HTF_VOLUME_CONFLUENCE_MULT, lookback: int = HTF_VOLUME_CONFLUENCE_LOOKBACK,
) -> float:
    """Stage A confluence-*scoring* input (see `screener._score_candidate`),
    NOT a gate -- how far above its N-week average the most recent weekly
    bar's volume is, at whatever zone `htf_pass` matched. Direction-agnostic
    on purpose: institutional absorption at real structure prints on red
    weekly bars just as often as green ones before the turn, and directional
    confirmation is Stage B's job (braking event + ITF volume spike), not
    Stage A's."""
    _vol_ok, ratio = volume_spike(df, mult=mult, lookback=lookback)
    return ratio


def fib_reaction(
    df: pd.DataFrame, fib: dict, direction: str,
    tolerance_pct: float = FIB_REACTION_TOLERANCE_PCT, lookback: int = FIB_REACTION_LOOKBACK_BARS,
) -> bool:
    """Stage B: price doesn't need to sit contained inside the whole
    [0.5, 0.786] golden-zone box -- a clear support/resistance reaction at
    the 0.5 or 0.618 level also qualifies. "Reaction" here means: within the
    last `lookback` bars, price's range came within `tolerance_pct` of one of
    those levels, and the current close is now back on the favorable side of
    it (above, for a LONG reacting off support; below, for a SHORT reacting
    off resistance)."""
    if len(df) < 2:
        return False
    recent = df.tail(lookback)
    last_close = float(df["Close"].iloc[-1])
    for level in (fib.get("0.5"), fib.get("0.618")):
        if level is None:
            continue
        touched = ((recent["Low"] <= level * (1 + tolerance_pct)) & (recent["High"] >= level * (1 - tolerance_pct))).any()
        if not touched:
            continue
        if direction == "LONG" and last_close > level:
            return True
        if direction == "SHORT" and last_close < level:
            return True
    return False


def detect_braking_event(
    df: pd.DataFrame, direction: str, window_bars: int = BRAKING_WINDOW_BARS,
) -> tuple[bool, str | None]:
    """Stage 7's multi-timeframe zoom-in: scans the last `window_bars` bars
    (most recent first) for ANY of four independent "braking/reversal event"
    signatures confirming `direction` -- a pin-bar rejection, a classic
    candlestick pattern (engulfing/hammer/shooting-star/doji), a volume-
    climax/absorption bar, or a Market Structure Shift. The reversal-candle
    indicator itself is unchanged (still `ta.candles.classify_reversal_candle`);
    this widens *where* it looks (a trailing window, not just the latest bar)
    and *what else* counts alongside it. Shared by Stage 7 entry confirmation
    (`analyze_mtfa`, checked on the 4H and 1H frames) and the backtest
    simulator's dynamic exit (called with the opposite of the held direction)."""
    if len(df) < 2:
        return False, None
    n = min(window_bars, len(df) - 1)
    expected_pattern_dir = "bullish" if direction == "LONG" else "bearish"

    for offset in range(n):
        end = len(df) - offset
        sub = df.iloc[:end]
        if len(sub) < 2:
            continue
        curr = sub.iloc[-1]

        if is_pin_bar_rejection(curr, direction):
            return True, "pin_bar_rejection"

        pattern_info = classify_reversal_candle(sub)
        if pattern_info["is_reversal"] and (
            pattern_info["direction"] == expected_pattern_dir or pattern_info["pattern"] == "doji"
        ):
            return True, pattern_info["pattern"]

        if is_volume_climax(sub):
            return True, "volume_climax"

        if is_structure_shift(sub, direction, lookback=BRAKING_STRUCTURE_LOOKBACK):
            return True, "structure_shift"

    return False, None


def swing_excluding_recent(df: pd.DataFrame, lookback: int = 60, exclude_recent: int = 3) -> tuple[float, float]:
    """Swing high/low for a Fibonacci retracement, computed over the window
    *before* the most recent `exclude_recent` bars. A reversal candle at a
    fresh extreme IS that extreme (fib 0.0/1.0), so measuring the swing over
    a window that includes it would make "price inside the 0.5-0.786
    retracement zone" self-referentially near-impossible right when a
    reversal just triggered. Excluding the last few bars measures the prior
    swing that price is actually retracing into."""
    trimmed = df.iloc[:-exclude_recent] if len(df) > exclude_recent + 5 else df
    hi, lo, _, _ = find_swing_high_low(trimmed, lookback=min(lookback, len(trimmed) - 1))
    return hi, lo


def _fetch_htf(ticker: str, asset_class: str) -> dict[str, pd.DataFrame]:
    if asset_class == "crypto":
        return {
            "weekly": get_binance_klines(ticker, interval=HTF_INTERVALS["crypto"]["weekly"], limit=300),
            "monthly": get_binance_klines(ticker, interval=HTF_INTERVALS["crypto"]["monthly"], limit=200),
        }
    return {
        "weekly": get_stock_history(ticker, period="10y", interval=HTF_INTERVALS["stocks"]["weekly"]),
        "monthly": get_stock_history(ticker, period="max", interval=HTF_INTERVALS["stocks"]["monthly"]),
    }


def _fetch_itf(ticker: str, asset_class: str) -> dict[str, pd.DataFrame]:
    """12H/8H/4H drive the 200MA-alignment vote (Stage B); 1H is added
    specifically for the Stage 7 multi-timeframe braking-event zoom-in
    (`detect_braking_event` is checked on both 4H and 1H)."""
    if asset_class == "crypto":
        frames = {tf: get_binance_klines(ticker, interval=tf, limit=400) for tf in ITF_INTERVALS_CRYPTO}
        frames["1h"] = get_binance_klines(ticker, interval="1h", limit=400)
        return frames
    base = get_stock_intraday_1h(ticker)
    frames = {tf: resample_ohlc(base, tf) for tf in ITF_RESAMPLE_RULES_STOCKS}
    frames["1h"] = base
    return frames


@dataclass
class MTFASetup:
    ticker: str
    asset_class: str
    price: float
    direction_bias: str  # LONG or SHORT (NEUTRAL setups never reach this dataclass)
    htf_trend: str
    htf_zone_type: str                 # "support" / "resistance" / "order_block" / "fib_golden_zone"
    htf_zone: tuple[float, float]
    divergence: DivergenceResult
    order_blocks: list[OrderBlock] = field(default_factory=list)
    nearest_support: float | None = None
    nearest_resistance: float | None = None
    swing_high: float = 0.0
    swing_low: float = 0.0
    fib_levels: dict = field(default_factory=dict)
    itf_trigger_tf: str = "4h"
    itf_pattern: str | None = None
    itf_volume_ratio: float = 0.0
    itf_ma200_aligned_count: int = 0
    itf_fib_levels: dict = field(default_factory=dict)
    itf_ema200: float | None = None
    itf_atr: float | None = None  # ATR on the trigger timeframe; used by RiskManager to bound stop distance
    monthly_trend: str = "UNKNOWN"  # macro context/bias only -- never a hard veto, see htf_pass()
    htf_volume_ratio: float = 0.0  # weekly volume vs its N-week average at the HTF zone -- scoring bonus, not a gate
    thesis: str = ""


def _trendline_zone(
    direction: str, price: float, weekly: pd.DataFrame,
    tolerance_pct: float = HTF_ZONE_PROXIMITY_PCT, lookback: int = 104, window: int = 3,
) -> tuple[float, float] | None:
    """Diagonal trendline zone: fits a straight line through the last two
    qualifying swing pivots (rising swing lows for a LONG support trendline,
    falling swing highs for a SHORT resistance trendline) and checks whether
    price is within tolerance_pct of that line's value projected to the most
    recent bar. A trendline price has already closed decisively through
    since its defining pivot is treated as broken (returns None), not a
    still-active zone -- a stale/invalidated trendline shouldn't count."""
    highs, lows = pivot_points_indexed(weekly, window=window, lookback=lookback)
    pivots = lows if direction == "LONG" else highs
    if len(pivots) < 2:
        return None

    (i1, p1), (i2, p2) = pivots[-2], pivots[-1]
    if i2 == i1:
        return None
    slope = (p2 - p1) / (i2 - i1)
    if direction == "LONG" and slope <= 0:
        return None  # swing lows aren't rising -- not a support trendline
    if direction == "SHORT" and slope >= 0:
        return None  # swing highs aren't falling -- not a resistance trendline

    d = weekly.tail(lookback).reset_index(drop=True)
    last_idx = len(d) - 1
    for i in range(i2 + 1, last_idx + 1):
        level_at_i = p2 + slope * (i - i2)
        close_i = float(d["Close"].iloc[i])
        if direction == "LONG" and close_i < level_at_i * (1 - tolerance_pct):
            return None  # already closed decisively through the line -- broken
        if direction == "SHORT" and close_i > level_at_i * (1 + tolerance_pct):
            return None

    projected = p2 + slope * (last_idx - i2)
    if projected <= 0 or abs(price - projected) / price > tolerance_pct:
        return None
    return (projected * 0.995, projected * 1.005)


def _ma_zone(
    price: float, weekly_with_ma: pd.DataFrame, tolerance_pct: float = HTF_ZONE_PROXIMITY_PCT,
) -> tuple[float, float] | None:
    """Dynamic moving-average zone: price within tolerance_pct of a major
    weekly EMA/SMA -- this project's actual long-term MAs
    (PRIMARY_MA_PERIOD/SECONDARY_MA_PERIOD, see config.py) -- counts as a
    valid zone. Direction-agnostic: a long-term MA can act as either support
    or resistance depending on which side price is currently testing it
    from, same as the pivot/order-block zones above."""
    last = weekly_with_ma.iloc[-1]
    for period in (PRIMARY_MA_PERIOD, SECONDARY_MA_PERIOD):
        for kind in ("EMA", "SMA"):
            val = last.get(f"{kind}{period}")
            if val is None or pd.isna(val):
                continue
            val = float(val)
            if abs(price - val) / price <= tolerance_pct:
                return (val * 0.995, val * 1.005)
    return None


def _direction_zone(
    direction: str, price: float, htf_trend: str, divergence: DivergenceResult,
    near: dict, obs: list[OrderBlock], fib: dict, weekly: pd.DataFrame,
) -> dict | None:
    if direction == "LONG":
        divergence_support = divergence.bullish
        trend_support = htf_trend in ("UPTREND", "MIXED")
    else:
        divergence_support = divergence.bearish
        trend_support = htf_trend in ("DOWNTREND", "MIXED")
    # Trend alignment / RSI divergence are confluence-scoring inputs, not a
    # hard veto -- their absence no longer blocks a setup that's sitting at a
    # real HTF zone. `divergence_support` is still used below to break ties
    # when both directions match a zone, and by the ITF doji allowance.

    zones: list[tuple[str, tuple[float, float]]] = []
    level_key = "nearest_support" if direction == "LONG" else "nearest_resistance"
    level = near.get(level_key)
    if level and abs(price - level) / price <= HTF_ZONE_PROXIMITY_PCT:
        zone_type = "support" if direction == "LONG" else "resistance"
        zones.append((zone_type, (level * 0.995, level * 1.005)))

    ob = nearest_unmitigated(obs, price, direction)
    if ob is not None and (
        _within(price, ob.bottom, ob.top) or abs(price - (ob.top if direction == "LONG" else ob.bottom)) / price <= HTF_ZONE_PROXIMITY_PCT
    ):
        zones.append(("order_block", (ob.bottom, ob.top)))

    if fib:
        lo_f, hi_f = sorted((fib["0.786"], fib["0.5"]))
        if _within(price, lo_f, hi_f):
            zones.append(("fib_golden_zone", (lo_f, hi_f)))

    trendline = _trendline_zone(direction, price, weekly)
    if trendline is not None:
        zone_type = "trendline_support" if direction == "LONG" else "trendline_resistance"
        zones.append((zone_type, trendline))

    ma_zone = _ma_zone(price, weekly)
    if ma_zone is not None:
        zones.append(("ma_zone", ma_zone))

    if not zones:
        return None
    return {"divergence_support": divergence_support, "trend_support": trend_support, "zones": zones}


def _monthly_trend(monthly: pd.DataFrame | None) -> str:
    if monthly is None or monthly.empty or len(monthly) < 60:
        return "UNKNOWN"  # not enough monthly history yet -- treated as non-contradictory, not bullish/bearish
    return determine_trend(add_moving_averages(monthly))


def htf_pass(weekly: pd.DataFrame, monthly: pd.DataFrame | None = None) -> dict | None:
    """The HTF (structure/OB/divergence/zone-match) half of MTFA, factored out
    so callers that already have a (possibly historically-sliced) weekly/
    monthly frame -- e.g. the backtest simulator -- can reuse the exact same
    structural logic without going through the live-data fetchers.

    Weekly drives the zone/structure/divergence match (it has enough bars for
    meaningful pivot/OB detection). Monthly structure is macro context/bias
    only -- it is NOT a hard rejection gate. A monthly uptrend doesn't block
    a legitimate weekly/daily SHORT pullback setup, and vice versa; the
    monthly trend is returned for downstream confluence *scoring*
    (`screener._score_candidate`) to weigh, not for this function to veto on.

    Weekly volume at the matched zone (`htf_volume_ratio`) is likewise not a
    gate here -- it's returned for `screener._score_candidate` to award as a
    confluence bonus. A zone touch on near-average or low volume (a
    low-volume retest) still passes Stage A; above-average volume just scores
    higher than a quiet one."""
    if weekly is None or weekly.empty or len(weekly) < 60:
        return None

    weekly = add_moving_averages(weekly)
    price = float(weekly["Close"].iloc[-1])
    htf_trend = determine_trend(weekly)
    monthly_trend = _monthly_trend(monthly)
    hi, lo, _, _ = find_swing_high_low(weekly, lookback=min(52, len(weekly) - 1))
    fib = fibonacci_levels(hi, lo)
    levels = pivot_levels(weekly, window=3, lookback=min(104, len(weekly)))
    near = nearest_levels(price, levels)
    divergence = rsi_divergence(weekly)
    obs = find_order_blocks(weekly)

    long_signal = _direction_zone("LONG", price, htf_trend, divergence, near, obs, fib, weekly)
    short_signal = _direction_zone("SHORT", price, htf_trend, divergence, near, obs, fib, weekly)
    vol_ratio = htf_volume_ratio(weekly)  # scoring input only, see docstring above -- never vetoes a zone match

    if long_signal and not short_signal:
        direction, signal = "LONG", long_signal
    elif short_signal and not long_signal:
        direction, signal = "SHORT", short_signal
    elif long_signal and short_signal:
        if long_signal["divergence_support"] and not short_signal["divergence_support"]:
            direction, signal = "LONG", long_signal
        elif short_signal["divergence_support"] and not long_signal["divergence_support"]:
            direction, signal = "SHORT", short_signal
        else:
            return None  # both directions equally supported at HTF -- too ambiguous to trade
    else:
        return None

    zone_type, zone = signal["zones"][0]
    return {
        "direction": direction, "signal": signal, "zone_type": zone_type, "zone": zone,
        "price": price, "htf_trend": htf_trend, "monthly_trend": monthly_trend, "divergence": divergence,
        "obs": obs, "near": near, "fib": fib, "hi": hi, "lo": lo, "htf_volume_ratio": vol_ratio,
    }


def analyze_mtfa(ticker: str, asset_class: str) -> MTFASetup | None:
    htf_frames = _fetch_htf(ticker, asset_class)
    htf = htf_pass(htf_frames["weekly"], htf_frames.get("monthly"))
    if htf is None:
        return None

    direction = htf["direction"]
    signal = htf["signal"]
    zone_type, zone = htf["zone_type"], htf["zone"]
    htf_trend, divergence, obs, near, fib, hi, lo = (
        htf["htf_trend"], htf["divergence"], htf["obs"], htf["near"], htf["fib"], htf["hi"], htf["lo"],
    )

    # --- ITF trigger confirmation ---
    primary_col = f"EMA{PRIMARY_MA_PERIOD}"
    itf_frames = _fetch_itf(ticker, asset_class)
    ma_aligned_count = 0
    for tf in ITF_INTERVALS_CRYPTO:  # 12h/8h/4h only -- 1h is excluded from this vote, it's for Stage 7 below
        tf_df = itf_frames.get(tf)
        if tf_df is None or tf_df.empty or len(tf_df) < PRIMARY_MA_PERIOD + 10:
            continue
        tf_df = add_moving_averages(tf_df, periods=(PRIMARY_MA_PERIOD,))
        last = tf_df.iloc[-1]
        if pd.isna(last.get(primary_col)):
            continue
        if direction == "LONG" and float(last["Close"]) >= float(last[primary_col]):
            ma_aligned_count += 1
        elif direction == "SHORT" and float(last["Close"]) <= float(last[primary_col]):
            ma_aligned_count += 1

    trigger_df = itf_frames.get("4h")
    if trigger_df is None or trigger_df.empty or len(trigger_df) < 20:
        return None

    trigger_df_ma = add_moving_averages(trigger_df, periods=(PRIMARY_MA_PERIOD,))
    ema200_val = trigger_df_ma[primary_col].iloc[-1]
    itf_ema200 = float(ema200_val) if not pd.isna(ema200_val) else None
    atr_val = atr(trigger_df).iloc[-1]
    itf_atr = float(atr_val) if not pd.isna(atr_val) else None

    # Stage 7 zoom-in: scan the last few bars on BOTH 4H and 1H for a braking/
    # reversal event -- either timeframe qualifying is enough.
    braking_pattern, braking_tf = None, None
    for tf in ("4h", "1h"):
        tf_df = itf_frames.get(tf)
        if tf_df is None or tf_df.empty:
            continue
        found, pattern = detect_braking_event(tf_df, direction)
        if found:
            braking_pattern, braking_tf = pattern, tf
            break
    if braking_pattern is None:
        return None

    vol_spike_ok, vol_ratio = volume_spike(trigger_df, mult=ITF_VOLUME_SPIKE_MULT, lookback=ITF_VOLUME_LOOKBACK)
    if not vol_spike_ok:
        return None

    if ma_aligned_count < ITF_MA200_MIN_ALIGNED:
        return None

    itf_price = float(trigger_df["Close"].iloc[-1])
    itf_hi, itf_lo = swing_excluding_recent(trigger_df)
    itf_fib = fibonacci_levels(itf_hi, itf_lo)
    contained = _within(itf_price, itf_fib["0.5"], itf_fib["0.786"])
    reacted = fib_reaction(trigger_df, itf_fib, direction)
    if not (contained or reacted):
        return None

    thesis_bits = [
        f"HTF ({htf_trend}) {zone_type} zone {zone[0]:.4g}-{zone[1]:.4g}",
    ]
    if signal["divergence_support"]:
        kind = "Bullish (accumulation)" if direction == "LONG" else "Bearish (distribution)"
        thesis_bits.append(f"{kind} RSI divergence confirmed on weekly")
    if htf["htf_volume_ratio"] >= HTF_VOLUME_CONFLUENCE_MULT:
        thesis_bits.append(f"weekly volume confluence at zone ({htf['htf_volume_ratio']}x avg)")
    else:
        thesis_bits.append(f"low-volume weekly retest at zone ({htf['htf_volume_ratio']}x avg)")
    fib_note = (
        f"inside Fib golden zone ({itf_fib['0.786']:.4g}-{itf_fib['0.5']:.4g})" if contained
        else "reacted off 0.5/0.618 Fib level"
    )
    thesis_bits.append(
        f"{braking_tf.upper()} {braking_pattern} braking/reversal event on {vol_ratio}x volume, "
        f"{ma_aligned_count}/3 ITF timeframes {PRIMARY_MA_PERIOD}MA-aligned, {fib_note}"
    )

    return MTFASetup(
        ticker=ticker, asset_class=asset_class, price=itf_price, direction_bias=direction,
        htf_trend=htf_trend, htf_zone_type=zone_type, htf_zone=zone, divergence=divergence,
        order_blocks=obs, nearest_support=near.get("nearest_support"),
        nearest_resistance=near.get("nearest_resistance"), swing_high=hi, swing_low=lo,
        fib_levels=fib, monthly_trend=htf["monthly_trend"], itf_trigger_tf=braking_tf, itf_pattern=braking_pattern,
        itf_volume_ratio=vol_ratio, itf_ma200_aligned_count=ma_aligned_count,
        itf_fib_levels=itf_fib, itf_ema200=itf_ema200, itf_atr=itf_atr, thesis="; ".join(thesis_bits),
        htf_volume_ratio=htf["htf_volume_ratio"],
    )
