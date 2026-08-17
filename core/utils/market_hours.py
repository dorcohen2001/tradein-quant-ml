"""US equity market session gating (NYSE/NASDAQ regular hours, weekends, holidays).

Only the "stocks" asset class is gated by this - crypto trades 24/7 and must
keep scanning/updating regardless of US market session.

THE SESSION IS CACHED PER DATE
------------------------------
`mcal.schedule()` rebuilds a DataFrame from the exchange's holiday rules on
every call, and this function is called far more often than the once-a-day its
answer actually changes: per asset class in the scan, per catch-up pass, per
position refresh, and once per trade inside the tracker's tradeable filter.
With several tenants on short cadences that is thousands of identical
reconstructions an hour.

A day's open/close are FIXED once the date is known -- holidays and early
closes are published in advance, not discovered intraday -- so one entry per
date is exact, not an approximation. Only that (open, close) pair is memoised;
the "is now inside it" comparison still runs on every call, so the answer keeps
changing at 09:30 and 16:00 ET as it must.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

_NYSE = mcal.get_calendar("NYSE")
_ET = ZoneInfo("America/New_York")

# date -> (open, close) in ET, or None when the exchange is shut that day.
# Bounded by construction: one entry per calendar date the process asks about,
# and a tenant process is short-lived (`--once` per supervisor tick).
_SESSIONS: dict[dt.date, tuple[dt.datetime, dt.datetime] | None] = {}


def session_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
    """(open, close) in ET for `day`, or None if the exchange is closed.

    Exposed so callers that need the actual boundaries -- an early close, the
    next open -- do not have to re-derive them from the calendar.
    """
    if day in _SESSIONS:
        return _SESSIONS[day]

    schedule = _NYSE.schedule(start_date=day, end_date=day)
    if schedule.empty:
        bounds = None  # weekend or market holiday - no session that day
    else:
        bounds = (schedule.iloc[0]["market_open"].tz_convert(_ET).to_pydatetime(),
                  schedule.iloc[0]["market_close"].tz_convert(_ET).to_pydatetime())
    _SESSIONS[day] = bounds
    return bounds


def is_market_open(now: dt.datetime | None = None) -> bool:
    """True only during regular NYSE/NASDAQ trading hours (09:30-16:00 ET),
    Monday-Friday, excluding official market holidays and on early-close days
    after the actual (shortened) close."""
    now_utc = now or dt.datetime.now(dt.timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)
    now_et = now_utc.astimezone(_ET)

    bounds = session_bounds(now_et.date())
    if bounds is None:
        return False
    market_open, market_close = bounds
    return market_open <= now_et <= market_close


def clear_session_cache() -> None:
    """Drop the memoised sessions. For tests, and for the rare long-lived
    process that outlives a calendar revision."""
    _SESSIONS.clear()
