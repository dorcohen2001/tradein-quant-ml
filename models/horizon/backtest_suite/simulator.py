"""Intraday trade execution logic & outcome tracker.

Given a signal produced at instant T, this walks forward through the real
subsequent INTRADAY bars and grades the trade through the same multi-tier
TP1(50%)/breakeven/TP2 lifecycle the live paper tracker
(`paper_trading.tracker`) uses.

THE INTRADAY CHANGE
-------------------
Stops and targets are evaluated against each bar's HIGH and LOW, not its
Close, and on intraday bars rather than daily ones. That is not a refinement,
it is a correctness fix: a 15m candle that opens at 100, wicks to 97 and
closes at 101 HAS taken out a stop at 98. A close-only or daily-only
simulator books that bar as a win the trade never had. Exit timestamps are
consequently precise to the execution bar, and the worst unrealised mark the
position ever reached (its maximum adverse excursion, usually an intraday
wick) is recorded with its own timestamp.

LIFECYCLE
---------
  ACTIVE       LONG takes the TP1 leg if High >= Target1 before Low <= Stop
               (SHORT mirrored). Stop first is a full-size LOSS ("SL").
  -> on TP1    `TP1_CLOSE_PCT` of the position books at Target1 and the stop
               for the remainder moves to breakeven (with the same slippage
               buffer used live). State becomes PARTIAL_TP1.
  PARTIAL_TP1  The remainder exits at the breakeven stop ("TP1_SL_BE") or at
               Target2 ("TP1_TP2"). P&L is the size-weighted blend of both legs.
  any bar      When neither stop nor target was touched, a dynamic trend-
               reversal exit is checked on the TRIGGER timeframe: an opposing
               braking/reversal event or a MACD histogram cross, combined with
               abnormal volume, closes the remaining position at that bar's
               close ("TREND_REVERSAL").
  any bar      HORIZON: the STAGNATION exit. An ACTIVE position that has run
               `STAGNATION_BARS` trigger bars without ever travelling
               `STAGNATION_MIN_PROGRESS_R` of the way to TP1 is cut at the
               bar's close ("STAGNATION"). This mirrors
               `paper_trading.tracker._stagnating` deliberately, including the
               running-maximum progress measure: it is the headline
               anti-LOSS_SL rule of this model, and a backtest that could not
               see it would be measuring a different strategy than the one
               that trades.

There is no fixed holding-period cutoff. A trade that is still unresolved when
the historical data runs out is reported OPEN (tag `END_OF_DATA`) -- an honest
"don't know yet", not a fabricated win or loss. `TradeRecord.resolved`
distinguishes the two and every summary metric excludes the unresolved ones.

INTRABAR ORDERING
-----------------
One bar whose range covers both the stop and a target cannot, on its own, say
which was touched first. Default policy is the conservative one -- assume the
stop -- because that is the only assumption that cannot flatter the win rate.
Running with a finer `--exec-interval` shrinks the ambiguous set (a 5m bar is
ambiguous far less often than a 15m one), and an optional
`core.backtesting.FinerBarTieBreaker` resolves what remains from finer candles
where they exist. See `core/backtesting/engine.py`.

ENTRY FILL
----------
By default the trade is assumed FILLED at the entry-zone midpoint at the
trigger bar's close -- the same simplification the daily version made, kept so
results stay comparable. It slightly flatters results versus the live tracker,
which requires a real entry-zone touch first. `require_entry_touch=True` now
models that properly (it needs intraday bars to be meaningful, which is why it
could not exist before): the order rests until price actually trades into the
zone, and expires unfilled after `entry_expiry`.
"""
from __future__ import annotations

import pandas as pd

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import (
    STAGNATION_BAR_MINUTES,
    STAGNATION_BARS,
    STAGNATION_EXIT_ENABLED,
    STAGNATION_MIN_PROGRESS_R,
    DYNAMIC_EXIT_CONTEXT_BARS,
    DYNAMIC_EXIT_VOLUME_LOOKBACK,
    DYNAMIC_EXIT_VOLUME_MULT,
    MAX_SLIPPAGE_PCT,
    TP1_CLOSE_PCT,
)
from core.backtesting import ExcursionTracker, TradeRecord, apply_slippage, hits, interval_of, pnl_pct
from core.backtesting.engine import resolve_same_bar
from ta.mtfa import detect_braking_event
from ta.ta_engine import macd, volume_spike

# The dynamic-exit reversal check is the one per-bar cost that is not O(1), so
# it runs at most once per TRIGGER bar even when the position is being managed
# on a finer execution interval -- a reversal "signal" recomputed every 5m
# against 15m structure would fire on the same formation three times anyway.
MIN_REVERSAL_CONTEXT_BARS = 2


def _opposite(direction: str) -> str:
    return "SHORT" if direction == "LONG" else "LONG"


def _trend_reversal_signal(window: pd.DataFrame, direction: str) -> bool:
    """True if `window` shows a braking/reversal event AGAINST the held
    `direction`. Reuses the exact same multi-condition, multi-bar detector
    Stage 7 uses for entry confirmation (`ta.mtfa.detect_braking_event`) --
    called with the OPPOSITE of the held direction, since a bearish event is
    what closes a LONG (and vice versa). A MACD histogram cross is kept as an
    independent additional condition on top of the four braking-event
    signatures (pin-bar rejection, classic pattern, volume climax, MSS)."""
    if len(window) < MIN_REVERSAL_CONTEXT_BARS:
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
    - 2.0x the N-bar average). `window` is already trimmed to a bounded
    trailing context (`DYNAMIC_EXIT_CONTEXT_BARS`) -- this is re-evaluated on
    every trigger bar of a trade that may run for days, so an unbounded
    from-inception window would make one trade's cost grow quadratically with
    how long it stays open."""
    if not _trend_reversal_signal(window, direction):
        return False, 0.0
    vol_ok, ratio = volume_spike(window, mult=DYNAMIC_EXIT_VOLUME_MULT, lookback=DYNAMIC_EXIT_VOLUME_LOOKBACK)
    return vol_ok, ratio


def _blend(direction: str, entry_price: float, tp1_price: float, exit_price: float) -> float:
    """Size-weighted P&L across the two legs of a position that banked TP1."""
    leg1 = pnl_pct(direction, entry_price, tp1_price)
    leg2 = pnl_pct(direction, entry_price, exit_price)
    return TP1_CLOSE_PCT * leg1 + (1 - TP1_CLOSE_PCT) * leg2


def _resolve_entry(
    bars: pd.DataFrame, direction: str, entry_low: float, entry_high: float,
    signal_time: pd.Timestamp, expiry: pd.Timedelta | None,
) -> tuple[pd.Timestamp, float, int] | None:
    """Find the bar on which an order into [entry_low, entry_high] would actually
    have filled. Returns (fill_time, fill_price, bar_position), or None if price
    never traded into the zone before the order expired.

    The fill price is the first price INSIDE the zone the market actually
    traded at on that bar:

      * the bar opened inside the zone -- which is the normal case on the
        signal bar itself, since `strategy_engine.entry_zone` builds the zone
        around the trigger price -- so it fills at the open;
      * the bar opened above the zone and fell into it, so it fills at the
        zone's upper edge;
      * the bar opened below and rose into it, so it fills at the lower edge.

    Deliberately direction-agnostic: which edge fills is decided by where price
    came FROM, not by whether the position is long or short.
    """
    deadline = signal_time + expiry if expiry is not None else None
    for pos, (ts, row) in enumerate(bars.iterrows()):
        if deadline is not None and ts > deadline:
            return None
        low, high = float(row["Low"]), float(row["High"])
        if high < entry_low or low > entry_high:
            continue
        open_px = float(row["Open"])
        if open_px > entry_high:
            fill = entry_high
        elif open_px < entry_low:
            fill = entry_low
        else:
            fill = open_px
        return ts, float(fill), pos
    return None


def simulate_trade(
    exec_df: pd.DataFrame,
    ticker: str,
    asset_class: str,
    direction: str,
    signal_time: pd.Timestamp,
    entry_low: float,
    entry_high: float,
    stop: float,
    target1: float,
    target2: float,
    score: float,
    interval: str,
    notional: float = 1000.0,
    reversal_df: pd.DataFrame | None = None,
    reversal_interval: str | None = None,
    enable_dynamic_exit: bool = True,
    tie_breaker=None,
    require_entry_touch: bool = False,
    entry_expiry: pd.Timedelta | None = None,
) -> TradeRecord | None:
    """Walk `exec_df` forward from `signal_time` and grade the trade.

    `exec_df` is the interval the position is MANAGED on (finer than the
    signal interval if you want tighter exit timestamps); `reversal_df` is the
    trigger-timeframe frame the dynamic exit reads its structure from.

    Returns None only when `require_entry_touch` is set and price never
    returned to the entry zone -- a setup that never became a position.
    """
    forward = exec_df.loc[exec_df.index >= pd.Timestamp(signal_time)]
    exec_delta = interval_of(interval).delta

    # --- entry ------------------------------------------------------------
    if require_entry_touch:
        filled = _resolve_entry(forward, direction, entry_low, entry_high, signal_time, entry_expiry)
        if filled is None:
            return None
        entry_time, entry_price, start_pos = filled
        # The fill bar stays in the walk: a bar that dipped into the entry zone
        # and kept going can genuinely take the stop out on that same bar, and
        # dropping it would quietly discard those losses.
        walk = forward.iloc[start_pos:]
    else:
        entry_price = (entry_low + entry_high) / 2
        entry_time = pd.Timestamp(signal_time)
        walk = forward

    initial_stop = stop
    breakeven_price = apply_slippage(entry_price, direction, MAX_SLIPPAGE_PCT, adverse=False)
    excursion = ExcursionTracker(direction, entry_price)

    # Trigger-frame context for the dynamic exit, plus a cursor so the check
    # runs once per trigger bar rather than once per execution bar.
    rev_df = reversal_df if reversal_df is not None else exec_df
    rev_delta = interval_of(reversal_interval or interval).delta
    rev_index = rev_df.index
    last_rev_pos = -1

    state = "ACTIVE"
    tp1_price: float | None = None
    outcome = tag = None
    exit_price = exit_time = None
    result_pct = None

    max_progress = 0.0          # running max favourable excursion, as a fraction of entry->TP1
    for ts, row in walk.iterrows():
        excursion.update(ts, row)
        # Bars are stamped with their OPEN time, but an exit is only KNOWN to
        # have happened once the bar completes. Exits are therefore timestamped
        # at the bar's close: a level touched somewhere inside the bar is
        # reported at the first instant it is certain, and a close-based exit
        # (trend reversal) is reported at the close it was decided on. The
        # alternative -- stamping the open -- prints trades that exit zero
        # minutes after they were entered.
        bar_close = ts + exec_delta
        active_stop = stop if state == "ACTIVE" else breakeven_price

        stop_hit = hits(row, direction, active_stop, "stop")
        t1_hit = hits(row, direction, target1, "target") if state == "ACTIVE" else False
        t2_hit = hits(row, direction, target2, "target")

        if stop_hit and (t1_hit or t2_hit):
            # Both sides of the trade printed inside one bar. Ask the finer
            # data which came first; without it, assume the stop.
            first = resolve_same_bar(
                tie_breaker, ticker, asset_class, ts, direction,
                active_stop, target2 if t2_hit else target1,
            )
            if first == "target":
                stop_hit = False

        if stop_hit:
            fill = apply_slippage(active_stop, direction, MAX_SLIPPAGE_PCT, adverse=True)
            if state == "ACTIVE":
                result_pct = pnl_pct(direction, entry_price, fill)
                outcome, tag = "LOSS", "SL"
            else:
                result_pct = _blend(direction, entry_price, tp1_price, fill)
                outcome, tag = ("WIN" if result_pct >= 0 else "LOSS"), "TP1_SL_BE"
            exit_price, exit_time = fill, bar_close
            break

        if t2_hit:
            # Reaching TP2 credits both legs, whether TP1 was banked on an
            # earlier bar or the same bar gapped straight through both.
            result_pct = _blend(direction, entry_price, tp1_price if tp1_price else target1, target2)
            outcome, tag, exit_price, exit_time = "WIN", "TP1_TP2", target2, bar_close
            break

        if t1_hit:
            tp1_price = target1
            state = "PARTIAL_TP1"
            stop = breakeven_price  # Breakeven Adjustment
            continue

        # HORIZON: stagnation. Checked AFTER stop and targets -- a bar that
        # took the stop is a stop, and a bar that reached TP1 banks the
        # partial. Grading either as the milder stagnation outcome would
        # flatter the results.
        #
        # ACTIVE only: a PARTIAL_TP1 position has reached TP1 by definition,
        # has banked half, and runs on a breakeven stop.
        if STAGNATION_EXIT_ENABLED and state == "ACTIVE":
            reach = abs(target1 - entry_price)
            if reach > 0:
                best = ((float(row["High"]) - entry_price) if direction == "LONG"
                        else (entry_price - float(row["Low"])))
                max_progress = max(max_progress, best / reach)
            elapsed_bars = ((bar_close - entry_time).total_seconds() / 60.0
                            / max(STAGNATION_BAR_MINUTES, 1))
            if elapsed_bars >= STAGNATION_BARS and max_progress < STAGNATION_MIN_PROGRESS_R:
                exit_p = float(row["Close"])
                result_pct = pnl_pct(direction, entry_price, exit_p)
                outcome = "WIN" if result_pct >= 0 else "LOSS"
                tag, exit_price, exit_time = "STAGNATION", exit_p, bar_close
                break

        if enable_dynamic_exit:
            # Only re-check once a NEW trigger bar has closed.
            rev_pos = int(rev_index.searchsorted(bar_close - rev_delta, side="right"))
            if rev_pos > last_rev_pos and rev_pos >= MIN_REVERSAL_CONTEXT_BARS:
                last_rev_pos = rev_pos
                window = rev_df.iloc[max(0, rev_pos - DYNAMIC_EXIT_CONTEXT_BARS):rev_pos]
                reversal, _ratio = _dynamic_exit_signal(window, direction)
                if reversal:
                    exit_p = float(row["Close"])
                    if state == "ACTIVE":
                        result_pct = pnl_pct(direction, entry_price, exit_p)
                    else:
                        result_pct = _blend(direction, entry_price, tp1_price, exit_p)
                    outcome = "WIN" if result_pct >= 0 else "LOSS"
                    tag, exit_price, exit_time = "TREND_REVERSAL", exit_p, bar_close
                    break

    resolved = outcome is not None
    if not resolved:
        # Ran out of historical data without hitting a stop, a target or a
        # reversal. The position is still open, not force-closed; `pnl_pct`
        # here is an unrealised mark against the last available close.
        if len(walk) == 0:
            exit_price, exit_time = entry_price, entry_time
        else:
            exit_price, exit_time = float(walk["Close"].iloc[-1]), walk.index[-1] + exec_delta
        if state == "PARTIAL_TP1":
            result_pct = _blend(direction, entry_price, tp1_price, exit_price)
        else:
            result_pct = pnl_pct(direction, entry_price, exit_price)
        outcome, tag = "OPEN", "END_OF_DATA"

    return TradeRecord(
        ticker=ticker, asset_class=asset_class, direction=direction, interval=interval,
        entry_time=entry_time, entry_price=entry_price,
        exit_time=exit_time, exit_price=float(exit_price),
        exit_tag=tag, outcome=outcome, resolved=resolved,
        pnl_pct=round(float(result_pct), 3), notional=notional,
        initial_stop=initial_stop,
        # `initial_stop` is the structural stop at signal time; `stop_at_exit`
        # is whatever was active when the trade closed -- the breakeven price
        # once TP1 has triggered. They differ for any trade that reached
        # PARTIAL_TP1, by design (Breakeven Adjustment).
        stop_at_exit=stop,
        target1=target1, target2=target2, tp1_hit=tp1_price is not None,
        mae_pct=excursion.mae_pct, mfe_pct=excursion.mfe_pct, mae_time=excursion.mae_time,
        score=score,
    )
