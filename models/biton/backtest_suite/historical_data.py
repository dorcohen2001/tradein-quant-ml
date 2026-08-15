"""Historical INTRADAY OHLCV provisioning and point-in-time slicing (Biton).

Biton is the frozen legacy pipeline: a single-timeframe model. `ta_engine.
analyze` takes ONE OHLCV frame and derives everything (EMA20/50/200, the
Fibonacci golden zone, pivot support/resistance, RSI, MACD, volume spike) from
it, and `generator.trade_generator._build_trade_params` builds entry/stop/
targets from that same result.

So the intraday upgrade here is narrower than Livne's, and deliberately so --
Biton's rules are untouched. What changes is the BAR SIZE those rules see: the
analysis frame is now any interval (1h by default, 15m/5m/4h/1d all valid)
instead of being hard-wired to daily candles, and stops/targets are graded
against intraday highs and lows.

Two frames are maintained per asset:

    analysis   the interval the strategy runs on (`--interval`)
    daily      the liquidity floor and the relative-strength comparison, which
               Biton's live screener computes on DAILY bars and which stay
               there so the funnel keeps the same meaning at any bar size

Lookahead is prevented by `core.backtesting.PointInTimeResampler`, which
selects bars by their CLOSE rather than their open -- see that class for why
naive slicing leaks the rest of the bar in progress.

yfinance's intraday history caps (~60 days at 15m/5m, ~730 days at 1h) apply
to stock windows exactly as they do for Livne; `core.backtesting.feed` clamps
and, if the whole window predates a cap, falls back to coarser bars.
"""
from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass

import pandas as pd

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from core.backtesting import INTERVALS, IntradayDataFeed, PointInTimeResampler, interval_of

# Bars of analysis-interval history needed before the first signal can be
# evaluated: EMA200 plus `pivot_levels`' 120-bar lookback plus slack. Biton's
# `analyze` degrades silently (UNKNOWN trend, empty pivots) rather than
# erroring on short frames, so this floor is what actually keeps the early
# part of a window from producing junk signals.
MIN_ANALYSIS_BARS = 260

# Calendar-day buffer AFTER the requested end, so a signal on the last bar of
# the window still has forward bars to be simulated against.
FORWARD_DAYS = 30

# Daily context (liquidity + relative strength + the macro regime) is
# resampled from 1h bars, and `ta_engine.determine_trend` needs EMA50/EMA200
# on daily to be meaningful. 400 calendar days covers that and stays inside
# yfinance's 730-day 1h ceiling.
DAILY_BASE_INTERVAL = "1h"
DAILY_WARMUP_DAYS = 400
DAILY_RULE = INTERVALS["1d"].pandas_rule


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def warmup_days(interval: str, bars: int = MIN_ANALYSIS_BARS) -> int:
    """Calendar days of history to fetch before `start` to guarantee `bars` of
    the analysis interval are available on day one.

    Sized against a ~6-hour trading day, which is the pessimistic case: a US
    equity session yields ~6.5 hours of intraday bars per calendar day while
    crypto yields 24, so this over-fetches for crypto (cheap, and cached) and
    is about right for stocks.
    """
    delta = interval_of(interval).delta
    needed = bars * delta / pd.Timedelta(hours=6)
    return int(max(20, min(needed + 5, 800)))


@dataclass
class AssetData:
    """Per-asset frames for one replay pass."""
    ticker: str
    asset_class: str
    interval: str
    exec_interval: str
    analysis: pd.DataFrame            # signal-generation bars (indexed by OPEN time)
    execution: pd.DataFrame           # bars an open position is managed on
    analysis_pit: PointInTimeResampler
    daily: PointInTimeResampler

    @property
    def usable(self) -> bool:
        return len(self.analysis) > 0 and len(self.execution) > 0


def build_asset_data(
    feed: IntradayDataFeed,
    ticker: str,
    asset_class: str,
    start: dt.date,
    end: dt.date,
    interval: str = "1h",
    exec_interval: str | None = None,
) -> AssetData | None:
    """Fetch/cache the analysis, execution and daily-context series for `ticker`.

    Returns None when the asset has no usable history -- a delisted ticker or
    an unlisted pair must skip, not abort the run.
    """
    exec_interval = exec_interval or interval
    spec = interval_of(interval)
    exec_spec = interval_of(exec_interval)
    fetch_end = pd.Timestamp(end) + pd.Timedelta(days=FORWARD_DAYS)

    analysis = feed.get_candles(
        ticker, asset_class, spec.name,
        pd.Timestamp(start) - pd.Timedelta(days=warmup_days(spec.name)), fetch_end,
    )
    if len(analysis) == 0:
        return None

    if exec_spec.name == spec.name:
        execution = analysis
    else:
        execution = feed.get_candles(
            ticker, asset_class, exec_spec.name,
            pd.Timestamp(start) - pd.Timedelta(days=warmup_days(exec_spec.name)), fetch_end,
        )
    if len(execution) == 0:
        return None

    daily_base = feed.get_candles(
        ticker, asset_class, DAILY_BASE_INTERVAL,
        pd.Timestamp(start) - pd.Timedelta(days=DAILY_WARMUP_DAYS), fetch_end,
    )
    if len(daily_base) == 0:
        return None

    return AssetData(
        ticker=ticker, asset_class=asset_class,
        interval=spec.name, exec_interval=exec_spec.name,
        analysis=analysis, execution=execution,
        analysis_pit=PointInTimeResampler(analysis, spec.pandas_rule, base_delta=spec.delta),
        daily=PointInTimeResampler(daily_base, DAILY_RULE, base_delta=interval_of(DAILY_BASE_INTERVAL).delta),
    )


def build_benchmark(
    feed: IntradayDataFeed, ticker: str, asset_class: str, start: dt.date, end: dt.date,
) -> PointInTimeResampler:
    """Daily point-in-time series for a macro benchmark (SPY/QQQ/^VIX, BTC/ETH),
    built from 1h bars so "as of 14:30" means yesterday's completed daily bar
    plus however much of today had actually traded."""
    win_start = pd.Timestamp(start) - pd.Timedelta(days=DAILY_WARMUP_DAYS)
    win_end = pd.Timestamp(end) + pd.Timedelta(days=FORWARD_DAYS)
    base = feed.get_candles(ticker, asset_class, DAILY_BASE_INTERVAL, win_start, win_end)
    if len(base) == 0:
        base = feed.get_candles(ticker, asset_class, "1d", win_start, win_end)
        return PointInTimeResampler(base, DAILY_RULE, base_delta=interval_of("1d").delta)
    return PointInTimeResampler(base, DAILY_RULE, base_delta=interval_of(DAILY_BASE_INTERVAL).delta)


def signal_bars(df: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DatetimeIndex:
    """Bars whose OPEN time falls in [start, end] -- the replay calendar. Each
    is evaluated at its CLOSE (`decision_time`), the first instant a strategy
    could have acted on it."""
    if len(df) == 0:
        return pd.DatetimeIndex([])
    lo, hi = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
    return df.index[(df.index >= lo) & (df.index < hi)]


def decision_time(bar_open: pd.Timestamp, interval: str) -> pd.Timestamp:
    return pd.Timestamp(bar_open) + interval_of(interval).delta


def forward_bars(df: pd.DataFrame, after: pd.Timestamp) -> pd.DataFrame:
    """Bars opening at or after `after` -- the only data the simulator may see."""
    return df.loc[df.index >= pd.Timestamp(after)]
