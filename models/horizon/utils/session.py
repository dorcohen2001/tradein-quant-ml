"""Session-aware entry gating for equities.

WHY THIS EXISTS
---------------
The midday liquidity trough is the worst window this strategy can enter in,
and the reason is mechanical rather than superstitious. The trigger is a 15m
candle whose volume must clear a multiple of its own 20-bar average. Around
midday both terms collapse: the candle is thin AND the average it is measured
against is depressed, so the ratio is noisy in exactly the range where the gate
decides. A "surge" printed at 12:15 ET is a fraction of the same ratio at the
open, and the setups that squeeze through are the low-momentum fakeouts this
model was recalibrated to avoid.

Livne's own live funnel, sampled 11:17-12:07 ET, had 52 of 62 trigger candles
come in BELOW their own 20-bar average.

WHAT IT DOES NOT DO
-------------------
It blocks NEW ENTRIES only. Open positions are refreshed, graded, stopped and
targeted normally throughout the blocked window -- refusing to manage an open
position because of the clock would be a far worse rule than the one it
replaces, and would leave real exposure unattended for two hours a day.

Crypto is never gated: there is no session, and no equivalent structural trough.

TIMEZONE
--------
The window is expressed in EXCHANGE-LOCAL time (America/New_York), not UTC.
The boundary tracks the trading session, and a UTC constant would silently
shift by an hour twice a year -- when the US changes clocks and Israel, where
this runs, changes on a different date.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from config import (
    SESSION_BLOCK_WINDOWS_ET,
    SESSION_FILTER_ASSET_CLASSES,
    SESSION_FILTER_ENABLED,
)

_ET = ZoneInfo("America/New_York")


def exchange_now(now: dt.datetime | None = None) -> dt.datetime:
    """`now` as exchange-local time.

    A naive datetime is treated as local wall clock (which is what
    `datetime.now()` returns everywhere else in this codebase) and converted,
    so callers do not have to think about it.
    """
    moment = now or dt.datetime.now()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(_ET)


def blocked_window(now: dt.datetime | None = None) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """The blocking window `now` falls inside, or None.

    Returns the window rather than a bool so the caller can say WHICH window
    blocked an entry -- "no setups found" and "entries are blocked until 13:30
    ET" are very different messages to read in a log at 12:15.
    """
    local = exchange_now(now)
    minutes = local.hour * 60 + local.minute
    for (start_h, start_m), (end_h, end_m) in SESSION_BLOCK_WINDOWS_ET:
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        # Inclusive of start, exclusive of end: a window ending at 13:30 must
        # not also block the 13:30 bar, which belongs to the afternoon.
        if start <= minutes < end:
            return ((start_h, start_m), (end_h, end_m))
    return None


def entries_blocked(asset_class: str, now: dt.datetime | None = None) -> tuple[bool, str]:
    """Whether NEW entries are blocked for `asset_class` right now.

    Returns `(blocked, reason)`. The reason is always populated when blocked,
    because it is written straight into the funnel/rejection log.
    """
    if not SESSION_FILTER_ENABLED:
        return False, ""
    if asset_class not in SESSION_FILTER_ASSET_CLASSES:
        return False, ""

    window = blocked_window(now)
    if window is None:
        return False, ""

    (start_h, start_m), (end_h, end_m) = window
    local = exchange_now(now)
    return True, (f"session filter: new {asset_class} entries are blocked during the "
                  f"{start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d} ET midday "
                  f"liquidity trough (now {local:%H:%M} ET)")
