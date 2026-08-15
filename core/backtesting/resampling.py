"""OHLCV resampling, including the point-in-time variant the replay loop needs.

`resample_ohlcv` is the ordinary aggregation (5m bars -> 15m, 1h -> 4h/daily/
weekly), matching `utils.data_fetch.resample_ohlc` in the live pipeline.

`PointInTimeResampler` is the part that exists specifically to stop lookahead
bias, and it is worth being explicit about the bug it prevents:

    Resampling a full daily history to weekly bars and THEN slicing to
    "as of Wednesday" hands the strategy a weekly bar whose High and Low
    already include Thursday and Friday.

A naive fix -- re-running `resample()` on the sliced frame at every one of the
thousands of timestamps in an intraday replay -- is correct but far too slow.
So the resampler precomputes the aggregation once and, for a given `as_of`,
returns:

    * every higher-timeframe bar that had fully CLOSED by `as_of`, sliced
      straight out of the precomputed frame, plus
    * the bar currently forming, re-aggregated from just the base rows at or
      before `as_of`.

That is exactly what a live caller sees mid-bar (Binance's last kline is the
in-progress one), so backtest and live evaluate the same shaped data, and it
costs one binary search plus an aggregation over the handful of base rows
inside the current bar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.backtesting.cache import OHLCV_COLUMNS, empty_frame

_AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}


def resample_ohlcv(df: pd.DataFrame, rule: str, drop_partial: bool = False) -> pd.DataFrame:
    """Aggregate an OHLCV frame to a coarser bar size.

    Bars are LEFT-labelled and LEFT-closed, i.e. the index is each bar's START
    timestamp -- the same convention Binance klines use (`open_time`) and the
    one `PointInTimeResampler` relies on. Empty bins (weekends, halts) are
    dropped.

    `drop_partial=True` additionally drops a trailing bar whose period has not
    finished within the data, for callers that must never analyse an
    incomplete candle.
    """
    if df is None or len(df) == 0:
        return empty_frame()

    out = df.resample(rule, label="left", closed="left").agg(_AGG)
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    if drop_partial and len(out):
        period = pd.tseries.frequencies.to_offset(rule)
        if df.index[-1] < out.index[-1] + period:
            out = out.iloc[:-1]
    out.index.name = "timestamp"
    return out[OHLCV_COLUMNS]


class PointInTimeResampler:
    """Serve `base_df` aggregated to `rule`, as known at an arbitrary instant.

    Construct once per (symbol, timeframe) and call `as_of(t)` inside the
    replay loop, where `t` is the DECISION instant -- the moment the strategy
    is being asked to act.

    `base_delta` is the length of one base bar and it is what makes `as_of`
    honest. OHLCV frames are indexed by each bar's OPEN time, so a bar stamped
    10:00 on a 1h series describes the market up to 11:00. Selecting rows with
    `index <= t` would therefore hand a strategy deciding at 10:30 an hour bar
    containing the next thirty minutes of price. Rows are instead selected by
    `index <= t - base_delta`, i.e. only bars that had genuinely CLOSED by `t`.

    The consequence is worth stating plainly: this is marginally more
    conservative than the live pipeline, which does see the currently-forming
    bar via Binance/yfinance. Intra-bar context below the base resolution
    simply cannot be reconstructed from the base bars, and under-informing the
    backtest is the safe direction to err in.
    """

    def __init__(self, base_df: pd.DataFrame, rule: str, base_delta: pd.Timedelta | str = "0s") -> None:
        self.rule = rule
        self.base_delta = pd.Timedelta(base_delta)
        base = base_df if base_df is not None else empty_frame()
        self._base = base
        self._empty = len(base) == 0
        # When the target bar size IS the base bar size no aggregation is
        # needed and `as_of` can hand back a plain slice -- which matters,
        # because the trigger timeframe is asked for on every single bar of
        # the replay. `.nanos` raises for non-fixed rules (W-MON, ME), which
        # can never be a passthrough anyway.
        try:
            rule_delta = pd.Timedelta(pd.tseries.frequencies.to_offset(rule).nanos)
        except (ValueError, AttributeError, TypeError):
            rule_delta = None
        self._passthrough = (
            not self._empty
            and rule_delta is not None
            and self.base_delta > pd.Timedelta(0)
            and rule_delta == self.base_delta
        )

        if self._empty:
            self._grid = empty_frame()
            self._closed = empty_frame()
            self._row_bin = np.empty(0, dtype="int64")
            self._base_index = np.empty(0, dtype="datetime64[ns]")
            return

        self._base_index = base.index.to_numpy()
        if self._passthrough:
            self._grid = base
            self._closed = base
            self._row_bin = np.arange(len(base), dtype="int64")
            self._grid_starts = self._base_index
            self._closed_starts = self._base_index
            self._values = {}
            return

        # `_grid` keeps EMPTY bins so that bin positions form a contiguous,
        # searchable axis; `_closed` is the caller-facing version with the
        # empty bins removed.
        self._grid = base.resample(rule, label="left", closed="left").agg(_AGG)
        self._closed = self._grid.dropna(subset=["Open", "High", "Low", "Close"])[OHLCV_COLUMNS]
        self._closed.index.name = "timestamp"

        grid_starts = self._grid.index.to_numpy()
        self._grid_starts = grid_starts
        # Bin position of every base row: non-decreasing, so it can be binary
        # searched to find where the currently-forming bar begins.
        self._row_bin = np.searchsorted(grid_starts, self._base_index, side="right") - 1

        self._closed_starts = self._closed.index.to_numpy()
        self._values = {c: base[c].to_numpy(dtype="float64") for c in OHLCV_COLUMNS}

    def __len__(self) -> int:
        return 0 if self._empty else len(self._base)

    @property
    def closed_bars(self) -> pd.DataFrame:
        """Every fully-aggregated bar over the whole base history. Only safe
        for whole-history work (e.g. deciding a replay calendar) -- never for
        point-in-time strategy input."""
        return self._closed

    def cutoff_position(self, ts) -> int:
        """Number of base bars that had CLOSED by decision instant `ts`."""
        if self._empty:
            return 0
        stamp = np.datetime64(pd.Timestamp(ts).to_datetime64() - self.base_delta.to_timedelta64())
        return int(np.searchsorted(self._base_index, stamp, side="right"))

    def as_of(self, ts) -> pd.DataFrame:
        """Every bar that had closed by `ts`, with the final one reflecting only
        the base data that had actually printed by then."""
        if self._empty:
            return empty_frame()

        i = self.cutoff_position(ts)
        if i == 0:
            return empty_frame()
        if self._passthrough:
            return self._base.iloc[:i]

        bin_pos = int(self._row_bin[i - 1])
        bin_start = self._grid_starts[bin_pos]

        # Everything strictly before the forming bar is already aggregated.
        k = int(np.searchsorted(self._closed_starts, bin_start, side="left"))
        closed = self._closed.iloc[:k]

        j = int(np.searchsorted(self._row_bin, bin_pos, side="left"))
        partial = self._aggregate(j, i, bin_start)
        if partial is None:
            return closed
        return pd.concat([closed, partial])

    def _aggregate(self, lo: int, hi: int, bin_start) -> pd.DataFrame | None:
        if hi <= lo:
            return None
        v = self._values
        row = {
            "Open": v["Open"][lo],
            "High": float(np.nanmax(v["High"][lo:hi])),
            "Low": float(np.nanmin(v["Low"][lo:hi])),
            "Close": v["Close"][hi - 1],
            "Volume": float(np.nansum(v["Volume"][lo:hi])),
        }
        if not np.isfinite(row["Open"]) or not np.isfinite(row["Close"]):
            return None
        idx = pd.DatetimeIndex([pd.Timestamp(bin_start)], name="timestamp")
        return pd.DataFrame([row], index=idx, columns=OHLCV_COLUMNS)


class PointInTimeFrames:
    """A named bundle of `PointInTimeResampler`s -- one per strategy timeframe.

    The models' MTFA code expects a `{timeframe_name: DataFrame}` mapping
    (`ta.mtfa.map_zones_top_down`, `_ma_alignment_vote`, ...). This produces
    exactly that mapping for any instant, so the *unmodified* production
    strategy functions can be driven through history.
    """

    def __init__(self, resamplers: dict[str, PointInTimeResampler]) -> None:
        self._resamplers = resamplers

    def __contains__(self, tf: str) -> bool:
        return tf in self._resamplers

    def get(self, tf: str) -> PointInTimeResampler | None:
        return self._resamplers.get(tf)

    def frames_at(self, ts, only: list[str] | None = None) -> dict[str, pd.DataFrame]:
        names = only if only is not None else list(self._resamplers)
        return {tf: self._resamplers[tf].as_of(ts) for tf in names if tf in self._resamplers}
