"""Paper Trading Tracker.

Persistent JSON-backed store of simulated ("paper") trade positions produced
by the live runner. Tracks each signal from PENDING (entry zone not yet
touched) through ACTIVE -> PARTIAL_TP1 (50% closed, stop moved to breakeven)
-> WIN_TP2 / LOSS_SL_BE, or straight ACTIVE -> LOSS_SL if the stop is hit
before TP1 ever triggers. An EMERGENCY_EXIT can close the (remaining)
position early out of ACTIVE or PARTIAL_TP1 on a detected volume-surge
reversal. Marks positions against live market prices via `utils.data_fetch`.
"""
from __future__ import annotations

import datetime as dt
import sys
import uuid
from dataclasses import asdict, dataclass

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import (
    MAX_SLIPPAGE_PCT,
    STAGNATION_BAR_MINUTES,
    STAGNATION_BARS,
    STAGNATION_EXIT_ENABLED,
    STAGNATION_MIN_PROGRESS_R,
    PAPER_TRADING_STORE,
    PENDING_EXPIRY_HOURS,
    TP1_CLOSE_PCT,
)
from core.data.health import note_failure
from core.paper_trading.store import load_trades, pnl_pct as _pnl_pct, save_trades
from paper_trading import journal
from utils.data_fetch import get_binance_24hr_ticker, get_stock_fast_info

# INTRADAY: expiry is now measured in HOURS, not days. A 15m-triggered setup
# that has not filled within a few hours no longer reflects the structure that
# produced it; leaving it PENDING for 5 days meant stale signals could fill
# against a completely different market.
PENDING_EXPIRY = dt.timedelta(hours=PENDING_EXPIRY_HOURS)


@dataclass
class PaperTrade:
    id: str
    signal_date: str
    ticker: str
    asset_class: str
    direction: str
    entry_zone: list
    stop_loss: float           # current active stop -- moved to breakeven_price once TP1 hits
    initial_stop_loss: float
    target1: float
    target2: float
    tp1_close_pct: float
    breakeven_price: float
    current_status: str = "PENDING"
    tp1_hit: bool = False
    tp1_exit_price: float | None = None
    remaining_size_pct: float = 1.0
    emergency_exit_reason: str | None = None
    entry_date: str | None = None
    exit_date: str | None = None
    exit_price: float | None = None
    pnl_pct: float | None = None
    telegram_message_id: int | None = None


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _migrate(trade: dict) -> dict:
    """Backfill fields introduced by the multi-tier TP/breakeven lifecycle so
    trades logged by the old single-shot-TP engine keep tracking correctly
    instead of KeyError'ing on the new state machine."""
    if "initial_stop_loss" in trade:
        return trade  # already on the new schema
    entry_low, entry_high = trade["entry_zone"]
    entry_price = (entry_low + entry_high) / 2
    direction = trade["direction"]
    trade["initial_stop_loss"] = trade["stop_loss"]
    trade["tp1_close_pct"] = TP1_CLOSE_PCT
    trade["breakeven_price"] = (
        entry_price * (1 + MAX_SLIPPAGE_PCT) if direction == "LONG" else entry_price * (1 - MAX_SLIPPAGE_PCT)
    )
    trade["tp1_hit"] = False
    trade["tp1_exit_price"] = None
    trade["remaining_size_pct"] = 1.0
    trade["emergency_exit_reason"] = None
    return trade


def _load() -> list[dict]:
    """Shared atomic-store read (core.paper_trading.store), with THIS model's
    schema migration applied per record -- only Livne knows how to backfill
    its multi-tier TP/breakeven fields."""
    return load_trades(PAPER_TRADING_STORE, migrate=_migrate)


def _save(trades: list[dict]) -> None:
    save_trades(PAPER_TRADING_STORE, trades)


def _journal_safe(trade: dict) -> bool:
    """Journal a trade without ever letting it break the cycle.

    `core.paper_trading.journal.upsert_row` is already non-raising, but the
    per-model `upsert_trade_row` above it also builds the row values from the
    trade dict -- so a schema surprise there could still raise. This is the
    outer guard: the trade store and the Telegram alert are what matter, and
    neither may be lost because a spreadsheet could not be written.
    """
    try:
        journal.upsert_trade_row(trade)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[journal] could not journal {trade.get('ticker')} "
              f"({trade.get('id')}): {type(exc).__name__}: {exc}. "
              f"Trade store and alerts unaffected.", file=sys.stderr)
        return False


def log_trade(setup, telegram_message_id: int | None = None) -> dict:
    """Persist a new PENDING paper trade from a `generator.trade_generator.
    TradeSetup` and return its record."""
    trades = _load()
    record = PaperTrade(
        id=str(uuid.uuid4())[:8],
        signal_date=_now(),
        ticker=setup.ticker,
        asset_class=setup.asset_class,
        direction=setup.direction,
        entry_zone=[setup.entry_low, setup.entry_high],
        stop_loss=setup.stop,
        initial_stop_loss=setup.stop,
        target1=setup.target1,
        target2=setup.target2,
        tp1_close_pct=setup.tp1_close_pct,
        breakeven_price=setup.breakeven_price,
        telegram_message_id=telegram_message_id,
    )
    trade_dict = asdict(record)
    trades.append(trade_dict)
    _save(trades)
    _journal_safe(trade_dict)
    return trade_dict


def has_open_trade(ticker: str, asset_class: str, trades: list[dict] | None = None) -> bool:
    """True if `ticker` already has an open (non-terminal) paper trade, so
    callers can skip generating a duplicate alert for the same setup.

    `trades` lets a caller supply a snapshot it already holds. The scan calls
    this once per candidate, and re-reading + re-migrating the whole store each
    time meant parsing 133 records (~110 KB) six times per asset class for an
    answer that cannot change mid-scan -- a watchlist has no duplicate tickers,
    and nothing else writes this store while the lock is held.
    """
    records = _load() if trades is None else trades
    return any(
        t["ticker"] == ticker and t["asset_class"] == asset_class
        and t["current_status"] in ("PENDING", "ACTIVE", "PARTIAL_TP1")
        for t in records
    )


def get_current_price(ticker: str, asset_class: str) -> float | None:
    try:
        if asset_class == "crypto":
            return float(get_binance_24hr_ticker(ticker)["lastPrice"])
        info = get_stock_fast_info(ticker)
        return info.get("last_price")
    except Exception as exc:
        # Names the exception TYPE and counts the streak, so a provider outage
        # is one loud line rather than forty indistinguishable ones.
        note_failure("binance" if asset_class == "crypto" else "yahoo",
                     "current_price", exc, ticker)
        return None


def _blended_pnl(trade: dict, entry_price: float, leg2_exit_price: float) -> float:
    """Weighted P&L across the TP1 partial close (tp1_close_pct of size) and
    the remaining leg's exit -- or, if TP1 never triggered, the whole
    position's single-leg P&L."""
    direction = trade["direction"]
    if not trade["tp1_hit"]:
        return round(_pnl_pct(direction, entry_price, leg2_exit_price), 2)
    pnl_leg1 = _pnl_pct(direction, entry_price, trade["tp1_exit_price"])
    pnl_leg2 = _pnl_pct(direction, entry_price, leg2_exit_price)
    blended = trade["tp1_close_pct"] * pnl_leg1 + trade["remaining_size_pct"] * pnl_leg2
    return round(blended, 2)


def _close_trade(trade: dict, status: str, exit_price: float, entry_price: float,
                  emergency_reason: str | None = None, at: str | None = None) -> None:
    """`at` is the ISO timestamp of the event. It defaults to now for the live
    path; the historical catch-up passes the timestamp of the BAR on which the
    level was actually crossed, which is the whole point of replaying."""
    stamp = at or _now()
    trade["current_status"] = status
    trade["exit_date"] = stamp
    trade["exit_price"] = exit_price
    trade["pnl_pct"] = _blended_pnl(trade, entry_price, exit_price)
    if emergency_reason:
        trade["emergency_exit_reason"] = emergency_reason



def _stagnating(trade: dict, high: float, low: float, ts: dt.datetime) -> tuple[bool, str]:
    """Whether an ACTIVE position has gone nowhere for long enough to cut.

    HORIZON CALIBRATION. Livne rides a directionless position all the way to a
    full stop, and that population is the single largest contributor to its
    LOSS_SL drag. Cutting early converts a full-R loss into a fraction of one,
    and occasionally into a small win.

    Two conditions, both required:

      TIME     at least `STAGNATION_BARS` bars of the trigger timeframe have
               elapsed since entry. Measured in BARS rather than wall-clock
               minutes because that is the unit the setup was formed in.

      NO PROGRESS  the position has never travelled `STAGNATION_MIN_PROGRESS_R`
               of the distance from entry to TP1, measured as the RUNNING
               maximum favourable excursion (`max_progress_r`, updated on every
               bar). Using the running maximum rather than the current bar is
               what stops this cutting a slow winner: a position that genuinely
               moved toward its target and then gave some back has made
               progress, and is left alone.

    Only ACTIVE positions are eligible. A PARTIAL_TP1 position has already
    reached TP1 by definition, has banked half, and is running on a breakeven
    stop; there is nothing left for this rule to protect.
    """
    if not STAGNATION_EXIT_ENABLED or trade.get("tp1_hit"):
        return False, ""

    entry_raw = trade.get("entry_date")
    if not entry_raw:
        return False, ""
    try:
        entered = dt.datetime.fromisoformat(entry_raw)
    except (TypeError, ValueError):
        return False, ""

    elapsed_bars = (ts - entered).total_seconds() / 60.0 / max(STAGNATION_BAR_MINUTES, 1)
    if elapsed_bars < STAGNATION_BARS:
        return False, ""

    entry_low, entry_high = trade["entry_zone"]
    entry_price = (entry_low + entry_high) / 2
    target1 = trade["target1"]
    reach = abs(target1 - entry_price)
    if reach <= 0:
        return False, ""

    # Progress is the RUNNING maximum favourable excursion, not this bar's.
    # `apply_bar` sees one bar at a time and has no memory, so without
    # persisting it a position that ran 60% of the way to TP1 and pulled back
    # would be cut as "directionless" on the next quiet bar -- the exact
    # opposite of the intent, which is to spare a slow winner.
    progress = float(trade.get("max_progress_r") or 0.0)

    if progress >= STAGNATION_MIN_PROGRESS_R:
        return False, ""
    return True, (f"no progress after {int(elapsed_bars)} bars "
                  f"({progress:.0%} of the way to TP1, need {STAGNATION_MIN_PROGRESS_R:.0%})")


def apply_bar(trade: dict, high: float, low: float, ts: dt.datetime) -> bool:
    """Apply ONE bar's traded range to ONE open trade. Returns True if the
    status changed. Mutates `trade` in place.

    This is the single implementation of Livne's lifecycle transitions, used by
    both callers:

        live      one call per cycle, with the range since the last poll and
                  ts=now -- byte-for-byte the behaviour this loop always had.
        catch-up  one call per historical bar, oldest first, with that bar's
                  timestamp (see core.paper_trading.catch_up).

    Keeping it in one place is deliberate. A separate replay implementation
    would be a second copy of a state machine that manages real positions, and
    it would drift from this one the first time either was tuned.

    NOT handled here: the emergency reversal exit. It needs a volume/candle
    pattern rather than a price range, it is live-only by design (the backtest
    does not simulate it either), and replaying it from historical bars would
    invent exits the live system would not have taken.
    """
    status = trade["current_status"]
    if status not in ("PENDING", "ACTIVE", "PARTIAL_TP1"):
        return False

    direction = trade["direction"]
    entry_low, entry_high = trade["entry_zone"]
    entry_price = (entry_low + entry_high) / 2
    stamp = ts.isoformat(timespec="seconds")

    if status == "PENDING":
        signal_dt = dt.datetime.fromisoformat(trade["signal_date"])
        # Entry-zone touch is an OVERLAP test between the entry zone and the
        # bar's traded range, not "is the last price inside the zone right
        # now". Entry zones are ~0.7% wide, so price routinely traded through
        # the zone and back out between polls -- with a point-in-time check
        # those trades stayed PENDING and eventually EXPIRED, silently
        # under-reporting fills and skewing every downstream win-rate metric.
        if low <= entry_high and high >= entry_low:
            trade["current_status"] = "ACTIVE"
            trade["entry_date"] = stamp
            trade["last_status_change"] = stamp
            return True
        if (ts - signal_dt) >= PENDING_EXPIRY:
            trade["current_status"] = "EXPIRED"
            trade["exit_date"] = stamp
            trade["last_status_change"] = stamp
            return True
        return False

    stop = trade["stop_loss"]
    hit_stop = low <= stop if direction == "LONG" else high >= stop
    hit_tp1 = high >= trade["target1"] if direction == "LONG" else low <= trade["target1"]
    hit_tp2 = high >= trade["target2"] if direction == "LONG" else low <= trade["target2"]

    changed = False
    if status == "ACTIVE":
        # HORIZON: track how far this position has EVER travelled toward TP1,
        # as a fraction of the entry->TP1 distance. `apply_bar` sees one bar at
        # a time, so the stagnation rule needs this persisted to tell a slow
        # winner from a directionless position.
        _reach = abs(trade["target1"] - entry_price)
        if _reach > 0:
            _best = (high - entry_price) if direction == "LONG" else (entry_price - low)
            trade["max_progress_r"] = max(float(trade.get("max_progress_r") or 0.0),
                                          _best / _reach)

        if hit_stop:
            # Same-bar ambiguity (the range covers both the stop and a target)
            # is resolved conservatively in favour of the stop, regardless of
            # what else the range also touched -- OHLC cannot say which came
            # first, and only the adverse assumption cannot flatter the result.
            fill = stop * (1 - MAX_SLIPPAGE_PCT) if direction == "LONG" else stop * (1 + MAX_SLIPPAGE_PCT)
            _close_trade(trade, "LOSS_SL", fill, entry_price, at=stamp)
            changed = True
        elif hit_tp2:
            # Gapped through both targets in one bar -- credit both legs.
            trade["tp1_hit"] = True
            trade["tp1_exit_price"] = trade["target1"]
            trade["remaining_size_pct"] = 1 - trade["tp1_close_pct"]
            _close_trade(trade, "WIN_TP2", trade["target2"], entry_price, at=stamp)
            changed = True
        elif hit_tp1:
            trade["tp1_hit"] = True
            trade["tp1_exit_price"] = trade["target1"]
            trade["remaining_size_pct"] = 1 - trade["tp1_close_pct"]
            trade["stop_loss"] = trade["breakeven_price"]  # Breakeven Adjustment
            trade["current_status"] = "PARTIAL_TP1"
            changed = True
        else:
            # HORIZON: the stagnation exit. Checked LAST in this branch, and
            # that ordering is load-bearing -- a bar that touched the stop is a
            # LOSS_SL and a bar that touched TP1 is a PARTIAL_TP1. Grading
            # either as the milder stagnation outcome would flatter the
            # results, which is the one thing a paper-trading system must never
            # do to itself.
            stale, reason = _stagnating(trade, high, low, ts)
            if stale:
                # Closed at the bar's midpoint: this is a discretionary exit at
                # market, not a level being hit, so neither extreme of the bar
                # is a defensible fill.
                fill = (high + low) / 2
                _close_trade(trade, "STAGNATION_EXIT", fill, entry_price, at=stamp)
                trade["stagnation_exit_reason"] = reason
                changed = True

    elif status == "PARTIAL_TP1":
        if hit_stop:
            fill = stop * (1 - MAX_SLIPPAGE_PCT) if direction == "LONG" else stop * (1 + MAX_SLIPPAGE_PCT)
            _close_trade(trade, "LOSS_SL_BE", fill, entry_price, at=stamp)
            changed = True
        elif hit_tp2:
            _close_trade(trade, "WIN_TP2", trade["target2"], entry_price, at=stamp)
            changed = True

    if changed:
        # Stamped here rather than by the caller: `exit_date` alone is not
        # enough for the daily summary, since PARTIAL_TP1 is a status change
        # but not a close and never sets it.
        trade["last_status_change"] = stamp
    return changed


# ORCHESTRATION MOVED OUT. `update_open_positions`, `flush_alerts` and
# `catch_up_open_positions` used to live here and were the same three functions
# Biton's tracker carried -- the loop, not the lifecycle. They are now
# `core.runtime.TenantRunner` + `core.paper_trading.refresh`, shared by every
# tenant, so a fix to the refresh order, the lookback window or the outbox
# handshake reaches this model without it being edited.
#
# Two Livne-specific pieces did NOT move, because they are lifecycle:
#   `apply_bar`      the multi-tier PARTIAL_TP1/breakeven state machine.
#   `_close_trade`   used by the runner's `pre_bar` hook to close an emergency
#                    reversal with this model's blended P&L accounting.



def get_all_trades() -> list[dict]:
    return _load()
