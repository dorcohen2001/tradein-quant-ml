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
