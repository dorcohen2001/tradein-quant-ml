"""One shared candle accessor for every tenant.

THE PROBLEM THIS SOLVES
-----------------------
The platform grew two independent market-data layers:

  * `core/utils/yf_cache.py` + each model's own `utils/data_fetch.py`
    (~550 duplicated lines across two tenants), caching into
    `runtime/cache/yfinance/`.
  * `core/backtesting/IntradayDataFeed`, caching into `data/`.

So two tenants scanning BTCUSDT 15m fetched it twice from Binance, and the
same symbol could be cached in two places in two formats. That is wasted rate
budget, and it means tenants are not behaviourally interchangeable.

This module is the single entry point. It wraps `IntradayDataFeed` -- which
already handles venue selection, pagination, interval fallback, the yfinance
lookback caps, and an on-disk cache shared across processes -- and adds the one
thing a LIVE caller needs that a backtest does not: a bounded notion of "fresh
enough".

HOW CROSS-PROCESS DEDUPLICATION IS GUARANTEED
---------------------------------------------
Every tenant runs as its own OS process, so an in-process memo cannot help
them share. The disk cache can, but only if they ask for the SAME window --
and two processes calling `now()` a few seconds apart would each request a
slightly different end time, so the second would see a "missing" trailing
segment and download it.

The end of the window is therefore QUANTIZED to the interval's cache bucket
(`core.data.cache_policy`). Every caller inside the same bucket computes a
byte-identical (start, end) pair, hits the same cache entry, and the second one
performs no network call at all. The bucket widths are the same ones the live
path already used for its TTLs: 60s for sub-hourly bars, 5min for 1h-4h, 30min
for daily and above -- so a cache entry can never outlive the bar it describes.

WHAT THIS DOES NOT COVER
------------------------
Candles only. Live last-price, market cap, earnings dates, order-book depth and
24h tickers are not OHLCV series and have no representation in a candle feed;
they stay in each tenant's data layer, where they are already cached with their
own TTLs. Claiming this module unified "all market data" would be false.
"""
from __future__ import annotations

import datetime as dt
import threading

import pandas as pd

from core.backtesting.feed import IntradayDataFeed
from core.data.cache_policy import bucket_seconds_for

# Minutes per interval token, for turning "N bars" into a time window.
_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440, "w": 10080}

# One feed per process. It owns the disk cache handle and an in-process memo,
# so sharing it across a tenant's own calls avoids re-reading the same Parquet
# file several times in one cycle.
_feed: IntradayDataFeed | None = None
_feed_lock = threading.Lock()


def interval_minutes(interval: str) -> int:
    """Bar length in minutes. `15m` -> 15, `1h` -> 60, `1d` -> 1440."""
    token = interval.strip().lower()
    unit = token[-1]
    try:
        value = int(token[:-1])
    except ValueError as exc:
        raise ValueError(f"unrecognised interval {interval!r}") from exc
    if unit not in _UNIT_MINUTES:
        raise ValueError(f"unrecognised interval unit in {interval!r}")
    return value * _UNIT_MINUTES[unit]


def get_feed(verbose: bool = False) -> IntradayDataFeed:
    """The process-wide feed. Lazily built and thread-safe: the supervisor runs
    a thread per tenant, and two threads racing to construct one would give
    them separate memos and separate cache handles."""
    global _feed
    if _feed is None:
        with _feed_lock:
            if _feed is None:
                _feed = IntradayDataFeed(verbose=verbose)
    return _feed


def quantized_end(interval: str, now: dt.datetime | None = None) -> pd.Timestamp:
    """`now` floored to this interval's cache bucket, in naive UTC.

    This is the whole deduplication mechanism. Two tenant processes calling a
    few seconds apart floor to the SAME instant, so they request an identical
    window and the second is served entirely from the shared disk cache.
    """
    moment = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
    bucket = bucket_seconds_for(interval)
    epoch = int(moment.timestamp())
    floored = dt.datetime.fromtimestamp((epoch // bucket) * bucket, dt.timezone.utc)
    return pd.Timestamp(floored.replace(tzinfo=None))


def get_candles(symbol: str, asset_class: str, interval: str = "1d",
                bars: int = 300, now: dt.datetime | None = None,
                feed: IntradayDataFeed | None = None) -> pd.DataFrame:
    """The `bars` most recent candles for `symbol` on `interval`.

    Returns a DataFrame indexed by naive-UTC timestamps with Open/High/Low/
    Close/Volume columns, oldest first -- the canonical shape produced by
    `core.backtesting.cache.normalize_frame`, which every consumer in this
    repository already expects.

    Never raises: an unlisted pair, a delisted ticker or a network failure
    comes back as an empty frame, so one bad symbol skips instead of aborting a
    whole watchlist scan.

    The requested window is deliberately over-wide (`bars * 3` bar-lengths)
    before being tail-sliced. Sessions have gaps -- weekends, holidays, halts,
    and for crypto the occasional outage -- so asking for exactly `bars` worth
    of wall-clock time reliably returns fewer than `bars` rows.
    """
    feed = feed or get_feed()
    end = quantized_end(interval, now)
    span = dt.timedelta(minutes=interval_minutes(interval) * max(bars, 1) * 3)
    start = end - span

    frame = feed.get_candles(symbol, asset_class, interval, start, end)
    if frame is None or len(frame) == 0:
        return frame if frame is not None else pd.DataFrame()
    return frame.tail(bars)


# ---------------------------------------------------------------------------
# Timezone-preserving stock adapter
# ---------------------------------------------------------------------------
# The exchange timezone US equity data is quoted in. This is NOT cosmetic.
#
# The shared feed normalises everything to naive UTC, which is right for
# storage and for crypto (Binance is UTC-native and trades continuously). It is
# WRONG to hand to a consumer that resamples: `models/livne/utils/data_fetch.
# resample_ohlc` builds the 12h/4h/3h frames Livne's HTF zone map depends on,
# and pandas anchors resample buckets to the index's own midnight. A naive-UTC
# index anchors those buckets to 00:00 UTC (20:00 the previous ET day), which
# silently shifts every bucket boundary and therefore every zone.
#
# Converting back to America/New_York reproduces the legacy fetcher's index
# exactly: a daily bar stored as 2026-08-14 04:00 UTC becomes
# 2026-08-14 00:00-04:00, which is what yfinance returned directly.
EXCHANGE_TZ = "America/New_York"

# Trading days per calendar period, for translating the legacy fetcher's
# `period=` strings into a bar count. Deliberately generous -- the frame is
# tail-sliced afterwards, and asking for too few bars silently truncates
# indicator warm-up.
_PERIOD_TRADING_DAYS = {
    "1d": 1, "5d": 5, "1mo": 23, "3mo": 66, "6mo": 130,
    "1y": 260, "2y": 520, "5y": 1300, "10y": 2600, "max": 5200,
}


def period_to_bars(period: str, interval: str) -> int:
    """How many bars of `interval` a legacy `period=` string covers.

    Used only to translate a call written against the old fetcher; a caller
    that already thinks in bars should pass `bars` directly.
    """
    days = _PERIOD_TRADING_DAYS.get(period.strip().lower())
    if days is None:
        days = 260
    minutes_per_day = 390 if interval_minutes(interval) < 1440 else 1440
    per_day = max(1, minutes_per_day // max(interval_minutes(interval), 1))
    return max(int(days * per_day), 1)


def to_exchange_tz(frame: pd.DataFrame, tz: str = EXCHANGE_TZ) -> pd.DataFrame:
    """Re-attach exchange-local timezone to a naive-UTC frame.

    The feed stores naive UTC; consumers that resample need the tz-aware
    exchange-local index the legacy fetcher produced. Localising to UTC and
    converting is exact -- it relabels the same instants, it does not move any
    bar.

    A frame that is already tz-aware is converted, not double-localised, so this
    is safe to apply twice.
    """
    if frame is None or len(frame) == 0:
        return frame
    out = frame.copy()
    if getattr(out.index, "tz", None) is None:
        out.index = out.index.tz_localize("UTC").tz_convert(tz)
    else:
        out.index = out.index.tz_convert(tz)
    return out


def get_stock_candles(ticker: str, interval: str = "1d", bars: int = 300,
                      period: str | None = None, tz: str | None = EXCHANGE_TZ,
                      now: dt.datetime | None = None, lookback_days: float | None = None,
                      feed: IntradayDataFeed | None = None) -> pd.DataFrame:
    """Stock candles from the shared layer, with the exchange timezone intact.

    This is the drop-in replacement for a model's own `get_stock_history`.
    Pass `period=` to mirror a legacy call ("1y", "6mo"); pass `bars=` to be
    explicit. `tz=None` returns the raw naive-UTC frame for callers that do not
    resample and prefer the storage form.

    Column set is OHLCV only. The legacy fetcher also carried `Dividends` and
    `Stock Splits`; a repo-wide search found no consumer of either, and every
    indicator, screener gate and chart reads only OHLCV.
    """
    if lookback_days is not None:
        # Explicit CALENDAR window. Required for intraday, because the bars->window
        # heuristic in `get_candles` assumes bars are spread across the clock and
        # US equities trade only 6.5h of each 24. At 15m that heuristic delivers
        # roughly a sixth of the bars asked for -- enough to pass an
        # overlap-based parity check while silently handing downstream code a
        # much shorter frame, which changes EMA-200 warm-up and the depth of
        # history the zone map is built from.
        feed = feed or get_feed()
        end = quantized_end(interval, now)
        start = end - dt.timedelta(days=lookback_days)
        frame = feed.get_candles(ticker, "stocks", interval, start, end)
        if frame is not None and len(frame):
            frame = frame.tail(bars) if bars and len(frame) > bars else frame
    else:
        if period is not None:
            bars = period_to_bars(period, interval)
        frame = get_candles(ticker, "stocks", interval, bars=bars, now=now, feed=feed)
    return to_exchange_tz(frame, tz) if tz else frame


def download_count(feed: IntradayDataFeed | None = None) -> int:
    """How many network downloads the shared feed has performed. Used by the
    deduplication tests to assert that a second tenant's identical request
    costs zero additional calls."""
    return (feed or get_feed()).download_count


def reset_feed() -> None:
    """Drop the process-wide feed. Tests only -- it discards the in-process
    memo so a fresh download count can be observed."""
    global _feed
    with _feed_lock:
        _feed = None
