"""Trade execution logic & outcome tracker.

Given a signal (direction, entry zone, stop, target1, target2) generated on a
historical date T, walks forward through the real subsequent daily bars and
grades the trade per the same multi-tier TP1(50%)/breakeven/TP2 lifecycle the
live paper tracker (`paper_trading.tracker`) uses:

  - ACTIVE: LONG WINs the TP1 leg if High >= Target1 before Low <= Stop (SHORT
    mirrored). Hitting Stop before Target1 is a full-size LOSS ("SL").
  - Once TP1 triggers, `TP1_CLOSE_PCT` of the position is booked at Target1
    and the stop for the remainder moves to breakeven (with the same
    slippage buffer used live). State becomes PARTIAL_TP1.
  - PARTIAL_TP1: the remaining size exits either at the breakeven stop
    ("TP1_SL_BE") or at Target2 ("TP1_TP2"). P&L is the size-weighted blend
    of both legs.
  - On any bar where neither the stop nor a target was hit, a *dynamic*
    trend-reversal exit is checked: an opposing candlestick pattern, a fresh
    break of the recent swing extreme, or a MACD histogram cross -- combined
    with abnormal volume vs. its N-day average -- closes the (remaining)
    position at that bar's close ("TREND_REVERSAL").

There is no fixed-duration forced closure. A trade stays open, bar by bar,
for as long as historical data exists and none of the above conditions have
fired -- previous versions of this module force-closed any unresolved trade
after a fixed `max_days` (originally 10), which is gone. What replaces it is
NOT "the trade waits forever": a finite backtest dataset has a real edge (the
requested `end` date plus `historical_data.FORWARD_CALENDAR_DAYS` of
subsequent bars, itself bounded by how much history actually exists). A trade
that is still unresolved when that edge is reached is reported as **OPEN**
(tag `END_OF_DATA`) -- an honest "don't know yet," not a fabricated win or
loss. `TradeOutcome.is_resolved` distinguishes OPEN from an actual exit;
`is_profitable` (P&L sign) is only meaningful for resolved trades, and
`run_backtest.print_report` excludes OPEN trades from win-rate/profit-factor
math for exactly that reason -- an unrealized mark-to-last-close is not a
realized result.

Same-day ambiguity (a single daily bar's range covers both the stop and a
target) is resolved conservatively in favor of the stop -- daily OHLCV has no
intrabar sequencing, so assuming the adverse outcome avoids overstating the
win rate. The dynamic-exit check only runs when neither the stop nor a target
was hit that same bar, so it never overrides a harder price-level event.

Known scope boundary: this backtest only has historical daily OHLCV. It
cannot replicate the live/paper-trading-only liquidity-heatmap bias or the
intraday emergency-reversal exit (both need data this dataset doesn't have:
historical order-book snapshots and intraday volume-anomaly candles). The
dynamic exit above is a *daily-bar* analogue of that live mechanism, using
the same data this backtest already has.

Known simplification: the trade is assumed FILLED at the entry-zone midpoint
on the signal date — the simulator never requires price to actually touch the
entry zone first. Because entry zones sit within ~0.5% of the signal price
this is usually harmless, but it slightly flatters results vs. the live paper
tracker (which requires a real entry-zone touch before a trade goes ACTIVE).
Keep this in mind when comparing backtest and live win rates.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from backtest_suite.historical_data import forward_window
from config import (
    DYNAMIC_EXIT_CONTEXT_BARS,
    DYNAMIC_EXIT_VOLUME_LOOKBACK,
    DYNAMIC_EXIT_VOLUME_MULT,
    MAX_SLIPPAGE_PCT,
    TP1_CLOSE_PCT,
)
from ta.mtfa import detect_braking_event
from ta.ta_engine import macd, volume_spike


def _opposite(direction: str) -> str:
    return "SHORT" if direction == "LONG" else "LONG"


def _pnl_pct(direction: str, entry_price: float, exit_price: float) -> float:
    if direction == "LONG":
        return (exit_price / entry_price - 1) * 100
    return (entry_price / exit_price - 1) * 100


def _trend_reversal_signal(window: pd.DataFrame, direction: str) -> bool:
    """True if `window` shows a braking/reversal event AGAINST the held
    `direction`. Reuses the exact same multi-condition, multi-bar detector
    Stage 7 uses for entry confirmation (`ta.mtfa.detect_braking_event`) --
    called with the OPPOSITE of the held direction, since a bearish event is
    what closes a LONG (and vice versa). A MACD histogram cross is kept as an
    independent additional condition on top of the four braking-event
    signatures (pin-bar rejection, classic pattern, volume climax, MSS)."""
    if len(window) < 2:
        return False

    braking_hit, _pattern = detect_braking_event(window, _opposite(direction))

    macd_hit = False
    if len(window) >= 35:  # MACD(12,26,9) needs ~35 bars before the histogram is meaningful
        hist = macd(window["Close"])["hist"]
        prev_hist, curr_hist = hist.iloc[-2], hist.iloc[-1]
        if not (pd.isna(prev_hist) or pd.isna(curr_hist)):
            macd_hit = (prev_hist >= 0 and curr_hist < 0) if direction == "LONG" else (prev_hist <= 0 and curr_hist > 0)

    return braking_hit or macd_hit


def _dynamic_exit_signal(window: pd.DataFrame, direction: str) -> tuple[bool, float]:
    """Trend-reversal signal combined with an abnormal-volume bar (spec: 1.5x
    - 2.0x the N-day average). `window` should already be trimmed to a bounded
    trailing context (see `DYNAMIC_EXIT_CONTEXT_BARS`) -- this is re-evaluated
    on every open bar of a trade that may now run for months, so an unbounded
    from-inception window would make a single trade's simulation cost grow
    quadratically with how long it stays open."""
    if not _trend_reversal_signal(window, direction):
        return False, 0.0
    vol_ok, ratio = volume_spike(window, mult=DYNAMIC_EXIT_VOLUME_MULT, lookback=DYNAMIC_EXIT_VOLUME_LOOKBACK)
    return vol_ok, ratio


@dataclass
class TradeOutcome:
    ticker: str
    asset_class: str
    direction: str
    signal_date: pd.Timestamp
    entry_low: float
    entry_high: float
    entry_price: float
    stop: float           # stop ACTIVE at exit time (breakeven-adjusted once TP1 hits -- see `initial_stop`)
    initial_stop: float   # the original structural stop at signal time, never mutated
    target1: float
    target2: float
    exit_date: pd.Timestamp
    exit_price: float
    outcome: str   # WIN / LOSS / OPEN
    tag: str       # SL / TP1_SL_BE / TP1_TP2 / TREND_REVERSAL / END_OF_DATA
    tp1_hit: bool
    pnl_pct: float
    days_held: int
    score: float

    def combined_outcome(self) -> str:
        """Single-token label for logs/CSV: WIN_TP1_TP2, WIN_TP1_SL_BE,
        WIN_TREND_REVERSAL, LOSS_SL, or OPEN_END_OF_DATA."""
        return f"{self.outcome}_{self.tag}"

    @property
    def is_resolved(self) -> bool:
        """False for OPEN/END_OF_DATA -- the position was still live when the
        backtest ran out of historical data, so there's no realized result
        yet. `pnl_pct` on an unresolved trade is an unrealized mark-to-last-
        close, not a completed trade's P&L."""
        return self.outcome in ("WIN", "LOSS")

    @property
    def is_profitable(self) -> bool:
        """Whether this trade should count as a "win" for performance
        reporting, regardless of *how* it exited -- a trade that exits on a
        trend-reversal signal (or hits breakeven after TP1) with positive
        P&L is still a win. Only meaningful for resolved trades; callers
        should filter on `is_resolved` first (see `run_backtest.print_report`)."""
        return self.pnl_pct > 0


def simulate_trade(
    full_df: pd.DataFrame,
    ticker: str,
    asset_class: str,
    direction: str,
    signal_date: pd.Timestamp,
    entry_low: float,
    entry_high: float,
    stop: float,
    target1: float,
    target2: float,
    score: float,
    enable_dynamic_exit: bool = True,
) -> TradeOutcome:
    entry_price = (entry_low + entry_high) / 2
    initial_stop = stop
    breakeven_price = entry_price * (1 + MAX_SLIPPAGE_PCT) if direction == "LONG" else entry_price * (1 - MAX_SLIPPAGE_PCT)
    future = forward_window(full_df, signal_date)  # unbounded: every available bar after signal_date

    state = "ACTIVE"
    tp1_exit_price: float | None = None
    outcome = tag = exit_price = exit_date = None
    pnl_pct = None

    for ts, row in future.iterrows():
        hi, lo = float(row["High"]), float(row["Low"])

        if state == "ACTIVE":
            if direction == "LONG":
                hit_stop, hit_t1, hit_t2 = lo <= stop, hi >= target1, hi >= target2
            else:
                hit_stop, hit_t1, hit_t2 = hi >= stop, lo <= target1, lo <= target2

            if hit_stop:
                # Same-day ambiguity (the daily range covers both the stop
                # and a target) is resolved conservatively in favor of the
                # stop, regardless of what else the range also touched.
                fill = stop * (1 - MAX_SLIPPAGE_PCT) if direction == "LONG" else stop * (1 + MAX_SLIPPAGE_PCT)
                outcome, tag, exit_price, exit_date = "LOSS", "SL", fill, ts
                pnl_pct = _pnl_pct(direction, entry_price, fill)
                break
            if hit_t2:
                # Gapped through both targets in one bar -- credit both legs.
                pnl1 = _pnl_pct(direction, entry_price, target1)
                pnl2 = _pnl_pct(direction, entry_price, target2)
                pnl_pct = TP1_CLOSE_PCT * pnl1 + (1 - TP1_CLOSE_PCT) * pnl2
                outcome, tag, exit_price, exit_date = "WIN", "TP1_TP2", target2, ts
                break
            if hit_t1:
                tp1_exit_price = target1
                state = "PARTIAL_TP1"
                stop = breakeven_price  # Breakeven Adjustment
                continue

            if enable_dynamic_exit:
                window = full_df.loc[:ts].tail(DYNAMIC_EXIT_CONTEXT_BARS)
                reversal, _ratio = _dynamic_exit_signal(window, direction)
                if reversal:
                    exit_p = float(row["Close"])
                    pnl_pct = _pnl_pct(direction, entry_price, exit_p)
                    outcome = "WIN" if pnl_pct >= 0 else "LOSS"
                    tag, exit_price, exit_date = "TREND_REVERSAL", exit_p, ts
                    break

        elif state == "PARTIAL_TP1":
            hit_stop = lo <= breakeven_price if direction == "LONG" else hi >= breakeven_price
            hit_t2 = hi >= target2 if direction == "LONG" else lo <= target2

            if hit_stop:
                fill = breakeven_price * (1 - MAX_SLIPPAGE_PCT) if direction == "LONG" else breakeven_price * (1 + MAX_SLIPPAGE_PCT)
                pnl1 = _pnl_pct(direction, entry_price, tp1_exit_price)
                pnl2 = _pnl_pct(direction, entry_price, fill)
                pnl_pct = TP1_CLOSE_PCT * pnl1 + (1 - TP1_CLOSE_PCT) * pnl2
                outcome = "WIN" if pnl_pct >= 0 else "LOSS"
                tag, exit_price, exit_date = "TP1_SL_BE", fill, ts
                break
            if hit_t2:
                pnl1 = _pnl_pct(direction, entry_price, tp1_exit_price)
                pnl2 = _pnl_pct(direction, entry_price, target2)
                pnl_pct = TP1_CLOSE_PCT * pnl1 + (1 - TP1_CLOSE_PCT) * pnl2
                outcome, tag, exit_price, exit_date = "WIN", "TP1_TP2", target2, ts
                break

            if enable_dynamic_exit:
                window = full_df.loc[:ts].tail(DYNAMIC_EXIT_CONTEXT_BARS)
                reversal, _ratio = _dynamic_exit_signal(window, direction)
                if reversal:
                    exit_p = float(row["Close"])
                    pnl1 = _pnl_pct(direction, entry_price, tp1_exit_price)
                    pnl2 = _pnl_pct(direction, entry_price, exit_p)
                    pnl_pct = TP1_CLOSE_PCT * pnl1 + (1 - TP1_CLOSE_PCT) * pnl2
                    outcome = "WIN" if pnl_pct >= 0 else "LOSS"
                    tag, exit_price, exit_date = "TREND_REVERSAL", exit_p, ts
                    break

    if outcome is None:
        # Ran out of historical data (or the signal fell on the very last
        # available bar) without ever hitting a stop, target, or reversal --
        # the position is still open, not force-closed. `pnl_pct` here is an
        # unrealized mark against the last available close.
        if len(future) == 0:
            exit_price, exit_date = entry_price, signal_date
        else:
            exit_price, exit_date = float(future["Close"].iloc[-1]), future.index[-1]
        if state == "PARTIAL_TP1":
            pnl1 = _pnl_pct(direction, entry_price, tp1_exit_price)
            pnl2 = _pnl_pct(direction, entry_price, exit_price)
            pnl_pct = TP1_CLOSE_PCT * pnl1 + (1 - TP1_CLOSE_PCT) * pnl2
        else:
            pnl_pct = _pnl_pct(direction, entry_price, exit_price)
        outcome, tag = "OPEN", "END_OF_DATA"

    days_held = (future.index.get_loc(exit_date) + 1) if exit_date in future.index else 0

    return TradeOutcome(
        ticker=ticker, asset_class=asset_class, direction=direction, signal_date=signal_date,
        entry_low=entry_low, entry_high=entry_high, entry_price=entry_price,
        stop=stop, initial_stop=initial_stop, target1=target1, target2=target2,
        exit_date=exit_date, exit_price=exit_price,
        outcome=outcome, tag=tag, tp1_hit=(tp1_exit_price is not None),
        pnl_pct=round(pnl_pct, 3), days_held=days_held, score=score,
    )
