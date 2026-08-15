"""Candle-interval registry: one place that knows, per interval, what each
venue calls it, whether the venue serves it natively at all, and how far back
that venue will actually go.

Two facts drive almost every awkward branch in the fetch layer, so they are
declared here once rather than rediscovered at each call site:

  1. Neither venue serves every interval. Binance has no 3h; yfinance has no
     sub-daily bar above 90m at all (no 2h/3h/4h/6h/8h/12h). Anything missing
     is built by resampling the venue's 1h bars -- exactly what the LIVE
     pipeline already does (`utils.data_fetch.resample_ohlc` / the `None`
     entries in `config.TF_INTERVALS`), so backtest and live agree on what a
     "4h stock bar" even means.

  2. yfinance hard-caps intraday history by interval, and the cap is the single
     biggest constraint on how long a stock backtest window can be:

         1m   ~30 days of history, and only 7 days per request
         2m/5m/15m/30m/90m   ~60 days
         1h                  ~730 days
         1d and coarser      effectively unlimited

     Binance imposes no equivalent lookback cap -- only 1000 candles per
     request, which `providers.BinanceKlineProvider` paginates through.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Interval:
    """One supported bar size and how each venue exposes it.

    `binance`/`yahoo` are the venue's own interval string, or None when the
    venue does not serve this bar size natively -- in which case the feed
    resamples it from `resample_base`.
    """
    name: str
    pandas_rule: str                    # pandas offset alias, for resample()
    binance: str | None
    yahoo: str | None
    resample_base: str | None           # interval name to build this one from
    yahoo_max_lookback_days: int | None  # how far back yfinance serves this interval
    yahoo_max_request_days: int | None   # max span of a SINGLE yfinance request
    span: str                           # bar length as a Timedelta string

    @property
    def delta(self) -> pd.Timedelta:
        """Bar length. Kept separate from `pandas_rule` because an anchored
        alias like "W-MON" is a valid resample rule but not a valid Timedelta."""
        return pd.Timedelta(self.span)

    @property
    def is_intraday(self) -> bool:
        return self.delta < pd.Timedelta("1D")

    def venue_interval(self, venue: str) -> str | None:
        return self.binance if venue == "binance" else self.yahoo


def _mk(name, rule, binance, yahoo, base=None, yf_lookback=None, yf_request=None, span=None) -> Interval:
    return Interval(name, rule, binance, yahoo, base, yf_lookback, yf_request, span or rule)


# Weekly resamples with an explicit W-MON anchor so a "week" runs Monday ->
# Sunday, matching Binance's `1w` klines and yfinance's `1wk` bars. pandas'
# unanchored "W" defaults to W-SUN, which would silently shift every weekly
# candle by a day relative to what the live pipeline sees.
#
# `1w`/`1d` carry no yfinance lookback cap; the intraday rows carry the real ones.
INTERVALS: dict[str, Interval] = {
    "1m":  _mk("1m",  "1min",  "1m",  "1m",  None,  yf_lookback=30,  yf_request=7),
    "3m":  _mk("3m",  "3min",  "3m",  None,  "1m",  yf_lookback=30,  yf_request=7),
    "5m":  _mk("5m",  "5min",  "5m",  "5m",  None,  yf_lookback=60,  yf_request=60),
    "15m": _mk("15m", "15min", "15m", "15m", "5m",  yf_lookback=60,  yf_request=60),
    "30m": _mk("30m", "30min", "30m", "30m", "15m", yf_lookback=60,  yf_request=60),
    "1h":  _mk("1h",  "1h",    "1h",  "1h",  None,  yf_lookback=730, yf_request=730),
    "2h":  _mk("2h",  "2h",    "2h",  None,  "1h"),
    "3h":  _mk("3h",  "3h",    None,  None,  "1h"),
    "4h":  _mk("4h",  "4h",    "4h",  None,  "1h"),
    "6h":  _mk("6h",  "6h",    "6h",  None,  "1h"),
    "8h":  _mk("8h",  "8h",    "8h",  None,  "1h"),
    "12h": _mk("12h", "12h",   "12h", None,  "1h"),
    "1d":  _mk("1d",  "1D",    "1d",  "1d",  None),
    "1w":  _mk("1w",  "W-MON", "1w",  "1wk", "1d",  span="7D"),
}

# Aliases so callers can use either the model's config vocabulary
# (`config.TF_INTERVALS` / `ZONE_DISCOVERY_TFS` say "daily"/"weekly"/"60m")
# or the venue's.
_ALIASES = {
    "1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m",
    "60m": "1h", "60min": "1h", "hourly": "1h",
    "1hr": "1h", "4hr": "4h",
    "daily": "1d", "1day": "1d", "d": "1d",
    "weekly": "1w", "1wk": "1w", "w": "1w",
}

# When a venue cannot serve an interval at all for the requested window (a
# yfinance 5m request older than 60 days), the feed degrades to the next
# coarser interval rather than silently returning nothing.
COARSER_FALLBACK: dict[str, str] = {
    "1m": "5m", "3m": "5m", "5m": "15m", "15m": "1h", "30m": "1h",
    "1h": "1d", "2h": "1d", "3h": "1d", "4h": "1d", "6h": "1d",
    "8h": "1d", "12h": "1d", "1d": "1w",
}


def interval_of(name: str) -> Interval:
    """Look up an `Interval` by name, accepting the aliases above.

    Raises KeyError (not a silent default) on an unknown interval -- a typo
    like "10m" must fail loudly rather than quietly backtest a different bar
    size than the caller asked for.
    """
    key = str(name).strip().lower()
    key = _ALIASES.get(key, key)
    if key not in INTERVALS:
        raise KeyError(f"unsupported interval {name!r}; supported: {', '.join(INTERVALS)}")
    return INTERVALS[key]


def venue_for(asset_class: str) -> str:
    return "binance" if asset_class == "crypto" else "yahoo"
