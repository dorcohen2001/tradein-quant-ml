"""Interval vocabulary: one registry, both venues' spellings.

THE BUG THIS PINS
-----------------
Binance calls a weekly bar `1w`; yfinance calls it `1wk`. The interval registry
knew that. `core.data.market.interval_minutes` did not -- it parsed
`<digits><one letter>` itself, so `1wk` hit `int("1w")` and raised. That
surfaced through the screener's per-ticker guard as

    [screener] AAPL skipped: unrecognised interval '1wk'

for EVERY stock symbol on EVERY cycle. The weekly frame is the first thing
`ta.mtfa._fetch_frames` requests, so nothing downstream of it ran: the stock
half of the funnel produced zero candidates while looking exactly like a quiet
market.

The lesson these tests encode is not "accept 1wk". It is that an interval
string has ONE authority (`core.backtesting.intervals`), and any module that
parses one itself will eventually disagree with a venue.
"""
from __future__ import annotations

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.backtesting.intervals import canonical_name, interval_of, interval_span  # noqa: E402
from core.data.cache_policy import (  # noqa: E402
    HISTORY_CACHE_SECONDS,
    INTRADAY_CACHE_SECONDS,
    MIDRANGE_CACHE_SECONDS,
    bucket_seconds_for,
)
from core.data.market import interval_minutes, period_to_bars  # noqa: E402


# ==========================================================================
# The regression itself
# ==========================================================================
@pytest.mark.parametrize("weekly", ["1w", "1wk", "weekly", "1WK", " 1wk "])
def test_every_weekly_spelling_resolves(weekly):
    """`1wk` is what models/livne/config.py's TF_INTERVALS carries for stocks,
    because it is what yfinance answers to."""
    assert canonical_name(weekly) == "1w"
    assert interval_minutes(weekly) == 7 * 24 * 60


def test_the_exact_call_that_skipped_every_stock():
    """`get_stock_history(period="2y", interval="1wk")` -> period_to_bars ->
    interval_minutes. This is the line the screener died on."""
    assert period_to_bars("2y", "1wk") > 0


@pytest.mark.parametrize("interval,minutes", [
    ("5m", 5), ("15m", 15), ("30m", 30),
    ("60m", 60), ("1h", 60), ("hourly", 60),
    ("4h", 240), ("12h", 720),
    ("1d", 1440), ("daily", 1440),
    ("1w", 10080), ("1wk", 10080), ("weekly", 10080),
])
def test_the_whole_vocabulary_has_a_bar_length(interval, minutes):
    assert interval_minutes(interval) == minutes


def test_an_unknown_bar_size_still_parses_by_shape():
    """The registry has no 45m. The generic parser stays as a fallback so
    nothing that worked before this change stops working."""
    assert interval_minutes("45m") == 45
    assert canonical_name("45m") == "45m"          # unknown -> returned as-is


def test_genuine_nonsense_still_raises_loudly():
    """A typo must fail, not quietly resolve to some other bar size."""
    with pytest.raises(ValueError):
        interval_minutes("nonsense")
    with pytest.raises(ValueError):
        interval_minutes("10x")


def test_the_error_names_what_is_supported():
    with pytest.raises(ValueError) as excinfo:
        interval_minutes("nonsense")
    message = str(excinfo.value)
    assert "1wk" in message and "1w" in message


# ==========================================================================
# Aliases must not fork the cache
# ==========================================================================
def test_both_weekly_spellings_key_the_same_cached_series():
    """The shared data layer exists so tenants reuse one cached series. If
    `1w` and `1wk` resolved to different names they would write
    data/yahoo/AAPL_1w.parquet AND AAPL_1wk.parquet, and the deduplication
    would silently stop working."""
    assert interval_of("1wk").name == interval_of("1w").name == "1w"


def test_venue_mapping_survives_the_alias():
    """Normalising must not lose the venue's own spelling on the way out."""
    spec = interval_of("1wk")
    assert spec.yahoo == "1wk"     # what yfinance is actually asked for
    assert spec.binance == "1w"    # what Binance is actually asked for


@pytest.mark.parametrize("interval,expected", [
    ("5m", INTRADAY_CACHE_SECONDS),
    ("15m", INTRADAY_CACHE_SECONDS),
    ("1h", MIDRANGE_CACHE_SECONDS),
    ("60m", MIDRANGE_CACHE_SECONDS),   # the alias must land in the SAME tier
    ("4h", MIDRANGE_CACHE_SECONDS),
    ("1d", HISTORY_CACHE_SECONDS),
    ("1wk", HISTORY_CACHE_SECONDS),
])
def test_cache_tier_follows_the_bar_period_not_the_spelling(interval, expected):
    """Written `60m`, a 1h frame missed the 1h-4h tier and was cached for half
    an hour -- the exact staleness cache_policy exists to prevent, reintroduced
    by spelling."""
    assert bucket_seconds_for(interval) == expected


def test_interval_span_reports_none_for_unknown_rather_than_raising():
    assert interval_span("1wk") is not None
    assert interval_span("45m") is None


# ==========================================================================
# period_to_bars: a weekly bar is five trading days, not one
# ==========================================================================
@pytest.mark.parametrize("period,expected", [("2y", 105), ("1y", 53), ("6mo", 27)])
def test_weekly_bar_counts_match_the_legacy_fetcher(period, expected):
    """Measured against `yf.Ticker("AAPL").history(period=..., interval="1wk")`.

    The old arithmetic floored 1440 // 10080 to 0, clamped it to 1, and so
    treated a weekly bar as a daily one: `2y` asked for 520 weekly bars, i.e.
    TEN YEARS of history where the legacy call returned two. Not a harmless
    over-fetch -- `ta.mtfa` builds its zone map from this frame's swing
    extremes, so the extra history changes which levels a setup is measured
    against.
    """
    assert period_to_bars(period, "1wk") == expected
    assert period_to_bars(period, "1w") == expected


@pytest.mark.parametrize("period,expected", [("2y", 520), ("1y", 260), ("6mo", 130)])
def test_daily_bar_counts_are_unchanged(period, expected):
    """The 1d path is the one `scripts/verify_stock_parity.py` proved equal to
    the legacy fetcher across all 70 watchlist symbols. It must not move."""
    assert period_to_bars(period, "1d") == expected


def test_intraday_bar_counts_are_unchanged():
    """Sessions are 390 minutes, not 1440 -- that branch is untouched."""
    assert period_to_bars("1mo", "15m") == 23 * 26
    assert period_to_bars("5d", "5m") == 5 * 78


def test_a_weekly_window_is_never_shorter_than_the_indicator_warm_up():
    """MIN_ZONE_HISTORY_BARS is 30. A period that cannot supply that many
    weekly bars would silently disable zone discovery on the weekly frame."""
    assert period_to_bars("1y", "1wk") >= 30
