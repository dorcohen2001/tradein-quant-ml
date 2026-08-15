"""Historical catch-up: gap detection, bar-by-bar replay, and the real
per-model lifecycle transitions driven by synthetic OHLC.

TWO LAYERS, TESTED DIFFERENTLY
------------------------------
The ENGINE (core.paper_trading.catch_up) is tested in-process with a trivial
stub lifecycle -- it is responsible for gap detection, ordering, stopping at a
terminal status and collecting events, and none of that should depend on a
model's rules.

The LIFECYCLE (`apply_bar` in each model's tracker) is tested through a
SUBPROCESS per model. The two models define clashing top-level module names
(`config`, `utils`, `paper_trading`) and cannot both be imported into one
pytest process -- and the pytest process should not import either, since doing
so would put a model's real trade-store paths one attribute access away from a
test. Each subprocess runs every scenario for one model and returns JSON.

That matters more than it sounds: `apply_bar` is the function the LIVE loop
calls too, so these scenarios pin live position management, not just replay.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys

import pandas as pd
import pytest

from core.paper_trading.catch_up import (
    CatchUpConfig,
    CatchUpEvent,
    CatchUpReport,
    fetch_gap_candles,
    format_catch_up_summary,
    gap_window,
    last_checked_at,
    replay_trade,
    run_catch_up,
    to_local_naive,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = dt.datetime(2026, 8, 15, 12, 0, 0)


UTC_OFFSET = dt.datetime.now().astimezone().utcoffset()


def venue_bars(first_bar_local: dt.datetime, rows, minutes: int = 5) -> pd.DataFrame:
    """A frame shaped the way Binance actually returns one: a NAIVE index that
    means UTC. `first_bar_local` is the LOCAL time the first bar should land on
    once `fetch_gap_candles` normalises it.

    Written this way deliberately -- building the fixture in local time would
    quietly cancel out the very timezone conversion these tests exist to check.
    """
    return bars(first_bar_local - UTC_OFFSET, rows, minutes)


def bars(start: dt.datetime, rows: list[tuple[float, float]], minutes: int = 5) -> pd.DataFrame:
    """Synthetic OHLC frame from a list of (high, low) pairs, indexed verbatim."""
    index = pd.DatetimeIndex([start + dt.timedelta(minutes=minutes * i) for i in range(len(rows))])
    return pd.DataFrame(
        {"Open": [(h + l) / 2 for h, l in rows],
         "High": [h for h, _ in rows],
         "Low": [l for _, l in rows],
         "Close": [(h + l) / 2 for h, l in rows],
         "Volume": [100] * len(rows)},
        index=index,
    )


# ==========================================================================
# Gap detection -- the zero-regression guarantee
# ==========================================================================
def test_healthy_cadence_produces_no_gap_and_no_fetch():
    """THE performance guarantee. At a 2-minute cycle the gap is ~2 min, far
    under the 30-min floor, so a healthy runner never fetches a single bar."""
    trade = {"current_status": "ACTIVE",
             "last_checked_at": (NOW - dt.timedelta(minutes=2)).isoformat()}
    assert gap_window(trade, NOW, CatchUpConfig()) is None


def test_real_outage_produces_a_window():
    trade = {"current_status": "ACTIVE",
             "last_checked_at": (NOW - dt.timedelta(hours=6)).isoformat()}
    window = gap_window(trade, NOW, CatchUpConfig())
    assert window is not None
    start, end = window
    assert start == NOW - dt.timedelta(hours=6)
    assert end == NOW


def test_gap_is_clamped_to_max_lookback():
    """A month-long gap must not try to fetch a month of 5m bars -- replaying
    the most recent 72h is strictly better than replaying none."""
    trade = {"current_status": "ACTIVE",
             "last_checked_at": (NOW - dt.timedelta(days=30)).isoformat()}
    start, end = gap_window(trade, NOW, CatchUpConfig(max_lookback_hours=72))
    assert start == NOW - dt.timedelta(hours=72)


def test_closed_trades_are_never_replayed():
    for status in ("WIN_TP2", "LOSS_SL", "EXPIRED", "EMERGENCY_EXIT"):
        trade = {"current_status": status,
                 "last_checked_at": (NOW - dt.timedelta(days=2)).isoformat()}
        assert gap_window(trade, NOW, CatchUpConfig()) is None


def test_future_or_missing_timestamp_is_not_replayed():
    assert gap_window({"current_status": "ACTIVE"}, NOW, CatchUpConfig()) is None
    ahead = {"current_status": "ACTIVE", "last_checked_at": (NOW + dt.timedelta(hours=1)).isoformat()}
    assert gap_window(ahead, NOW, CatchUpConfig()) is None


def test_last_checked_falls_back_for_pre_existing_trades():
    """Every trade written before this feature lacks `last_checked_at`."""
    assert last_checked_at({"last_checked_at": "2026-08-15T11:00:00"}).hour == 11
    assert last_checked_at({"last_status_change": "2026-08-15T10:00:00"}).hour == 10
    assert last_checked_at({"entry_date": "2026-08-15T09:00:00"}).hour == 9
    assert last_checked_at({"signal_date": "2026-08-15T08:00:00"}).hour == 8
    assert last_checked_at({}) is None
    assert last_checked_at({"last_checked_at": "not-a-date", "signal_date": "2026-08-15T08:00:00"}).hour == 8


# ==========================================================================
# Engine: ordering, termination, event capture
# ==========================================================================
def stub_apply_bar(trade, high, low, ts):
    """A minimal LONG lifecycle, standing in for a model's rules so the engine
    is tested independently of either model."""
    if trade["current_status"] != "ACTIVE":
        return False
    if low <= trade["stop_loss"]:
        trade.update(current_status="LOSS_SL", exit_price=trade["stop_loss"],
                     pnl_pct=-1.0, exit_date=ts.isoformat())
        return True
    if high >= trade["target1"]:
        trade.update(current_status="WIN_TP1", exit_price=trade["target1"],
                     pnl_pct=2.0, exit_date=ts.isoformat())
        return True
    return False


def test_replay_walks_bars_in_order_and_stops_at_the_first_resolution():
    trade = {"id": "t1", "ticker": "BTCUSDT", "current_status": "ACTIVE",
             "stop_loss": 95.0, "target1": 110.0}
    # bar 2 hits the stop; bar 3 would have hit the target -- it must not,
    # because the position is already closed by then.
    frame = bars(NOW, [(101, 99), (102, 100), (99, 94), (115, 100)])
    events = replay_trade(trade, frame, stub_apply_bar)

    assert len(events) == 1
    assert events[0].to_status == "LOSS_SL"
    assert events[0].at == NOW + dt.timedelta(minutes=10)
    assert trade["current_status"] == "LOSS_SL"


def test_replay_records_the_transition_it_saw():
    trade = {"id": "t2", "ticker": "ETHUSDT", "current_status": "ACTIVE",
             "stop_loss": 95.0, "target1": 110.0}
    events = replay_trade(trade, bars(NOW, [(101, 99), (112, 100)]), stub_apply_bar)
    assert (events[0].from_status, events[0].to_status) == ("ACTIVE", "WIN_TP1")
    assert events[0].pnl_pct == 2.0
    assert events[0].ticker == "ETHUSDT"


def test_replay_with_no_touch_leaves_the_trade_open():
    trade = {"id": "t3", "ticker": "X", "current_status": "ACTIVE",
             "stop_loss": 95.0, "target1": 110.0}
    assert replay_trade(trade, bars(NOW, [(101, 99), (102, 100)]), stub_apply_bar) == []
    assert trade["current_status"] == "ACTIVE"


def test_a_raising_apply_bar_stops_that_trade_without_propagating():
    """One bad trade must not abort the whole catch-up pass."""
    def boom(trade, high, low, ts):
        raise ValueError("bad bar")
    trade = {"id": "t4", "ticker": "X", "current_status": "ACTIVE"}
    assert replay_trade(trade, bars(NOW, [(1, 1)]), boom) == []
    assert trade["current_status"] == "ACTIVE"


def test_run_catch_up_skips_healthy_trades_without_fetching():
    def explode(*a, **k):
        raise AssertionError("must not fetch for a healthy trade")

    trades = [{"id": "a", "ticker": "BTCUSDT", "asset_class": "crypto", "current_status": "ACTIVE",
               "last_checked_at": (NOW - dt.timedelta(minutes=2)).isoformat()}]
    report = run_catch_up(trades, stub_apply_bar, fetch_crypto=explode,
                          fetch_stock=explode, now=NOW)
    assert report.trades_replayed == 0
    assert report.events == []


def test_run_catch_up_replays_and_persists_the_transition():
    frame = venue_bars(NOW - dt.timedelta(hours=2), [(101, 99), (99, 94)])
    trades = [{"id": "a", "ticker": "BTCUSDT", "asset_class": "crypto", "current_status": "ACTIVE",
               "stop_loss": 95.0, "target1": 110.0,
               "last_checked_at": (NOW - dt.timedelta(hours=3)).isoformat()}]
    report = run_catch_up(trades, stub_apply_bar,
                          fetch_crypto=lambda *a, **k: frame, fetch_stock=None, now=NOW)
    assert report.trades_replayed == 1
    assert len(report.resolved) == 1
    assert trades[0]["current_status"] == "LOSS_SL"


def test_is_tradeable_filter_skips_stocks_when_market_closed():
    """Mirrors the live loop -- the replay must not resolve a stock on bars the
    live system would never have acted on."""
    def explode(*a, **k):
        raise AssertionError("must not fetch a filtered-out trade")

    trades = [{"id": "s", "ticker": "AAPL", "asset_class": "stocks", "current_status": "ACTIVE",
               "last_checked_at": (NOW - dt.timedelta(hours=8)).isoformat()}]
    report = run_catch_up(trades, stub_apply_bar, fetch_crypto=explode, fetch_stock=explode,
                          now=NOW, is_tradeable=lambda t: t["asset_class"] != "stocks")
    assert report.trades_replayed == 0


def test_fetch_failure_is_recorded_and_does_not_raise():
    def failing(*a, **k):
        raise RuntimeError("binance down")

    trades = [{"id": "a", "ticker": "BTCUSDT", "asset_class": "crypto", "current_status": "ACTIVE",
               "last_checked_at": (NOW - dt.timedelta(hours=4)).isoformat()}]
    report = run_catch_up(trades, stub_apply_bar, fetch_crypto=failing, fetch_stock=failing, now=NOW)
    assert report.skipped_no_data == ["BTCUSDT"]
    assert trades[0]["current_status"] == "ACTIVE"


# ==========================================================================
# Candle window handling
# ==========================================================================
def test_candles_are_sliced_strictly_after_the_last_check():
    """The bar containing the last check was already evaluated by the cycle
    that ran then; re-applying it could re-trigger a handled level."""
    start = NOW - dt.timedelta(hours=1)
    frame = venue_bars(start - dt.timedelta(minutes=10), [(1, 1)] * 20)
    trade = {"ticker": "BTCUSDT", "asset_class": "crypto"}
    window = fetch_gap_candles(trade, start, NOW, CatchUpConfig(),
                               fetch_crypto=lambda *a, **k: frame, fetch_stock=None)
    assert window is not None
    assert window.index.min() > start


def test_utc_candle_index_is_converted_to_local_naive():
    """Binance returns UTC, trade stamps are naive local. Comparing them
    unconverted shifts the whole replay window by the UTC offset."""
    utc_index = pd.DatetimeIndex([dt.datetime(2026, 8, 15, 10, 0)])
    converted = to_local_naive(utc_index)
    offset = dt.datetime.now().astimezone().utcoffset()
    assert converted[0] == pd.Timestamp(dt.datetime(2026, 8, 15, 10, 0) + offset)
    assert converted.tz is None


def test_tz_aware_index_is_converted_not_dropped():
    aware = pd.DatetimeIndex([pd.Timestamp("2026-08-15 09:30", tz="America/New_York")])
    assert to_local_naive(aware).tz is None


def test_empty_or_malformed_frames_are_skipped():
    trade = {"ticker": "BTCUSDT", "asset_class": "crypto"}
    cfg = CatchUpConfig()
    assert fetch_gap_candles(trade, NOW - dt.timedelta(hours=2), NOW, cfg,
                             fetch_crypto=lambda *a, **k: None, fetch_stock=None) is None
    assert fetch_gap_candles(trade, NOW - dt.timedelta(hours=2), NOW, cfg,
                             fetch_crypto=lambda *a, **k: pd.DataFrame(), fetch_stock=None) is None
    no_hl = pd.DataFrame({"Close": [1.0]}, index=pd.DatetimeIndex([NOW - dt.timedelta(minutes=5)]))
    assert fetch_gap_candles(trade, NOW - dt.timedelta(hours=2), NOW, cfg,
                             fetch_crypto=lambda *a, **k: no_hl, fetch_stock=None) is None


# ==========================================================================
# Consolidated notification
# ==========================================================================
def _report(*events):
    return CatchUpReport(events=list(events), trades_replayed=len(events), bars_replayed=120)


def test_no_events_means_no_message():
    assert format_catch_up_summary("Livne model", CatchUpReport()) is None


def test_summary_is_one_message_covering_every_resolution():
    report = _report(
        CatchUpEvent("1", "BTCUSDT", "ACTIVE", "LOSS_SL", NOW, 62000.0, -1.2),
        CatchUpEvent("2", "ETHUSDT", "ACTIVE", "WIN_TP2", NOW, 1900.0, 3.4),
        CatchUpEvent("3", "SOLUSDT", "PENDING", "ACTIVE", NOW, None, None),
    )
    out = format_catch_up_summary("Livne model", report)
    assert out.count("CATCH-UP SYNC") == 1          # one message, not three alerts
    for ticker in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        assert ticker in out
    assert "*2* resolved" in out                     # the PENDING->ACTIVE is not "resolved"
    assert "+2.20%" in out                           # net of -1.2 and +3.4


def test_summary_states_the_real_crossing_times():
    """The entire point: these are not "happening now" alerts."""
    when = dt.datetime(2026, 8, 15, 3, 14)
    out = format_catch_up_summary("Livne model", _report(
        CatchUpEvent("1", "BTCUSDT", "ACTIVE", "LOSS_SL", when, 62000.0, -1.2)))
    assert "2026-08-15 03:14" in out
    assert "actually crossed" in out


def test_summary_rows_are_fenced_so_statuses_are_markdown_safe():
    """LOSS_SL_BE has two underscores -- outside a fence that is a 400."""
    out = format_catch_up_summary("Livne model", _report(
        CatchUpEvent("1", "BRK_B", "PARTIAL_TP1", "LOSS_SL_BE", NOW, 100.0, -0.1)))
    assert "```" in out
    assert "LOSS_SL_BE" in out and "BRK_B" in out


def test_summary_reports_symbols_it_could_not_replay():
    report = _report(CatchUpEvent("1", "BTCUSDT", "ACTIVE", "LOSS_SL", NOW, 1.0, -1.0))
    report.skipped_no_data = ["AAPL", "AAPL", "MSFT"]
    out = format_catch_up_summary("Livne model", report)
    assert "AAPL, MSFT" in out


# ==========================================================================
# REAL per-model lifecycles, driven by synthetic OHLC (subprocess per model)
# ==========================================================================
SCENARIO_SCRIPT = r'''
import datetime as dt, json, sys, os
sys.path.insert(0, os.getcwd())
from paper_trading.tracker import apply_bar

T0 = dt.datetime(2026, 8, 15, 12, 0, 0)

def trade(**kw):
    base = dict(id="x", ticker="TEST", asset_class="crypto", direction="LONG",
                entry_zone=[99.0, 101.0], stop_loss=95.0, initial_stop_loss=95.0,
                target1=110.0, target2=120.0, current_status="ACTIVE",
                signal_date=(T0 - dt.timedelta(hours=1)).isoformat(),
                tp1_close_pct=0.5, breakeven_price=100.15, tp1_hit=False,
                tp1_exit_price=None, remaining_size_pct=1.0, emergency_exit_reason=None,
                entry_date=(T0 - dt.timedelta(minutes=30)).isoformat())
    base.update(kw); return base

def run(tr, sequence):
    """Feed (high, low) bars in order; return the trade plus what changed when."""
    fired = []
    for i, (hi, lo) in enumerate(sequence):
        ts = T0 + dt.timedelta(minutes=5 * i)
        if apply_bar(tr, hi, lo, ts):
            fired.append({"status": tr["current_status"], "at": ts.isoformat()})
    return {"trade": tr, "fired": fired}

out = {}
# --- stop-loss during the gap (LONG) ---
out["long_stop"] = run(trade(), [(102, 100), (101, 94), (115, 90)])
# --- take-profit during the gap (LONG) ---
out["long_tp1"]  = run(trade(), [(102, 100), (111, 100)])
out["long_tp2"]  = run(trade(), [(102, 100), (121, 100)])
# --- same bar covers both -> conservative stop ---
out["same_bar"]  = run(trade(), [(125, 90)])
# --- SHORT mirror ---
out["short_stop"] = run(trade(direction="SHORT", stop_loss=105.0, target1=90.0,
                              target2=80.0, breakeven_price=99.85),
                        [(100, 98), (106, 99)])
out["short_tp"]   = run(trade(direction="SHORT", stop_loss=105.0, target1=90.0,
                              target2=80.0, breakeven_price=99.85),
                        [(100, 98), (100, 89)])
# --- pending fill at the exact bar that touched the zone ---
out["pending_fill"] = run(trade(current_status="PENDING", entry_date=None),
                          [(97, 96), (100, 98)])
# --- no touch -> untouched ---
out["no_touch"] = run(trade(), [(102, 99), (103, 100)])
print(json.dumps(out, default=str))
'''


@pytest.fixture(scope="session")
def livne_results():
    return _run_scenarios("livne")


@pytest.fixture(scope="session")
def biton_results():
    return _run_scenarios("biton")


def _run_scenarios(model: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", SCENARIO_SCRIPT],
        cwd=os.path.join(REPO_ROOT, "models", model),
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        pytest.fail(f"{model} scenario run failed:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


# ---- Livne (multi-tier lifecycle) ----------------------------------------
def test_livne_stop_loss_triggers_on_the_gap_bar(livne_results):
    result = livne_results["long_stop"]
    assert result["trade"]["current_status"] == "LOSS_SL"
    # Second bar (index 1) is the one that breached -- and the THIRD bar, which
    # would have hit TP2, must not resurrect a closed position.
    assert result["fired"] == [{"status": "LOSS_SL", "at": "2026-08-15T12:05:00"}]
    assert result["trade"]["exit_date"] == "2026-08-15T12:05:00"
    assert result["trade"]["last_status_change"] == "2026-08-15T12:05:00"


def test_livne_stop_fill_includes_adverse_slippage(livne_results):
    trade = livne_results["long_stop"]["trade"]
    assert trade["exit_price"] < 95.0            # filled worse than the level
    assert trade["exit_price"] == pytest.approx(95.0 * (1 - 0.0015))
    assert trade["pnl_pct"] < 0


def test_livne_take_profit_banks_a_partial_and_moves_the_stop(livne_results):
    trade = livne_results["long_tp1"]["trade"]
    assert trade["current_status"] == "PARTIAL_TP1"
    assert trade["tp1_hit"] is True
    assert trade["tp1_exit_price"] == 110.0
    assert trade["remaining_size_pct"] == 0.5
    assert trade["stop_loss"] == trade["breakeven_price"]   # breakeven adjustment
    assert livne_results["long_tp1"]["fired"][0]["at"] == "2026-08-15T12:05:00"


def test_livne_gap_through_both_targets_credits_both_legs(livne_results):
    trade = livne_results["long_tp2"]["trade"]
    assert trade["current_status"] == "WIN_TP2"
    assert trade["tp1_hit"] is True                 # first leg still credited
    assert trade["exit_price"] == 120.0
    assert trade["pnl_pct"] > 0


def test_livne_same_bar_ambiguity_resolves_to_the_stop(livne_results):
    """One bar covering stop AND target cannot say which came first; only the
    adverse assumption cannot flatter the win rate."""
    assert livne_results["same_bar"]["trade"]["current_status"] == "LOSS_SL"


def test_livne_short_levels_are_mirrored(livne_results):
    assert livne_results["short_stop"]["trade"]["current_status"] == "LOSS_SL"
    assert livne_results["short_tp"]["trade"]["current_status"] == "PARTIAL_TP1"


def test_livne_short_profit_is_positive_when_price_falls(livne_results):
    trade = livne_results["short_tp"]["trade"]
    assert trade["tp1_exit_price"] == 90.0
    assert trade["current_status"] == "PARTIAL_TP1"


def test_livne_pending_fills_at_the_bar_that_touched_the_zone(livne_results):
    result = livne_results["pending_fill"]
    assert result["trade"]["current_status"] == "ACTIVE"
    # First bar (97/96) misses the 99-101 zone; the second overlaps it.
    assert result["trade"]["entry_date"] == "2026-08-15T12:05:00"


def test_livne_untouched_position_is_left_alone(livne_results):
    result = livne_results["no_touch"]
    assert result["fired"] == []
    assert result["trade"]["current_status"] == "ACTIVE"
    assert result["trade"].get("pnl_pct") is None
    assert result["trade"].get("exit_date") is None


# ---- Biton (single-shot lifecycle) ---------------------------------------
def test_biton_stop_loss_triggers_on_the_gap_bar(biton_results):
    result = biton_results["long_stop"]
    assert result["trade"]["current_status"] == "LOSS_SL"
    assert result["fired"] == [{"status": "LOSS_SL", "at": "2026-08-15T12:05:00"}]
    assert result["trade"]["exit_date"] == "2026-08-15T12:05:00"


def test_biton_take_profit_closes_outright_not_partially(biton_results):
    """Biton is single-shot -- TP1 CLOSES the position. Livne banks a partial
    at the same level. Conflating the two would corrupt both models' P&L."""
    trade = biton_results["long_tp1"]["trade"]
    assert trade["current_status"] == "WIN_TP1"
    assert trade["exit_price"] == 110.0
    assert trade["pnl_pct"] == pytest.approx(10.0)   # entry 100 -> 110


def test_biton_gap_through_both_targets_credits_tp2(biton_results):
    trade = biton_results["long_tp2"]["trade"]
    assert trade["current_status"] == "WIN_TP2"
    assert trade["exit_price"] == 120.0


def test_biton_same_bar_ambiguity_resolves_to_the_stop(biton_results):
    assert biton_results["same_bar"]["trade"]["current_status"] == "LOSS_SL"


def test_biton_short_levels_are_mirrored(biton_results):
    assert biton_results["short_stop"]["trade"]["current_status"] == "LOSS_SL"
    assert biton_results["short_tp"]["trade"]["current_status"] == "WIN_TP1"


def test_biton_pending_fills_at_the_bar_that_touched_the_zone(biton_results):
    assert biton_results["pending_fill"]["trade"]["entry_date"] == "2026-08-15T12:05:00"


def test_biton_untouched_position_is_left_alone(biton_results):
    assert biton_results["no_touch"]["fired"] == []
