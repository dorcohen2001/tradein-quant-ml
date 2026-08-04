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
    EMA200_ANCHOR_PROXIMITY_PCT,
    HTF_VOLUME_CONFLUENCE_LOOKBACK,
    HTF_VOLUME_CONFLUENCE_MULT,
    HTF_ZONE_PROXIMITY_PCT,
    FIB_REACTION_LOOKBACK_BARS,
    FIB_REACTION_TOLERANCE_PCT,
    LTF_ALIGNMENT_TFS,
    LTF_CONFIRM_TFS,
    LTF_MA_MIN_ALIGNED,
    LTF_TRIGGER_TF,
    MIN_ZONE_HISTORY_BARS,
    PRIMARY_MA_PERIOD,
    SECONDARY_MA_PERIOD,
    TF_INTERVALS,
    ZONE_DISCOVERY_TFS,
    ZONE_REFINE_PROXIMITY_PCT,
    ZONE_REFINE_TFS,
)
from ta.candles import (
    classify_reversal_candle,
    detect_reversal_formation,
    directional_volume_confirmation,
    is_pin_bar_rejection,
    is_structure_shift,
    is_volume_climax,
)
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
from utils.data_fetch import (
    get_binance_klines,
    get_stock_history,
    get_stock_intraday,
    get_stock_intraday_1h,
    resample_ohlc,
)


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


_CRYPTO_LIMITS = {"weekly": 200, "daily": 400, "12h": 400, "4h": 500, "1h": 600, "15m": 600, "5m": 600}


def _fetch_frames(ticker: str, asset_class: str) -> dict[str, pd.DataFrame]:
    """Every timeframe the intraday ladder needs, in one pass:

        weekly / daily / 12h  -- top-down zone DISCOVERY
        4h / 3h / 1h          -- precision REFINEMENT of those zones
        1h / 15m / 5m         -- EXECUTION (trigger + confirmation + MA vote)

    `1h` deliberately serves double duty as both the finest refinement frame
    and the coarsest execution frame -- it is fetched once and shared.

    Neither venue offers every interval natively, so `TF_INTERVALS` marks the
    ones that must be resampled from 1h bars (3h for crypto; 12h/4h/3h for
    stocks, where yfinance has no sub-daily bar above 90m)."""
    intervals = TF_INTERVALS[asset_class]
    frames: dict[str, pd.DataFrame] = {}

    if asset_class == "crypto":
        for tf, interval in intervals.items():
            if interval is not None:
                frames[tf] = get_binance_klines(ticker, interval=interval, limit=_CRYPTO_LIMITS.get(tf, 400))
    else:
        frames["weekly"] = get_stock_history(ticker, period="2y", interval=intervals["weekly"])
        frames["daily"] = get_stock_history(ticker, period="1y", interval=intervals["daily"])
        frames["1h"] = get_stock_intraday_1h(ticker)
        frames["15m"] = get_stock_intraday(ticker, interval="15m")
        frames["5m"] = get_stock_intraday(ticker, interval="5m")

    # Fill in whatever the venue could not serve natively by resampling 1h.
    base_1h = frames.get("1h")
    if base_1h is not None and not base_1h.empty:
        for tf, interval in intervals.items():
            if interval is None and tf not in frames:
                frames[tf] = resample_ohlc(base_1h, tf)

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
    monthly_trend: str = "UNKNOWN"  # highest-timeframe (weekly) bias -- context only, never a hard veto
    htf_volume_ratio: float = 0.0  # zone-timeframe volume vs its N-bar average -- scoring bonus, not a gate
    thesis: str = ""
    # --- intraday additions ---
    zone_tf: str = ""                     # which timeframe supplied the zone actually used
    zone_discovery_tfs: list[str] = field(default_factory=list)
    zone_refined_tfs: list[str] = field(default_factory=list)
    reversal_conviction: float = 0.0      # 0-1, per config.REVERSAL_CONVICTION
    ema200_anchor: float | None = None    # EMA200 level price is reacting at, if any
    ema200_anchor_tf: str | None = None
    volume_detail: dict = field(default_factory=dict)  # ratio / direction_ok / exhaustion
    confirm_agreement: int = 0            # how many LTF_CONFIRM_TFS agree with the direction
    # Swing extremes measured on the TRIGGER timeframe. TP2 must be derived
    # from these, not from the discovery-timeframe swing: a weekly/12h swing
    # extreme is months of travel away and produces an intraday target that
    # can never be reached (a live SHORT priced TP2 at 14R before this).
    itf_swing_high: float = 0.0
    itf_swing_low: float = 0.0


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


def _ema200_anchor(price: float, df: pd.DataFrame, tolerance_pct: float = EMA200_ANCHOR_PROXIMITY_PCT) -> float | None:
    """The EMA200 level on `df` if price is currently pressed against it.

    Treated as a high-certainty structural barrier in its own right: price
    seldom slices the 200 EMA cleanly on first contact, so a reaction there is
    among the most reliable intraday reversal locations. Returns the level so
    callers can both score the confluence and use it for stop placement."""
    if df is None or df.empty or len(df) < PRIMARY_MA_PERIOD // 2:
        return None
    with_ma = add_moving_averages(df, periods=(PRIMARY_MA_PERIOD,))
    val = with_ma[f"EMA{PRIMARY_MA_PERIOD}"].iloc[-1]
    if pd.isna(val):
        return None
    val = float(val)
    if val > 0 and abs(price - val) / price <= tolerance_pct:
        return val
    return None


def _zone_frame_ok(df: pd.DataFrame | None) -> bool:
    return df is not None and not df.empty and len(df) >= MIN_ZONE_HISTORY_BARS


def map_zones_top_down(
    direction: str, price: float, frames: dict[str, pd.DataFrame],
) -> dict | None:
    """Top-down zone map: DISCOVER on weekly -> daily -> 12h, then precision-
    REFINE on 4h -> 3h -> 1h.

    Discovery answers "is price at a level that matters on a higher
    timeframe", using the loose HTF_ZONE_PROXIMITY_PCT band. Refinement
    answers "and is it precisely there right now", using the much tighter
    ZONE_REFINE_PROXIMITY_PCT -- which is what makes an intraday stop
    placeable at all. A discovery hit is mandatory; refinement hits raise
    conviction and supply the tighter zone edges used downstream for stops.

    Returns None when no discovery timeframe places price at a zone.
    """
    discovered: list[dict] = []
    refined: list[dict] = []

    for tf in ZONE_DISCOVERY_TFS:
        df = frames.get(tf)
        if not _zone_frame_ok(df):
            continue
        with_ma = add_moving_averages(df)
        hi, lo, _, _ = find_swing_high_low(df, lookback=min(52, len(df) - 1))
        fib = fibonacci_levels(hi, lo)
        levels = pivot_levels(df, window=3, lookback=min(104, len(df)))
        near = nearest_levels(price, levels)
        obs = find_order_blocks(df)
        signal = _direction_zone(direction, price, "MIXED", rsi_divergence(df), near, obs, fib, with_ma)
        if signal:
            for zone_type, zone in signal["zones"]:
                discovered.append({"tf": tf, "type": zone_type, "zone": zone,
                                    "support": near.get("nearest_support"),
                                    "resistance": near.get("nearest_resistance"),
                                    "obs": obs, "hi": hi, "lo": lo, "fib": fib})

    if not discovered:
        return None

    ema_anchor_tf = None
    ema_anchor_level = None
    for tf in ZONE_REFINE_TFS:
        df = frames.get(tf)
        if not _zone_frame_ok(df):
            continue
        levels = pivot_levels(df, window=3, lookback=min(120, len(df)))
        near = nearest_levels(price, levels)
        key = "nearest_support" if direction == "LONG" else "nearest_resistance"
        level = near.get(key)
        if level and abs(price - level) / price <= ZONE_REFINE_PROXIMITY_PCT:
            zone_type = "support" if direction == "LONG" else "resistance"
            refined.append({"tf": tf, "type": zone_type,
                             "zone": (level * (1 - ZONE_REFINE_PROXIMITY_PCT), level * (1 + ZONE_REFINE_PROXIMITY_PCT)),
                             "support": near.get("nearest_support"),
                             "resistance": near.get("nearest_resistance")})
        if ema_anchor_level is None:
            anchor = _ema200_anchor(price, df)
            if anchor is not None:
                ema_anchor_level, ema_anchor_tf = anchor, tf

    primary = refined[0] if refined else discovered[0]
    # Structural levels prefer the refined (tighter) read when one exists,
    # falling back to the discovery frame's levels otherwise.
    support = primary.get("support") or discovered[0].get("support")
    resistance = primary.get("resistance") or discovered[0].get("resistance")

    return {
        "zone_type": primary["type"],
        "zone": primary["zone"],
        "zone_tf": primary["tf"],
        "discovered": discovered,
        "refined": refined,
        "nearest_support": support,
        "nearest_resistance": resistance,
        "order_blocks": discovered[0]["obs"],
        "swing_high": discovered[0]["hi"],
        "swing_low": discovered[0]["lo"],
        "fib": discovered[0]["fib"],
        "ema200_anchor": ema_anchor_level,
        "ema200_anchor_tf": ema_anchor_tf,
    }


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


def _ma_alignment_vote(direction: str, frames: dict[str, pd.DataFrame]) -> int:
    """How many of LTF_ALIGNMENT_TFS (1h/15m/5m) closed on the correct side of
    the 200 EMA. A timeframe with too little history to compute the MA simply
    does not vote rather than voting against."""
    primary_col = f"EMA{PRIMARY_MA_PERIOD}"
    aligned = 0
    for tf in LTF_ALIGNMENT_TFS:
        df = frames.get(tf)
        if df is None or df.empty or len(df) < PRIMARY_MA_PERIOD + 10:
            continue
        with_ma = add_moving_averages(df, periods=(PRIMARY_MA_PERIOD,))
        last = with_ma.iloc[-1]
        if pd.isna(last.get(primary_col)):
            continue
        if direction == "LONG" and float(last["Close"]) >= float(last[primary_col]):
            aligned += 1
        elif direction == "SHORT" and float(last["Close"]) <= float(last[primary_col]):
            aligned += 1
    return aligned


def _confirm_agreement(direction: str, frames: dict[str, pd.DataFrame]) -> int:
    """How many confirmation timeframes (5m, 1h) currently lean `direction`,
    by close vs. their own short EMA. Scoring input only -- a confirmation
    frame disagreeing does not veto a valid trigger."""
    agree = 0
    for tf in LTF_CONFIRM_TFS:
        df = frames.get(tf)
        if df is None or df.empty or len(df) < 25:
            continue
        ema = df["Close"].ewm(span=20, adjust=False).mean().iloc[-1]
        close = float(df["Close"].iloc[-1])
        if pd.isna(ema):
            continue
        if (direction == "LONG" and close >= float(ema)) or (direction == "SHORT" and close <= float(ema)):
            agree += 1
    return agree


def analyze_mtfa(ticker: str, asset_class: str) -> MTFASetup | None:
    """Intraday MTFA: daily/weekly context -> top-down zone map -> 15m trigger.

    Hard gates, in short-circuit order:
      1. a top-down zone map exists for exactly one direction
      2. an explicit reversal FORMATION on the 15m trigger
      3. MANDATORY directional volume confirmation on that formation candle
      4. >= LTF_MA_MIN_ALIGNED of 1h/15m/5m aligned with the 200 EMA
      5. Fib containment or a 0.5/0.618 reaction on the trigger frame
    """
    frames = _fetch_frames(ticker, asset_class)

    trigger_df = frames.get(LTF_TRIGGER_TF)
    if trigger_df is None or trigger_df.empty or len(trigger_df) < 30:
        return None
    price = float(trigger_df["Close"].iloc[-1])

    # --- Context / bias (daily drives regime, weekly is the higher bias) ---
    daily = frames.get("daily")
    weekly = frames.get("weekly")
    htf_trend = determine_trend(add_moving_averages(daily)) if _zone_frame_ok(daily) else "UNKNOWN"
    higher_trend = determine_trend(add_moving_averages(weekly)) if _zone_frame_ok(weekly) else "UNKNOWN"
    zone_volume_ratio = htf_volume_ratio(daily) if _zone_frame_ok(daily) else 0.0
    divergence = rsi_divergence(daily) if _zone_frame_ok(daily) else DivergenceResult()

    # --- 1. Top-down zone map, evaluated for both directions ---
    long_map = map_zones_top_down("LONG", price, frames)
    short_map = map_zones_top_down("SHORT", price, frames)
    if long_map and short_map:
        # Price sits at both a support and a resistance read. Break the tie on
        # the daily context trend; if that is neutral too, the location is
        # genuinely ambiguous and is not worth trading intraday.
        if htf_trend == "UPTREND":
            direction, zone_map = "LONG", long_map
        elif htf_trend == "DOWNTREND":
            direction, zone_map = "SHORT", short_map
        else:
            return None
    elif long_map:
        direction, zone_map = "LONG", long_map
    elif short_map:
        direction, zone_map = "SHORT", short_map
    else:
        return None

    # --- 2. Explicit reversal formation on the 15m trigger (HARD) ---
    found, pattern, conviction, bar_offset = detect_reversal_formation(trigger_df, direction)
    if not found:
        return None

    # --- 3. MANDATORY directional volume confirmation on the formation bar ---
    formation_df = trigger_df.iloc[: len(trigger_df) - bar_offset]
    volume_ok, volume_detail = directional_volume_confirmation(formation_df, direction)
    if not volume_ok:
        return None

    # --- 4. 200 EMA alignment across the execution ladder (HARD) ---
    ma_aligned_count = _ma_alignment_vote(direction, frames)
    if ma_aligned_count < LTF_MA_MIN_ALIGNED:
        return None

    # --- 5. Fib confluence on the trigger frame (HARD) ---
    itf_hi, itf_lo = swing_excluding_recent(trigger_df)
    itf_fib = fibonacci_levels(itf_hi, itf_lo)
    contained = _within(price, itf_fib["0.5"], itf_fib["0.786"])
    reacted = fib_reaction(trigger_df, itf_fib, direction)
    if not (contained or reacted):
        return None

    # --- Trigger-frame derived values for RiskManager ---
    trigger_ma = add_moving_averages(trigger_df, periods=(PRIMARY_MA_PERIOD,))
    ema200_val = trigger_ma[f"EMA{PRIMARY_MA_PERIOD}"].iloc[-1]
    itf_ema200 = float(ema200_val) if not pd.isna(ema200_val) else None
    atr_val = atr(trigger_df).iloc[-1]
    itf_atr = float(atr_val) if not pd.isna(atr_val) else None

    confirm_agreement = _confirm_agreement(direction, frames)
    discovery_tfs = sorted({d["tf"] for d in zone_map["discovered"]})
    refined_tfs = sorted({r["tf"] for r in zone_map["refined"]})
    zone = zone_map["zone"]

    # --- Thesis ---
    thesis_bits = [
        f"Daily context {htf_trend} (weekly {higher_trend}); "
        f"{zone_map['zone_type']} zone {zone[0]:.4g}-{zone[1]:.4g} discovered on "
        f"{'/'.join(discovery_tfs)}" + (f", refined on {'/'.join(refined_tfs)}" if refined_tfs else "")
    ]
    if zone_map["ema200_anchor"] is not None:
        thesis_bits.append(
            f"price reacting at the {zone_map['ema200_anchor_tf']} {PRIMARY_MA_PERIOD} EMA "
            f"({zone_map['ema200_anchor']:.4g}) -- high-certainty structural barrier"
        )
    if (direction == "LONG" and divergence.bullish) or (direction == "SHORT" and divergence.bearish):
        kind = "Bullish (accumulation)" if direction == "LONG" else "Bearish (distribution)"
        thesis_bits.append(f"{kind} RSI divergence on daily")
    vol_word = "buyer volume surge" if direction == "LONG" else "seller volume expansion"
    vol_note = f"{vol_word} {volume_detail['ratio']}x avg on the {LTF_TRIGGER_TF} {pattern}"
    if volume_detail.get("exhaustion"):
        exhausted = "buyer" if direction == "SHORT" else "seller"
        vol_note += f", preceded by {exhausted} exhaustion ({volume_detail['prior_push_ratio']}x avg into the extreme)"
    thesis_bits.append(vol_note)
    fib_note = (
        f"inside Fib golden zone ({itf_fib['0.786']:.4g}-{itf_fib['0.5']:.4g})" if contained
        else "reacted off 0.5/0.618 Fib level"
    )
    thesis_bits.append(
        f"{ma_aligned_count}/{len(LTF_ALIGNMENT_TFS)} execution timeframes {PRIMARY_MA_PERIOD}MA-aligned, "
        f"{confirm_agreement}/{len(LTF_CONFIRM_TFS)} confirmation frames agree, {fib_note}"
    )

    return MTFASetup(
        ticker=ticker, asset_class=asset_class, price=price, direction_bias=direction,
        htf_trend=htf_trend, htf_zone_type=zone_map["zone_type"], htf_zone=zone, divergence=divergence,
        order_blocks=zone_map["order_blocks"], nearest_support=zone_map["nearest_support"],
        nearest_resistance=zone_map["nearest_resistance"],
        swing_high=zone_map["swing_high"], swing_low=zone_map["swing_low"],
        fib_levels=zone_map["fib"], monthly_trend=higher_trend,
        itf_trigger_tf=LTF_TRIGGER_TF, itf_pattern=pattern,
        itf_volume_ratio=volume_detail["ratio"], itf_ma200_aligned_count=ma_aligned_count,
        itf_fib_levels=itf_fib, itf_ema200=itf_ema200, itf_atr=itf_atr,
        itf_swing_high=itf_hi, itf_swing_low=itf_lo,
        thesis="; ".join(thesis_bits), htf_volume_ratio=zone_volume_ratio,
        zone_tf=zone_map["zone_tf"], zone_discovery_tfs=discovery_tfs, zone_refined_tfs=refined_tfs,
        reversal_conviction=conviction, ema200_anchor=zone_map["ema200_anchor"],
        ema200_anchor_tf=zone_map["ema200_anchor_tf"], volume_detail=volume_detail,
        confirm_agreement=confirm_agreement,
    )
