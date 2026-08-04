"""Candlestick reversal/exhaustion pattern detection for ITF trigger confirmation.

Pure functions over OHLC rows (pandas Series with Open/High/Low/Close) — no
indicator state, so they compose cleanly with the volume/MA/Fib checks in
`ta.mtfa`.
"""
from __future__ import annotations

import pandas as pd

from config import (
    BRAKING_PIN_BAR_WICK_RATIO,
    BRAKING_SMALL_BODY_RATIO,
    BRAKING_VOLUME_CLIMAX_MULT,
    LTF_EXHAUSTION_LOOKBACK,
    LTF_EXHAUSTION_MAX_RATIO,
    LTF_VOLUME_LOOKBACK,
    LTF_VOLUME_SURGE_MULT,
    REVERSAL_CONVICTION,
    REVERSAL_PIN_BAR_WICK_RATIO,
    REVERSAL_SCAN_BARS,
)
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


def _engulfs_range(prev: pd.Series, curr: pd.Series, direction: str) -> bool:
    """Stronger than a textbook engulfing: `curr` must close beyond the whole
    of `prev`'s range, in `direction`, on a body of its own colour. Used for
    the doji-then-engulfing formation, where the doji has no meaningful body
    for a conventional engulfing test to reference."""
    if direction == "LONG":
        return float(curr["Close"]) > float(prev["High"]) and float(curr["Close"]) > float(curr["Open"])
    return float(curr["Close"]) < float(prev["Low"]) and float(curr["Close"]) < float(curr["Open"])


def detect_reversal_formation(
    df: pd.DataFrame, direction: str,
    scan_bars: int = REVERSAL_SCAN_BARS, wick_ratio: float = REVERSAL_PIN_BAR_WICK_RATIO,
) -> tuple[bool, str | None, float, int]:
    """Explicit, direction-matching candlestick reversal formation on the
    trigger timeframe. Returns `(found, pattern, conviction, bar_offset)`,
    where `bar_offset` is how many bars back from the latest the formation
    completed (0 = the current bar).

    This REPLACES `detect_braking_event` as the entry trigger. The difference
    is deliberate: the old detector accepted a volume climax or a market-
    structure shift on their own, neither of which is a reversal *formation* --
    both routinely fire mid-trend and, on a 15m chart, fire constantly. Here
    an actual formation is required, and climax/MSS are demoted to confluence
    flags scored elsewhere.

    Formations, highest conviction first (see REVERSAL_CONVICTION):
      doji_then_engulfing -- indecision immediately resolved in `direction`
      engulfing           -- classic two-bar reversal
      hammer / shooting_star -- single-bar exhaustion with a dominant wick
      pin_bar_rejection   -- wick >= `wick_ratio` of range (stricter than the
                             braking detector's 0.5, so a marginal wick no
                             longer counts as high conviction)
    """
    if len(df) < 3:
        return False, None, 0.0, 0

    want_bullish = direction == "LONG"
    best: tuple[str, float, int] | None = None

    for offset in range(min(scan_bars, len(df) - 2)):
        end = len(df) - offset
        sub = df.iloc[:end]
        prev, curr = sub.iloc[-2], sub.iloc[-1]

        matches: list[str] = []
        if is_doji(prev) and _engulfs_range(prev, curr, direction):
            matches.append("doji_then_engulfing")
        if (is_bullish_engulfing(prev, curr) if want_bullish else is_bearish_engulfing(prev, curr)):
            matches.append("engulfing")
        if want_bullish and is_hammer(curr):
            matches.append("hammer")
        if not want_bullish and is_shooting_star(curr):
            matches.append("shooting_star")
        if is_pin_bar_rejection(curr, direction, wick_ratio=wick_ratio):
            matches.append("pin_bar_rejection")

        for pattern in matches:
            conviction = REVERSAL_CONVICTION.get(pattern, 0.5)
            # Prefer the strongest formation; on a tie prefer the most recent
            # bar, since a fresher signal is the one price is reacting to now.
            if best is None or conviction > best[1]:
                best = (pattern, conviction, offset)

    if best is None:
        return False, None, 0.0, 0
    return True, best[0], best[1], best[2]


def directional_volume_confirmation(
    df: pd.DataFrame, direction: str,
    surge_mult: float = LTF_VOLUME_SURGE_MULT, lookback: int = LTF_VOLUME_LOOKBACK,
    exhaustion_lookback: int = LTF_EXHAUSTION_LOOKBACK,
    exhaustion_max_ratio: float = LTF_EXHAUSTION_MAX_RATIO,
) -> tuple[bool, dict]:
    """MANDATORY directional volume proof on the reversal candle (the last bar
    of `df`, so callers should slice to end at the formation bar).

    A bare volume spike is not enough and never was -- an expansion bar
    printing AGAINST the proposed direction is precisely what a failed
    reversal looks like. So two things must both hold:

      1. the candle itself leans in `direction` (by body, or by a dominant
         rejection wick -- a hammer closing marginally red is still buyers
         taking control of the range), and
      2. its volume is at least `surge_mult` x its own N-bar average.

    For LONG that is a buyer volume surge; for SHORT, seller volume expansion
    on the rejection.

    Buyer/seller EXHAUSTION into the extreme -- volume fading across the prior
    push -- is computed and returned but is not required. Demanding it too
    would reject sharp V-reversals that turn without a slow bleed first. It
    feeds the conviction score and the alert thesis instead.
    """
    detail = {"ratio": 0.0, "direction_ok": False, "exhaustion": False, "prior_push_ratio": None}
    if len(df) < 2:
        return False, detail

    curr = df.iloc[-1]
    avg_vol = df["Volume"].tail(lookback + 1).iloc[:-1].mean()
    if not avg_vol or pd.isna(avg_vol):
        return False, detail

    ratio = float(curr["Volume"]) / float(avg_vol)
    detail["ratio"] = round(ratio, 2)

    body_bullish = float(curr["Close"]) > float(curr["Open"])
    rng = _range(curr)
    lower_dominant = rng > 0 and (_lower_wick(curr) / rng) >= 0.5
    upper_dominant = rng > 0 and (_upper_wick(curr) / rng) >= 0.5
    direction_ok = (body_bullish or lower_dominant) if direction == "LONG" else ((not body_bullish) or upper_dominant)
    detail["direction_ok"] = bool(direction_ok)

    prior = df.iloc[-(exhaustion_lookback + 1):-1]
    if len(prior) >= 2:
        prior_ratio = float(prior["Volume"].mean()) / float(avg_vol)
        detail["prior_push_ratio"] = round(prior_ratio, 2)
        detail["exhaustion"] = prior_ratio <= exhaustion_max_ratio

    return bool(direction_ok and ratio >= surge_mult), detail


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
