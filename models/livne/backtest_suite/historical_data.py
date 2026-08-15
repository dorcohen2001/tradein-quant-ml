"""Historical INTRADAY OHLCV provisioning and point-in-time slicing.

This is the Livne backtest's data layer. It downloads (once), caches to disk,
and then hands the replay loop a `{timeframe: DataFrame}` mapping for any
instant in history -- the exact shape `ta.mtfa`'s production functions expect
from the live fetchers, so the strategy code itself never learns it is being
backtested.

WHAT CHANGED FROM THE DAILY VERSION
-----------------------------------
The previous module fetched one daily bar series per asset and sliced it
day-by-day. The live Livne model is intraday (daily/weekly context, top-down
zone map, 15m trigger with 5m/1h confirmation), so a daily-bar backtest could
only ever approximate it. This module supplies the real ladder:

    weekly / daily / 12h   zone DISCOVERY   -- resampled from 1h base bars
    4h / 3h / 1h           zone REFINEMENT  -- resampled from 1h base bars
    15m                    TRIGGER          -- fetched natively
    5m                     CONFIRMATION     -- fetched natively

Three base series per asset (1h, 15m, 5m) therefore back all eight strategy
timeframes, mirroring `config.TF_INTERVALS`, where a `None` likewise means
"resample this one from 1h".

LOOKAHEAD
---------
Handled by `core.backtesting.PointInTimeResampler`, and it is the whole reason
the frames are built this way rather than by resampling once and slicing. See
that class's docstring: bars are indexed by OPEN time, so selecting `index <=
t` would leak the remainder of the bar in progress. Every frame handed to the
strategy contains only bars that had genuinely CLOSED by the decision instant.

DATA-RANGE LIMITS (read before choosing a window)
-------------------------------------------------
Crypto (Binance) has no history cap -- any window works, it just takes longer
to download the first time.

Stocks (yfinance) cap intraday history hard: ~60 days for 15m/5m and ~730 days
for 1h. A stock backtest asking for 15m signals more than ~55 days back gets
its window clamped (and says so); `core.backtesting.feed` degrades to coarser
bars rather than returning nothing. This is a property of the free data, not
of this code -- validating months of intraday stock history needs a paid feed.
"""
from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field

import pandas as pd

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import (
    LTF_CONFIRM_TFS,
    LTF_TRIGGER_TF,
    PRIMARY_MA_PERIOD,
    ZONE_DISCOVERY_TFS,
    ZONE_REFINE_TFS,
)
from core.backtesting import INTERVALS, IntradayDataFeed, PointInTimeResampler, interval_of
from core.backtesting.resampling import PointInTimeFrames

# Every strategy timeframe the intraday ladder needs, mapped to the base series
# it is built from. Keys match `config.ZONE_DISCOVERY_TFS` / `ZONE_REFINE_TFS`
# / `LTF_TRIGGER_TF` / `LTF_CONFIRM_TFS` so the production MTFA code finds what
# it looks for by name.
TIMEFRAME_BASES: dict[str, str] = {
    "weekly": "1h", "daily": "1h", "12h": "1h",
    "4h": "1h", "3h": "1h", "1h": "1h",
    "15m": "15m", "5m": "5m",
}

# pandas resample rule per strategy timeframe.
TIMEFRAME_RULES: dict[str, str] = {
    "weekly": INTERVALS["1w"].pandas_rule,
    "daily": INTERVALS["1d"].pandas_rule,
    "12h": INTERVALS["12h"].pandas_rule,
    "4h": INTERVALS["4h"].pandas_rule,
    "3h": INTERVALS["3h"].pandas_rule,
    "1h": INTERVALS["1h"].pandas_rule,
    "15m": INTERVALS["15m"].pandas_rule,
    "5m": INTERVALS["5m"].pandas_rule,
}

# Calendar days of history fetched BEFORE the requested start, per base series.
#
# 1h is the binding one: the daily and weekly context frames are resampled
# from it, and `ta_engine.determine_trend` needs EMA150/EMA200 on daily to be
# meaningful (~200 daily bars) while `map_zones_top_down` needs at least
# MIN_ZONE_HISTORY_BARS weekly bars. 400 calendar days satisfies both and
# still sits inside yfinance's 730-day 1h ceiling.
#
# 15m/5m only have to cover the 200-period EMA alignment vote plus the swing/
# pivot lookbacks: 210 bars of 15m is ~2.2 days of crypto or ~8 stock sessions.
# 20 calendar days is generous for both and keeps the download small.
WARMUP_DAYS: dict[str, int] = {"1h": 400, "15m": 20, "5m": 20}

# Calendar days fetched AFTER the requested end, so a signal generated on the
# last bar of the window still has forward bars to be simulated against.
# Trades have no fixed holding-period cutoff (see simulator.py); one still
# unresolved when this buffer runs out is reported OPEN, not force-closed.
FORWARD_DAYS = 30


@dataclass
class AssetData:
    """Everything the replay loop needs for one asset.

    `frames` produces the point-in-time `{timeframe: DataFrame}` mapping for
    the strategy; `trigger` and `execution` are plain frames the loop and the
    simulator walk bar by bar.
    """
    ticker: str
    asset_class: str
    trigger_interval: str
    exec_interval: str
    trigger: pd.DataFrame                  # signal-generation bars (indexed by OPEN time)
    execution: pd.DataFrame                # bars the open position is managed on
    frames: PointInTimeFrames
    daily: PointInTimeResampler            # liquidity / relative-strength context
    bases: dict[str, pd.DataFrame] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return len(self.trigger) > 0 and len(self.execution) > 0


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def required_timeframes() -> list[str]:
    """The strategy timeframes actually referenced by config, de-duplicated."""
    names = list(ZONE_DISCOVERY_TFS) + list(ZONE_REFINE_TFS) + [LTF_TRIGGER_TF] + list(LTF_CONFIRM_TFS)
    seen: list[str] = []
    for n in names:
        if n not in seen and n in TIMEFRAME_BASES:
            seen.append(n)
    return seen


def _base_window(base: str, start: dt.date, end: dt.date) -> tuple[pd.Timestamp, pd.Timestamp]:
    warmup = WARMUP_DAYS.get(base, 60)
    return (
        pd.Timestamp(start) - pd.Timedelta(days=warmup),
        pd.Timestamp(end) + pd.Timedelta(days=FORWARD_DAYS),
    )


def build_asset_data(
    feed: IntradayDataFeed,
    ticker: str,
    asset_class: str,
    start: dt.date,
    end: dt.date,
    trigger_interval: str = LTF_TRIGGER_TF,
    exec_interval: str | None = None,
) -> AssetData | None:
    """Fetch/cache every base series for `ticker` and wrap them in point-in-time
    resamplers. Returns None when the asset has no usable intraday history --
    a delisted ticker or an unlisted pair must skip, not abort the run."""
    exec_interval = exec_interval or trigger_interval
    trigger_spec = interval_of(trigger_interval)
    exec_spec = interval_of(exec_interval)

    bases: dict[str, pd.DataFrame] = {}
    needed = set(TIMEFRAME_BASES[tf] for tf in required_timeframes())
    needed.add(trigger_spec.name)
    needed.add(exec_spec.name)

    for base in sorted(needed):
        win_start, win_end = _base_window(base, start, end)
        bases[base] = feed.get_candles(ticker, asset_class, base, win_start, win_end)

    trigger = bases.get(trigger_spec.name)
    execution = bases.get(exec_spec.name)
    if trigger is None or len(trigger) == 0 or execution is None or len(execution) == 0:
        return None

    resamplers: dict[str, PointInTimeResampler] = {}
    for tf in required_timeframes():
        base_name = TIMEFRAME_BASES[tf]
        base_df = bases.get(base_name)
        if base_df is None or len(base_df) == 0:
            continue
        resamplers[tf] = PointInTimeResampler(
            base_df, TIMEFRAME_RULES[tf], base_delta=interval_of(base_name).delta,
        )

    if not resamplers:
        return None

    # Daily is also used outside the MTFA ladder -- for the dollar-volume
    # liquidity floor and the relative-strength comparison, both of which the
    # live screener computes on daily bars.
    daily = resamplers.get("daily") or PointInTimeResampler(
        bases["1h"], TIMEFRAME_RULES["daily"], base_delta=interval_of("1h").delta,
    )

    return AssetData(
        ticker=ticker, asset_class=asset_class,
        trigger_interval=trigger_spec.name, exec_interval=exec_spec.name,
        trigger=trigger, execution=execution,
        frames=PointInTimeFrames(resamplers), daily=daily, bases=bases,
    )


def build_benchmark(
    feed: IntradayDataFeed, ticker: str, asset_class: str, start: dt.date, end: dt.date,
) -> PointInTimeResampler:
    """Daily point-in-time series for a macro benchmark (SPY/QQQ/^VIX, BTC/ETH).

    Built from 1h bars rather than daily ones so that "as of 14:30 on the 6th"
    means the 5th's completed daily bar plus however much of the 6th had
    actually traded -- not the 6th's full, not-yet-finished bar.
    """
    win_start, win_end = _base_window("1h", start, end)
    base = feed.get_candles(ticker, asset_class, "1h", win_start, win_end)
    if len(base) == 0:
        # ^VIX and some ETFs occasionally serve no intraday history at all.
        # A daily series is still enough for a regime read; it just cannot
        # reflect the current, incomplete session.
        base = feed.get_candles(ticker, asset_class, "1d", win_start, win_end)
        return PointInTimeResampler(base, TIMEFRAME_RULES["daily"], base_delta=interval_of("1d").delta)
    return PointInTimeResampler(base, TIMEFRAME_RULES["daily"], base_delta=interval_of("1h").delta)


def signal_bars(df: pd.DataFrame, start: dt.date, end: dt.date) -> pd.DatetimeIndex:
    """Bars of `df` whose OPEN time falls in [start, end] -- the replay calendar.

    Each of these is evaluated at its CLOSE (see `decision_time`), which is the
    first instant a strategy could actually have acted on it.
    """
    if len(df) == 0:
        return pd.DatetimeIndex([])
    lo, hi = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(days=1)
    return df.index[(df.index >= lo) & (df.index < hi)]


def decision_time(bar_open: pd.Timestamp, interval: str) -> pd.Timestamp:
    """The instant a signal on the bar opening at `bar_open` becomes actionable:
    that bar's close. Everything downstream -- the strategy's data cut, the
    entry timestamp, where forward simulation starts -- keys off this."""
    return pd.Timestamp(bar_open) + interval_of(interval).delta


def forward_bars(df: pd.DataFrame, after: pd.Timestamp) -> pd.DataFrame:
    """Bars opening at or after `after` -- the only data the simulator may look
    at when grading a trade entered at `after`."""
    return df.loc[df.index >= pd.Timestamp(after)]


def enough_history(data: AssetData, at: pd.Timestamp) -> bool:
    """Whether the 200-period MA on the trigger frame is computable yet at `at`.

    Cheap pre-filter for the very start of a window, where the warmup buffer
    may not have covered every asset (a recently-listed pair, a ticker with a
    gap in Yahoo's intraday history).
    """
    trigger_pit = data.frames.get(data.trigger_interval)
    if trigger_pit is None:
        return False
    return trigger_pit.cutoff_position(at) >= PRIMARY_MA_PERIOD + 10


def describe_coverage(data: AssetData) -> str:
    parts = []
    for name, df in sorted(data.bases.items()):
        if len(df):
            parts.append(f"{name}:{len(df)} bars {df.index[0]:%Y-%m-%d}->{df.index[-1]:%Y-%m-%d}")
        else:
            parts.append(f"{name}:EMPTY")
    return f"{data.ticker} | " + "  ".join(parts)
