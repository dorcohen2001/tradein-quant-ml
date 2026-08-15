"""How long a cached candle frame may be reused, by bar interval.

THE RULE
--------
A cache entry must never outlive the bar it describes. If it does, the strategy
evaluates a candle that already closed -- and on the live path that means a
stop or target is checked against stale bars.

Both models had a `_bucket_seconds_for`, but they had drifted:

    Livne   three tiers: 60s for <=30m bars, 300s for 1h-4h, 1800s otherwise.
    Biton   two tiers:   60s for <=15m bars, 1800s for everything else.

So a 1h frame was cached for a full 30 minutes under Biton -- half the bar
period, meaning every second cycle re-used the previous cycle's data verbatim.
That is the exact class of bug the tiering was introduced to fix for Livne's
sub-hourly intervals; Biton simply never had the middle tier added.

This module is the single tiered policy, applied to both. For Biton it is
strictly a FRESHNESS improvement: 1h-4h frames now refresh every 5 minutes
instead of every 30. No threshold, rule or signal changes -- only how stale the
bars underneath them may be.

`_bucket()` is a coarse time quantiser rather than a real cache: the returned
integer is passed as a function argument into the memoised fetchers, so every
call within the same bucket window hits the same memo entry and the first call
of a new window misses and re-fetches.
"""
from __future__ import annotations

import datetime as dt

# 30-minute buckets for daily/weekly context frames, which barely move
# intraday.
HISTORY_CACHE_SECONDS = 1800
# Sub-hourly execution bars: must refresh well inside the bar period.
INTRADAY_CACHE_SECONDS = 60
# 1h-4h refinement/confirmation frames.
MIDRANGE_CACHE_SECONDS = 300

LIVE_INTRADAY_INTERVALS = frozenset({"1m", "3m", "5m", "15m", "30m"})
MIDRANGE_INTERVALS = frozenset({"1h", "2h", "3h", "4h"})


def bucket_seconds_for(interval: str) -> int:
    """Cache-bucket width for a candle interval, tiered to the bar period."""
    if interval in LIVE_INTRADAY_INTERVALS:
        return INTRADAY_CACHE_SECONDS
    if interval in MIDRANGE_INTERVALS:
        return MIDRANGE_CACHE_SECONDS
    return HISTORY_CACHE_SECONDS


def bucket(seconds: int = HISTORY_CACHE_SECONDS) -> int:
    """Current time quantised to `seconds`-wide windows."""
    return int(dt.datetime.now(dt.timezone.utc).timestamp() // seconds)
