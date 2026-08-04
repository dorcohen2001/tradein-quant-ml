"""US equity market session gating (NYSE/NASDAQ regular hours, weekends, holidays).

Only the "stocks" asset class is gated by this - crypto trades 24/7 and must
keep scanning/updating regardless of US market session.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

_NYSE = mcal.get_calendar("NYSE")
_ET = ZoneInfo("America/New_York")


def is_market_open(now: dt.datetime | None = None) -> bool:
    """True only during regular NYSE/NASDAQ trading hours (09:30-16:00 ET),
    Monday-Friday, excluding official market holidays and on early-close days
    after the actual (shortened) close."""
    now_utc = now or dt.datetime.now(dt.timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)
    now_et = now_utc.astimezone(_ET)

    schedule = _NYSE.schedule(start_date=now_et.date(), end_date=now_et.date())
    if schedule.empty:
        return False  # weekend or market holiday - no session today

    market_open = schedule.iloc[0]["market_open"].tz_convert(_ET)
    market_close = schedule.iloc[0]["market_close"].tz_convert(_ET)
    return market_open <= now_et <= market_close
