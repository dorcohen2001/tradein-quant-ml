"""Raw candle providers: Binance public REST and yfinance.

Both expose the same signature -- `fetch(symbol, interval, start, end)` ->
canonical OHLCV frame -- so `feed.IntradayDataFeed` can treat "where the
candles come from" as a detail. Neither provider caches anything or knows
what a strategy is; caching is `cache.CandleCache`'s job and is applied one
layer up.

Binance: `/api/v3/klines` caps a single response at 1000 candles, so long
windows are walked with a startTime/endTime cursor. On a 5m interval that is
one request per ~3.5 days, i.e. ~105 requests for a year -- which is exactly
why the result gets cached. Requests are paced and 429/418 rate-limit
responses are honoured (`Retry-After`) rather than retried blindly, because
being IP-banned mid-download costs far more than the pause does.

yfinance: intraday history is hard-capped per interval (see
`intervals.Interval.yahoo_max_lookback_days`). Requests are clamped to the
cap, chunked to `yahoo_max_request_days`, and the caller is told -- via the
returned frame simply starting later than asked -- rather than the request
failing. `feed` turns "the whole window predates the cap" into a documented
fall back to a coarser interval.
"""
from __future__ import annotations

import datetime as dt
import sys
import time

import pandas as pd
import requests

from core.backtesting.cache import empty_frame, normalize_frame
from core.backtesting.intervals import Interval

BINANCE_BASE_URL = "https://api.binance.com"
REQUEST_TIMEOUT = 20

_KLINE_COLUMNS = [
    "open_time", "Open", "High", "Low", "Close", "Volume", "close_time",
    "quote_asset_volume", "num_trades", "taker_buy_base", "taker_buy_quote", "ignore",
]


def _to_ms(ts) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def _log(msg: str, verbose: bool) -> None:
    if verbose:
        print(msg, file=sys.stderr)


class BinanceKlineProvider:
    """Paginated downloader for Binance spot klines (no auth required).

    `pace_seconds` spaces consecutive requests. Binance's spot REST limit is
    1200 request-weight per minute and a 1000-candle klines call costs 2, so
    ~8 requests/second would already be legal; 0.12s is deliberately well
    inside that, since a backtest download is not latency-sensitive and an
    IP ban is measured in minutes.
    """

    venue = "binance"

    def __init__(self, base_url: str = BINANCE_BASE_URL, pace_seconds: float = 0.12,
                 max_retries: int = 4, verbose: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.pace_seconds = pace_seconds
        self.max_retries = max_retries
        self.verbose = verbose
        self._session = requests.Session()

    def _request(self, params: dict) -> list:
        url = f"{self.base_url}/api/v3/klines"
        delay = 1.0
        for attempt in range(self.max_retries):
            resp = self._session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code in (418, 429):
                # 429 = rate limited, 418 = already banned for ignoring a 429.
                wait = float(resp.headers.get("Retry-After", delay))
                _log(f"[data] binance rate limit ({resp.status_code}); sleeping {wait:.0f}s", self.verbose)
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue
            if resp.status_code >= 500:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"binance klines failed after {self.max_retries} attempts: {params}")

    def fetch(self, symbol: str, interval: Interval, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        native = interval.binance
        if native is None:
            raise ValueError(f"binance has no native {interval.name} interval")

        start_ms, end_ms = _to_ms(start), _to_ms(end)
        step_ms = int(interval.delta.value // 1_000_000)
        rows: list = []
        cursor = start_ms

        while cursor < end_ms:
            batch = self._request({
                "symbol": symbol, "interval": native,
                "startTime": cursor, "endTime": end_ms, "limit": 1000,
            })
            if not batch:
                break
            rows.extend(batch)
            # Advance past the last candle's OPEN time by one full bar. Using
            # `+1ms` instead would re-request the same final candle forever
            # whenever a batch comes back short of the 1000-row cap.
            next_cursor = int(batch[-1][0]) + step_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < 1000:
                break
            if self.pace_seconds:
                time.sleep(self.pace_seconds)

        if not rows:
            return empty_frame()

        df = pd.DataFrame(rows, columns=_KLINE_COLUMNS)
        df.index = pd.to_datetime(df["open_time"].astype("int64"), unit="ms")
        return normalize_frame(df)


class YahooProvider:
    """yfinance-backed downloader with the interval lookback caps handled.

    Two behaviours are worth naming because they surface as "missing" data
    rather than as errors:

      * A `start` older than the interval's cap is silently clamped to the
        cap. Yahoo would otherwise return an empty frame for the whole
        request, discarding the recent data it *would* have served.
      * 1m data is additionally limited to 7 days per request, so the window
        is walked in chunks and concatenated.
    """

    venue = "yahoo"

    def __init__(self, pace_seconds: float = 0.0, max_retries: int = 3, verbose: bool = True) -> None:
        self.pace_seconds = pace_seconds
        self.max_retries = max_retries
        self.verbose = verbose

    @staticmethod
    def earliest_available(interval: Interval, now: pd.Timestamp | None = None) -> pd.Timestamp | None:
        """Oldest timestamp yfinance will serve for this interval, or None when
        the interval has no cap (daily and coarser)."""
        if interval.yahoo_max_lookback_days is None:
            return None
        # tz-naive UTC, matching every other timestamp in this package -- a
        # tz-aware "now" here would raise on comparison against the caller's
        # naive window bounds rather than clamping them.
        now = now or pd.Timestamp.now("UTC").tz_localize(None).normalize()
        # One day of slack: Yahoo's cutoff is approximate and requesting
        # exactly at the boundary intermittently returns nothing.
        return now - pd.Timedelta(days=interval.yahoo_max_lookback_days - 1)

    def _history(self, ticker: str, native: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        import yfinance as yf

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                df = yf.Ticker(ticker).history(
                    start=start.strftime("%Y-%m-%d"),
                    end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    interval=native, auto_adjust=True, prepost=False,
                )
                return normalize_frame(df)
            except Exception as exc:  # network/parse failures are transient far more often than not
                last_exc = exc
                time.sleep(2 * (attempt + 1))
        _log(f"[data] yfinance {ticker} {native} failed: {last_exc}", self.verbose)
        return empty_frame()

    def fetch(self, symbol: str, interval: Interval, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        native = interval.yahoo
        if native is None:
            raise ValueError(f"yfinance has no native {interval.name} interval")

        floor = self.earliest_available(interval)
        if floor is not None and start < floor:
            _log(
                f"[data] {symbol} {interval.name}: requested start {start.date()} predates yfinance's "
                f"~{interval.yahoo_max_lookback_days}-day cap for this interval; clamped to {floor.date()}",
                self.verbose,
            )
            start = floor
        if start >= end:
            return empty_frame()

        chunk_days = interval.yahoo_max_request_days
        if not chunk_days:
            return self._history(symbol, native, start, end)

        frames: list[pd.DataFrame] = []
        cursor = start
        span = pd.Timedelta(days=chunk_days)
        while cursor < end:
            chunk_end = min(cursor + span, end)
            part = self._history(symbol, native, cursor, chunk_end)
            if len(part):
                frames.append(part)
            cursor = chunk_end
            if self.pace_seconds:
                time.sleep(self.pace_seconds)

        if not frames:
            return empty_frame()
        return normalize_frame(pd.concat(frames))


def default_providers(verbose: bool = True) -> dict[str, object]:
    return {"binance": BinanceKlineProvider(verbose=verbose), "yahoo": YahooProvider(verbose=verbose)}


def as_timestamp(value) -> pd.Timestamp:
    """Accept a date, datetime, string or Timestamp and return a tz-naive UTC
    Timestamp, matching the canonical index produced by `cache.normalize_frame`."""
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        value = dt.datetime.combine(value, dt.time.min)
    ts = pd.Timestamp(value)
    if ts.tz is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts
