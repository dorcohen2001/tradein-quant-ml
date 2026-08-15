"""IntradayDataFeed -- the single entry point for "give me candles".

    feed = IntradayDataFeed()                       # caches under <repo>/data
    df = feed.get_candles("BTCUSDT", "crypto", "5m", start, end)
    df = feed.get_candles("AAPL",    "stocks", "15m", start, end)

Responsibilities, in the order they are applied:

  1. Serve from the on-disk cache (`cache.CandleCache`) whenever the cached
     series already spans the requested window.
  2. Otherwise download only the MISSING head and/or tail segments, merge them
     into the cache, and serve from the merged result. Re-running a backtest
     with a slightly extended end date therefore downloads only the new tail.
  3. Build intervals the venue does not serve natively by resampling its 1h
     bars -- the same substitution the live pipeline makes (`config.TF_INTERVALS`
     marks these with `None`).
  4. Degrade gracefully when yfinance's per-interval lookback cap means the
     requested window simply cannot be served at that resolution: fall back to
     the next coarser interval (5m -> 15m -> 1h -> 1d) and say so, instead of
     handing back an empty frame that looks like "no signals found".

Everything it returns is canonical: tz-naive UTC DatetimeIndex, columns
Open/High/Low/Close/Volume, sorted and de-duplicated.
"""
from __future__ import annotations

import os
import sys

import pandas as pd

from core.backtesting.cache import CandleCache, empty_frame
from core.backtesting.intervals import COARSER_FALLBACK, Interval, interval_of, venue_for
from core.backtesting.providers import YahooProvider, as_timestamp, default_providers
from core.backtesting.resampling import resample_ohlcv

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DEFAULT_CACHE_DIR = os.path.join(_REPO_ROOT, "data")


class IntradayDataFeed:
    def __init__(
        self,
        cache_dir: str | None = None,
        cache_format: str = "auto",
        offline: bool = False,
        allow_interval_fallback: bool = True,
        verbose: bool = True,
        providers: dict | None = None,
    ) -> None:
        self.cache = CandleCache(cache_dir or DEFAULT_CACHE_DIR, fmt=cache_format)
        self.offline = offline
        self.allow_interval_fallback = allow_interval_fallback
        self.verbose = verbose
        self.providers = providers or default_providers(verbose=verbose)
        # In-process memo on top of the disk cache: a replay loop asks for the
        # same (symbol, interval, window) once per asset class pass, and
        # re-reading a 100k-row Parquet file for that is pure waste.
        self._memo: dict[tuple, pd.DataFrame] = {}
        self._downloads = 0

    # -- logging ------------------------------------------------------------
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr)

    @property
    def download_count(self) -> int:
        """How many provider round-trips this feed has made -- surfaced by the
        runners so a run that is slow because everything is cold is visibly
        different from one that is slow for any other reason."""
        return self._downloads

    # -- public API ---------------------------------------------------------
    def get_candles(
        self,
        symbol: str,
        asset_class: str,
        interval: str,
        start,
        end,
        allow_fallback: bool | None = None,
    ) -> pd.DataFrame:
        """OHLCV candles for `symbol` on `interval`, covering [start, end].

        Returns an empty frame (never raises) when the symbol has no data at
        all -- a delisted ticker or an unlisted Binance pair must skip that
        asset, not abort the whole backtest.
        """
        spec = interval_of(interval)
        venue = venue_for(asset_class)
        start_ts, end_ts = as_timestamp(start), as_timestamp(end)
        # Callers pad their window with a forward buffer so a signal on the last
        # bar still has bars to be simulated against, which routinely pushes the
        # requested end past today. Clamping it here stops every run from
        # re-requesting a range that cannot exist yet -- Yahoo answers those with
        # a "possibly delisted" warning, and because the cache can never reach
        # that end the same futile download would repeat on every single run.
        now = pd.Timestamp.now("UTC").tz_localize(None)
        end_ts = min(end_ts, now)
        if start_ts >= end_ts:
            return empty_frame()

        memo_key = (venue, symbol, spec.name, start_ts, end_ts)
        if memo_key in self._memo:
            return self._memo[memo_key]

        fallback = self.allow_interval_fallback if allow_fallback is None else allow_fallback
        try:
            df = self._resolve(symbol, venue, spec, start_ts, end_ts, fallback)
        except Exception as exc:
            self._log(f"[data] {symbol} {spec.name}: {type(exc).__name__}: {exc}")
            df = empty_frame()

        self._memo[memo_key] = df
        return df

    # -- internals ----------------------------------------------------------
    def _resolve(
        self, symbol: str, venue: str, spec: Interval,
        start: pd.Timestamp, end: pd.Timestamp, fallback: bool,
    ) -> pd.DataFrame:
        native = spec.venue_interval(venue)

        if native is None:
            return self._build_by_resampling(symbol, venue, spec, start, end, fallback)

        if venue == "yahoo" and spec.yahoo_max_lookback_days is not None:
            floor = YahooProvider.earliest_available(spec)
            if floor is not None and end <= floor:
                # The ENTIRE requested window predates what Yahoo serves at this
                # resolution. Clamping would produce nothing, so step down a
                # rung instead of pretending the window had no candles.
                return self._fallback(symbol, venue, spec, start, end, fallback,
                                       reason=f"window ends {end.date()}, before yfinance's "
                                              f"{spec.yahoo_max_lookback_days}-day {spec.name} cap")

        df = self._cached_or_download(symbol, venue, spec, start, end)
        if len(df) == 0 and fallback:
            return self._fallback(symbol, venue, spec, start, end, fallback,
                                   reason=f"no {spec.name} candles returned")
        return df

    def _build_by_resampling(
        self, symbol: str, venue: str, spec: Interval,
        start: pd.Timestamp, end: pd.Timestamp, fallback: bool,
    ) -> pd.DataFrame:
        base_name = spec.resample_base or "1h"
        base_spec = interval_of(base_name)
        # Pad the base window by one target bar so the first returned bar is a
        # complete one rather than a fragment of the bar `start` falls inside.
        base = self._resolve(symbol, venue, base_spec, start - spec.delta, end, fallback)
        if len(base) == 0:
            return empty_frame()
        out = resample_ohlcv(base, spec.pandas_rule)
        return out.loc[out.index >= start - spec.delta]

    def _fallback(
        self, symbol: str, venue: str, spec: Interval,
        start: pd.Timestamp, end: pd.Timestamp, fallback: bool, reason: str,
    ) -> pd.DataFrame:
        coarser = COARSER_FALLBACK.get(spec.name)
        if not fallback or coarser is None:
            self._log(f"[data] {symbol} {spec.name}: {reason}; no fallback available")
            return empty_frame()
        self._log(f"[data] {symbol}: {reason} -> falling back to {coarser} bars")
        return self._resolve(symbol, venue, interval_of(coarser), start, end, fallback)

    def _cached_or_download(
        self, symbol: str, venue: str, spec: Interval, start: pd.Timestamp, end: pd.Timestamp,
    ) -> pd.DataFrame:
        cached = self.cache.load(venue, symbol, spec.name)
        missing = self._missing_segments(cached, spec, start, end)

        if missing and not self.offline:
            provider = self.providers[venue]
            fetched: list[pd.DataFrame] = []
            for seg_start, seg_end in missing:
                self._log(f"[data] downloading {symbol} {spec.name} {seg_start.date()} -> {seg_end.date()}")
                self._downloads += 1
                part = provider.fetch(symbol, spec, seg_start, seg_end)
                if len(part):
                    fetched.append(part)
            if fetched:
                cached = self.cache.merge(venue, symbol, spec.name, pd.concat(fetched))
        elif missing and self.offline:
            self._log(f"[data] offline: serving {symbol} {spec.name} from cache only "
                      f"({len(missing)} uncached segment(s) skipped)")

        if len(cached) == 0:
            return empty_frame()
        return cached.loc[(cached.index >= start) & (cached.index <= end)]

    @staticmethod
    def _missing_segments(
        cached: pd.DataFrame, spec: Interval, start: pd.Timestamp, end: pd.Timestamp,
    ) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """Sub-windows of [start, end] not already covered by the cache.

        Coverage is tracked as a single [first, last] span rather than
        bar-by-bar on purpose: real series are legitimately full of holes
        (weekends, halts, exchange downtime) and treating every hole as
        "missing" would re-download the whole history on every run forever.
        Only genuine head/tail extensions are fetched.
        """
        if len(cached) == 0:
            return [(start, end)]

        first, last = cached.index[0], cached.index[-1]
        gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        # One bar of tolerance either side: the boundary bar is often the one
        # that was still forming when it was cached.
        if start < first - spec.delta:
            gaps.append((start, min(first, end)))
        if end > last + spec.delta:
            gaps.append((max(last, start), end))
        return gaps
