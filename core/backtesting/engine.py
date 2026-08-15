"""Intrabar execution primitives shared by both models' simulators.

The one idea this module exists for: on an intraday backtest a stop or target
is triggered by the candle's HIGH/LOW, not its Close. A 15m bar that opens at
100, wicks to 97 and closes at 101 has hit a stop at 98 -- a close-only
simulator would call that bar a non-event and report a win that never
happened.

What is NOT solvable from a single OHLC bar is the ORDER of events inside it.
When one bar's range covers both the stop and a target, the bar alone cannot
say which came first. Two policies are offered:

  * Default -- resolve conservatively in favour of the STOP. Assuming the
    adverse outcome is the only choice that cannot flatter the win rate.
  * `FinerBarTieBreaker` -- if finer candles (say 1m) are available for that
    same window, replay them inside the ambiguous bar and use whichever level
    was actually touched first. This is a real answer rather than an
    assumption, and it is opt-in because it needs a second, much larger
    dataset.

Position lifecycle (TP1 partials, breakeven moves, reversal exits) stays in
each model's own simulator, because the two models genuinely differ there.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


def pnl_pct(direction: str, entry_price: float, exit_price: float) -> float:
    """Percentage P&L of a closed leg. SHORT is quoted so that a fall in price
    is a gain, consistent with both models' live trackers."""
    if entry_price <= 0:
        return 0.0
    if direction == "LONG":
        return (exit_price / entry_price - 1) * 100
    return (entry_price / exit_price - 1) * 100 if exit_price > 0 else 0.0


def pnl_points(direction: str, entry_price: float, exit_price: float) -> float:
    """Absolute price movement captured, signed by direction ("points"/$ per unit)."""
    return (exit_price - entry_price) if direction == "LONG" else (entry_price - exit_price)


def apply_slippage(price: float, direction: str, pct: float, adverse: bool = True) -> float:
    """Model a stop becoming a market order: the fill lands `pct` WORSE than the
    trigger level for the position's direction."""
    sign = -1 if (direction == "LONG") == adverse else 1
    return price * (1 + sign * pct)


def hits(row, direction: str, level: float, side: str) -> bool:
    """Did this candle's range reach `level`?

    `side` is "stop" or "target": a LONG's stop sits below entry (touched by
    the bar's Low) while its targets sit above (touched by the High); SHORT is
    the mirror. Everything is evaluated on High/Low -- never on Close.
    """
    high, low = float(row["High"]), float(row["Low"])
    above = (direction == "LONG") == (side == "target")
    return high >= level if above else low <= level


def duration_breakdown(entry_ts, exit_ts) -> dict[str, float]:
    """Holding period expressed the three ways the trade log reports it."""
    if entry_ts is None or exit_ts is None:
        return {"minutes": 0.0, "hours": 0.0, "days": 0.0}
    delta = pd.Timestamp(exit_ts) - pd.Timestamp(entry_ts)
    minutes = delta.total_seconds() / 60.0
    return {
        "minutes": round(minutes, 1),
        "hours": round(minutes / 60.0, 2),
        "days": round(minutes / 1440.0, 3),
    }


@dataclass
class ExcursionTracker:
    """Running maximum favourable/adverse excursion, with the timestamps.

    This is what makes "precise intraday timestamps for drawdowns" real at the
    trade level: MAE is the worst unrealised point the position ever reached
    and WHEN, which on intraday data is usually a wick nobody would ever see
    in a daily-bar backtest.
    """
    direction: str
    entry_price: float
    mae_pct: float = 0.0
    mfe_pct: float = 0.0
    mae_time: pd.Timestamp | None = None
    mfe_time: pd.Timestamp | None = None

    def update(self, ts, row) -> None:
        high, low = float(row["High"]), float(row["Low"])
        best, worst = (high, low) if self.direction == "LONG" else (low, high)
        best_pct = pnl_pct(self.direction, self.entry_price, best)
        worst_pct = pnl_pct(self.direction, self.entry_price, worst)
        if best_pct > self.mfe_pct:
            self.mfe_pct, self.mfe_time = best_pct, ts
        if worst_pct < self.mae_pct:
            self.mae_pct, self.mae_time = worst_pct, ts


@dataclass
class FinerBarTieBreaker:
    """Resolves same-bar stop/target ambiguity using finer candles.

    `loader(symbol, asset_class, start, end)` must return an OHLCV frame at a
    finer resolution than the simulation bar. When one bar's range covers both
    a stop and a target, the finer bars inside that bar are walked in order and
    the FIRST level actually touched wins.

    If the finer data is unavailable for that window (very common for stocks:
    yfinance serves 1m for only ~30 days) the tie-breaker reports "unknown" and
    the caller falls back to the conservative stop-first rule.
    """
    loader: object
    interval_delta: pd.Timedelta
    resolved: int = 0
    unresolved: int = 0
    _cache: dict = field(default_factory=dict, repr=False)

    def first_touch(
        self, symbol: str, asset_class: str, bar_ts, direction: str,
        stop: float, target: float,
    ) -> str | None:
        """"stop", "target", or None when the finer data can't say."""
        start = pd.Timestamp(bar_ts)
        end = start + self.interval_delta
        key = (symbol, start)
        fine = self._cache.get(key)
        if fine is None:
            try:
                fine = self.loader(symbol, asset_class, start, end)
            except Exception:
                fine = None
            self._cache[key] = fine if fine is not None else False
        if fine is None or fine is False or len(fine) == 0:
            self.unresolved += 1
            return None

        window = fine.loc[(fine.index >= start) & (fine.index < end)]
        for _ts, row in window.iterrows():
            stop_hit = hits(row, direction, stop, "stop")
            target_hit = hits(row, direction, target, "target")
            if stop_hit and target_hit:
                continue  # still ambiguous at this resolution -- look at the next finer bar
            if stop_hit:
                self.resolved += 1
                return "stop"
            if target_hit:
                self.resolved += 1
                return "target"
        self.unresolved += 1
        return None


def resolve_same_bar(
    tie_breaker: FinerBarTieBreaker | None, symbol: str, asset_class: str,
    bar_ts, direction: str, stop: float, target: float,
) -> str:
    """Which of a stop and a target that BOTH sit inside one bar's range
    triggered first. Falls back to "stop" -- the conservative assumption --
    whenever no tie-breaker is configured or the finer data cannot decide."""
    if tie_breaker is None:
        return "stop"
    verdict = tie_breaker.first_touch(symbol, asset_class, bar_ts, direction, stop, target)
    return verdict or "stop"
