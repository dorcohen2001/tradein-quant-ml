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
import json
import os
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import (
    CYCLE_INTERVAL_MINUTES,
    EMERGENCY_CANDLE_LOOKBACK,
    MAX_SLIPPAGE_PCT,
    PAPER_TRADING_STORE,
    PENDING_EXPIRY_HOURS,
    TP1_CLOSE_PCT,
)
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
    if not os.path.exists(PAPER_TRADING_STORE):
        return []
    with open(PAPER_TRADING_STORE, "r", encoding="utf-8") as f:
        try:
            trades = json.load(f)
        except json.JSONDecodeError as exc:
            # A corrupt store must be a hard error: returning [] here would let
            # the next _save() overwrite the file and silently erase all history.
            raise RuntimeError(
                f"{PAPER_TRADING_STORE} is corrupt ({exc}). Fix or restore it "
                f"manually before running again."
            ) from exc
    return [_migrate(t) for t in trades]


def _save(trades: list[dict]) -> None:
    """Atomically replace the store (temp file + os.replace) so a crash
    mid-write can never leave a truncated/corrupt trades.json behind."""
    store_dir = os.path.dirname(PAPER_TRADING_STORE)
    os.makedirs(store_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=store_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(trades, f, indent=2)
        os.replace(tmp_path, PAPER_TRADING_STORE)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


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
    journal.upsert_trade_row(trade_dict)
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


def _pnl_pct(direction: str, entry_price: float, exit_price: float) -> float:
    if direction == "LONG":
        return (exit_price / entry_price - 1) * 100
    return (entry_price / exit_price - 1) * 100


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
                  emergency_reason: str | None = None) -> None:
    trade["current_status"] = status
    trade["exit_date"] = _now()
    trade["exit_price"] = exit_price
    trade["pnl_pct"] = _blended_pnl(trade, entry_price, exit_price)
    if emergency_reason:
        trade["emergency_exit_reason"] = emergency_reason


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
        # last-trade price -- is what every level test below uses, so a level
        # touched between two cycles is still caught. Fetched once per trade
        # and reused by both the PENDING entry test and the ACTIVE stop/target
        # tests.
        rng = get_recent_intraday_range(trade["ticker"], trade["asset_class"], CYCLE_INTERVAL_MINUTES)
        hi, lo = (rng["high"], rng["low"]) if rng is not None else (price, price)

        if status == "PENDING":
            signal_dt = dt.datetime.fromisoformat(trade["signal_date"])
            # Entry-zone touch is an OVERLAP test between the entry zone and
            # the cycle's traded range, not "is the last price inside the
            # zone right now". Entry zones are ~0.7% wide while a cycle is 15
            # minutes, so price routinely traded through the zone and back out
            # before the poll -- with the old point-in-time check those trades
            # stayed PENDING and eventually EXPIRED, silently under-reporting
            # fills and skewing every downstream win-rate metric.
            if lo <= entry_high and hi >= entry_low:
                trade["current_status"] = "ACTIVE"
                trade["entry_date"] = _now()
                changed.append(trade)
            elif (dt.datetime.now() - signal_dt) >= PENDING_EXPIRY:
                trade["current_status"] = "EXPIRED"
                trade["exit_date"] = _now()
                changed.append(trade)
            continue

        # status in (ACTIVE, PARTIAL_TP1): first check for an emergency
        # reversal exit on the (remaining) open position.
        candles = get_recent_intraday_candles(trade["ticker"], trade["asset_class"], EMERGENCY_CANDLE_LOOKBACK + 5)
        if candles is not None:
            is_emergency, reason = detect_emergency_reversal(direction, candles)
            if is_emergency:
                _close_trade(trade, "EMERGENCY_EXIT", price, entry_price, emergency_reason=reason)
                changed.append(trade)
                continue

        stop = trade["stop_loss"]
        hit_stop = lo <= stop if direction == "LONG" else hi >= stop
        hit_tp1 = hi >= trade["target1"] if direction == "LONG" else lo <= trade["target1"]
        hit_tp2 = hi >= trade["target2"] if direction == "LONG" else lo <= trade["target2"]

        if status == "ACTIVE":
            if hit_stop:
                # Same-cycle ambiguity (the intraday range covers both the
                # stop and a target) is resolved conservatively in favor of
                # the stop, regardless of what else the range also touched.
                fill = stop * (1 - MAX_SLIPPAGE_PCT) if direction == "LONG" else stop * (1 + MAX_SLIPPAGE_PCT)
                _close_trade(trade, "LOSS_SL", fill, entry_price)
                changed.append(trade)
            elif hit_tp2:
                # Gapped through both targets in one cycle -- credit both legs.
                trade["tp1_hit"] = True
                trade["tp1_exit_price"] = trade["target1"]
                trade["remaining_size_pct"] = 1 - trade["tp1_close_pct"]
                _close_trade(trade, "WIN_TP2", trade["target2"], entry_price)
                changed.append(trade)
            elif hit_tp1:
                trade["tp1_hit"] = True
                trade["tp1_exit_price"] = trade["target1"]
                trade["remaining_size_pct"] = 1 - trade["tp1_close_pct"]
                trade["stop_loss"] = trade["breakeven_price"]  # Breakeven Adjustment
                trade["current_status"] = "PARTIAL_TP1"
                changed.append(trade)

        elif status == "PARTIAL_TP1":
            if hit_stop:
                fill = stop * (1 - MAX_SLIPPAGE_PCT) if direction == "LONG" else stop * (1 + MAX_SLIPPAGE_PCT)
                _close_trade(trade, "LOSS_SL_BE", fill, entry_price)
                changed.append(trade)
            elif hit_tp2:
                _close_trade(trade, "WIN_TP2", trade["target2"], entry_price)
                changed.append(trade)

    # Stamp every transition in one place. `exit_date` alone is not enough for
    # the daily summary: PARTIAL_TP1 is a status CHANGE but not a close, so it
    # never sets exit_date, and a trade that entered days ago but hit TP1 today
    # would be invisible to a 24-hour reporting window.
    for trade in changed:
        trade["last_status_change"] = _now()

    _save(trades)
    for trade in changed:
        journal.upsert_trade_row(trade)
    return changed


def get_all_trades() -> list[dict]:
    return _load()
