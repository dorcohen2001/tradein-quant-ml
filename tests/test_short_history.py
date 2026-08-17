"""Instruments whose history is SHORTER than the window asked for.

A recently listed ticker is not a broken ticker. ARM listed in September 2023;
a request for five years of weekly context reaches back to 2020, Yahoo answers
"possibly delisted; no price data found", and a naive layer concludes the
symbol is dead and skips it.

Two separate defects produced that, and both are pinned here:

  1. The window was over-wide. `get_candles` padded every request by 3x the bar
     span, which is right for intraday (a bar only exists during a 6.5h
     session) and badly wrong for daily and coarser, where the calendar is only
     7/5 of the trading days. A `period="2y"` weekly request therefore asked
     for SIX years.

  2. The gap was re-requested forever. A head segment that comes back empty
     caches nothing, so the same span is "missing" again next cycle -- one
     futile round trip per IPO ticker per cycle, permanently, against the same
     rate budget the live scan needs.

Every test here runs against a STUB PROVIDER that mimics a listing date. No
network, no cassettes to refresh, and the failure is reproducible in
milliseconds on a machine that has never heard of ARM.
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.backtesting.cache import CandleCache, empty_frame  # noqa: E402
from core.backtesting.feed import IntradayDataFeed  # noqa: E402
from core.backtesting.intervals import interval_of  # noqa: E402

LISTED = pd.Timestamp("2023-09-11")     # the week ARM began trading
TODAY = pd.Timestamp("2026-08-17")


class ListingAwareProvider:
    """A venue that simply has no data before an instrument existed.

    Counts every request, because the bug being pinned is not "wrong data" --
    the data was always right -- it is a request that repeats forever.
    """

    venue = "yahoo"

    def __init__(self, listed: pd.Timestamp = LISTED) -> None:
        self.listed = listed
        self.requests: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    def fetch(self, symbol, interval, start, end):
        self.requests.append((start, end))
        first = max(pd.Timestamp(start), self.listed)
        if first > pd.Timestamp(end):
            return empty_frame()        # entirely before listing: nothing exists
        index = pd.date_range(first, pd.Timestamp(end), freq="7D")
        if len(index) == 0:
            return empty_frame()
        return pd.DataFrame(
            {"Open": 10.0, "High": 11.0, "Low": 9.0, "Close": 10.5, "Volume": 1e6},
            index=index,
        )


@pytest.fixture
def feed(tmp_path):
    provider = ListingAwareProvider()
    f = IntradayDataFeed(cache_dir=str(tmp_path), verbose=False,
                         providers={"yahoo": provider, "binance": provider})
    f.test_provider = provider
    return f


def _window():
    return LISTED - pd.Timedelta(days=1200), TODAY   # deliberately pre-listing


# ==========================================================================
# The data must arrive
# ==========================================================================
def test_a_recently_listed_instrument_returns_its_available_history(feed):
    start, end = _window()
    frame = feed.get_candles("ARM", "stocks", "1wk", start, end)

    assert len(frame) > 0, "a listed, trading instrument must never come back empty"
    assert frame.index[0] >= LISTED


def test_it_is_not_mistaken_for_a_delisted_symbol(feed):
    """The distinguishing property: a genuinely dead symbol returns nothing at
    ANY point in the window; a young one returns everything after its listing."""
    start, end = _window()
    young = feed.get_candles("ARM", "stocks", "1wk", start, end)

    dead = IntradayDataFeed(
        cache_dir=str(feed.cache.root), verbose=False,
        providers={"yahoo": ListingAwareProvider(listed=pd.Timestamp("2099-01-01")),
                   "binance": ListingAwareProvider()},
    ).get_candles("DEAD", "stocks", "1wk", start, end)

    assert len(young) > 0
    assert len(dead) == 0


def test_history_shorter_than_the_request_is_not_an_error(feed):
    """Asking for more than exists must return what exists, not raise."""
    start, end = _window()
    frame = feed.get_candles("ARM", "stocks", "1wk", start, end)
    expected_weeks = len(pd.date_range(LISTED, end, freq="7D"))
    assert len(frame) == expected_weeks


# ==========================================================================
# ...and the futile request must stop
# ==========================================================================
def test_the_pre_listing_gap_is_requested_at_most_once(feed):
    """THE regression. Before this, every cycle re-requested the same
    impossible span: one wasted round trip per IPO ticker per cycle, forever,
    each one logged as "possibly delisted"."""
    start, end = _window()

    feed.get_candles("ARM", "stocks", "1wk", start, end)
    after_first = len(feed.test_provider.requests)

    # A second process (fresh memo, same disk cache) must not repeat it.
    second = IntradayDataFeed(cache_dir=str(feed.cache.root), verbose=False,
                              providers={"yahoo": feed.test_provider,
                                         "binance": feed.test_provider})
    second.get_candles("ARM", "stocks", "1wk", start, end)

    new_requests = feed.test_provider.requests[after_first:]
    pre_listing = [(s, e) for s, e in new_requests if e <= LISTED]
    assert pre_listing == [], f"re-requested history that cannot exist: {pre_listing}"


def test_the_listing_floor_is_persisted_for_other_processes(feed):
    """Tenants are separate OS processes; an in-memory floor would help none of
    them. It has to reach disk, next to the series it describes."""
    start, end = _window()
    feed.get_candles("ARM", "stocks", "1wk", start, end)

    floor = CandleCache(feed.cache.root).earliest_available("yahoo", "ARM", "1w")
    assert floor is not None
    assert floor >= LISTED


def test_a_genuine_tail_extension_is_still_fetched(feed):
    """The floor must not freeze the series: new bars still arrive.

    The window ENDS in the past on the first call and is extended on the
    second. Extending past `now` would prove nothing -- the feed clamps `end`
    to the current time so a run never re-requests a range that cannot exist
    yet.
    """
    start, _ = _window()
    stale_end = TODAY - pd.Timedelta(days=200)

    feed.get_candles("ARM", "stocks", "1wk", start, stale_end)
    before = len(feed.test_provider.requests)

    later = IntradayDataFeed(cache_dir=str(feed.cache.root), verbose=False,
                             providers={"yahoo": feed.test_provider,
                                        "binance": feed.test_provider})
    frame = later.get_candles("ARM", "stocks", "1wk", start, TODAY - pd.Timedelta(days=5))

    tail_requests = feed.test_provider.requests[before:]
    assert tail_requests, "a tail extension must still download"
    assert all(s >= stale_end - pd.Timedelta(days=14) for s, _e in tail_requests), \
        "the extension must fetch only the TAIL, not re-walk the whole series"
    assert frame.index[-1] > stale_end


def test_the_floor_never_shrinks_a_known_series(tmp_path):
    """A transient empty response must not permanently narrow what we believe
    exists -- that would silently truncate a long history."""
    cache = CandleCache(str(tmp_path))
    cache.record_earliest("yahoo", "AAPL", "1w", pd.Timestamp("1996-09-23"))
    cache.record_earliest("yahoo", "AAPL", "1w", pd.Timestamp("2024-01-01"))

    assert cache.earliest_available("yahoo", "AAPL", "1w") == pd.Timestamp("1996-09-23")


def test_corrupt_metadata_is_ignored_not_fatal(tmp_path):
    """It is an optimisation hint. Losing it costs one redundant request;
    raising on it would cost the whole scan."""
    cache = CandleCache(str(tmp_path))
    path = cache.meta_path("yahoo", "ARM", "1w")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    assert cache.earliest_available("yahoo", "ARM", "1w") is None


# ==========================================================================
# The window that started it
# ==========================================================================
def test_daily_and_coarser_windows_are_not_padded_like_intraday():
    """3x is right for a bar that only exists inside a 6.5h session, and wrong
    for one that exists on trading days: it turned a 2-year weekly request into
    a 6-year one, reaching back past the listing date of every post-2020 IPO in
    the watchlist."""
    import datetime as dt

    from core.data.market import interval_minutes, quantized_end

    captured = {}

    class Recorder:
        def get_candles(self, symbol, asset_class, interval, start, end):
            captured[interval] = (end - start).days
            return empty_frame()

    from core.data.market import get_candles

    get_candles("ARM", "stocks", "1wk", bars=105, feed=Recorder())
    get_candles("ARM", "stocks", "15m", bars=100, feed=Recorder())

    weekly_years = captured["1wk"] / 365
    assert 2.5 < weekly_years < 4.0, f"weekly window is {weekly_years:.1f} years"

    # Intraday keeps the generous padding it genuinely needs.
    intraday_span = captured["15m"] * 24 * 60
    assert intraday_span >= interval_minutes("15m") * 100 * 2.5
