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
    CATCH_UP_CONFIG,
    CYCLE_INTERVAL_MINUTES,
    EMERGENCY_CANDLE_LOOKBACK,
    MAX_SLIPPAGE_PCT,
    PAPER_TRADING_STORE,
    PENDING_EXPIRY_HOURS,
    TP1_CLOSE_PCT,
)
from core.paper_trading.catch_up import CatchUpConfig, CatchUpReport, run_catch_up
from core.paper_trading.store import load_trades, pnl_pct as _pnl_pct, save_trades
from core.utils.market_hours import is_market_open
from paper_trading import journal
from risk.risk_manager import detect_emergency_reversal
from utils.data_fetch import (
    get_binance_24hr_ticker,
    get_recent_intraday_candles,
    get_recent_intraday_range,
    get_stock_fast_info,
)

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


def has_open_trade(ticker: str, asset_class: str) -> bool:
    """True if `ticker` already has an open (non-terminal) paper trade, so
    callers can skip generating a duplicate alert for the same setup."""
    return any(
        t["ticker"] == ticker and t["asset_class"] == asset_class
        and t["current_status"] in ("PENDING", "ACTIVE", "PARTIAL_TP1")
        for t in _load()
    )


def get_current_price(ticker: str, asset_class: str) -> float | None:
    try:
        if asset_class == "crypto":
            return float(get_binance_24hr_ticker(ticker)["lastPrice"])
        info = get_stock_fast_info(ticker)
        return info.get("last_price")
    except Exception as exc:
        print(f"[tracker] price fetch failed for {ticker}: {exc}", file=sys.stderr)
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


def update_open_positions() -> list[dict]:
    """Refresh every open trade against the current market price. Returns the
    list of trades whose status changed in this pass, so callers can push
    Telegram notifications for newly triggered/partial/closed positions."""
    trades = _load()
    changed: list[dict] = []
    stocks_market_open = is_market_open()
    if not stocks_market_open:
        print(
            "[market] US equities market is CLOSED — skipping stock position "
            "refresh this cycle (crypto still evaluated).",
            file=sys.stderr,
        )

    for trade in trades:
        status = trade["current_status"]
        if status not in ("PENDING", "ACTIVE", "PARTIAL_TP1"):
            continue
        if trade["asset_class"] == "stocks" and not stocks_market_open:
            continue

        price = get_current_price(trade["ticker"], trade["asset_class"])
        if price is None:
            continue

        direction = trade["direction"]
        entry_low, entry_high = trade["entry_zone"]
        entry_price = (entry_low + entry_high) / 2

        # The price RANGE since the last check -- not just the instantaneous
        # last-trade price -- is what every level test uses, so a level touched
        # between two cycles is still caught.
        rng = get_recent_intraday_range(trade["ticker"], trade["asset_class"], CYCLE_INTERVAL_MINUTES)
        hi, lo = (rng["high"], rng["low"]) if rng is not None else (price, price)

        # Emergency reversal is checked BEFORE the range, and only here: it is
        # a live-only signal driven by a volume/candle anomaly rather than by a
        # price level, so it has no meaningful historical replay (see
        # `apply_bar`).
        if status in ("ACTIVE", "PARTIAL_TP1"):
            candles = get_recent_intraday_candles(trade["ticker"], trade["asset_class"],
                                                  EMERGENCY_CANDLE_LOOKBACK + 5)
            if candles is not None:
                is_emergency, reason = detect_emergency_reversal(direction, candles)
                if is_emergency:
                    now_iso = _now()
                    _close_trade(trade, "EMERGENCY_EXIT", price, entry_price,
                                 emergency_reason=reason, at=now_iso)
                    trade["last_status_change"] = now_iso
                    trade["last_checked_at"] = now_iso
                    changed.append(trade)
                    continue

        # One bar -- the range since the last poll -- through the SAME
        # transition function the historical catch-up replays bar by bar.
        if apply_bar(trade, hi, lo, dt.datetime.now()):
            changed.append(trade)

        # Written for every trade actually evaluated (changed or not), so the
        # next run can tell how much price action it missed. Not written for
        # trades skipped above (closed market, no price): those genuinely were
        # not checked, and pretending otherwise would hide a real gap.
        trade["last_checked_at"] = _now()

    _save(trades)
    for trade in changed:
        _journal_safe(trade)
    return changed


def catch_up_open_positions(now: dt.datetime | None = None) -> CatchUpReport:
    """Replay any price action missed while this runner was stopped.

    Call BEFORE `update_open_positions`. On a healthy runner every open trade
    was checked one cycle ago, `gap_window` returns None for all of them, and
    this costs one timestamp comparison each -- no fetch, no bars, no cost.

    After a real outage it walks the missed window bar by bar through the same
    `apply_bar` the live loop uses, so stops and targets are graded at the
    level and the MINUTE they were actually crossed rather than wherever price
    happened to be at restart.

    Returns a CatchUpReport for the caller to turn into ONE consolidated
    Telegram message. Never raises: a failed catch-up leaves every position
    exactly as it was for the normal live pass.
    """
    trades = _load()
    stocks_market_open = is_market_open()

    def tradeable(trade: dict) -> bool:
        # Mirrors the live loop: stocks are not evaluated outside RTH, so the
        # replay must not resolve them on bars the live system would never
        # have acted on either.
        return not (trade["asset_class"] == "stocks" and not stocks_market_open)

    try:
        report = run_catch_up(
            trades, apply_bar,
            fetch_crypto=get_binance_klines, fetch_stock=get_stock_history,
            config=CATCH_UP_CONFIG, now=now, is_tradeable=tradeable,
        )
    except Exception as exc:  # noqa: BLE001 -- catch-up must never break the cycle
        print(f"[catch-up] pass failed (live positions unaffected): {exc}", file=sys.stderr)
        return CatchUpReport()

    if report.events:
        # Only persist if something actually changed. A no-op catch-up must not
        # rewrite the store.
        _save(trades)
        by_id = {t["id"]: t for t in trades}
        for event in report.events:
            record = by_id.get(event.trade_id)
            if record is not None:
                _journal_safe(record)
    return report


def get_all_trades() -> list[dict]:
    return _load()
