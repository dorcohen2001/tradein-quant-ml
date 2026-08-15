"""Intrabar execution primitives: P&L, slippage, level detection, tie-breaks.

The single idea this module protects: a stop or target is triggered by a
candle's HIGH/LOW, never its Close. A close-only simulator calls a bar that
wicked through the stop a non-event and reports a win that never happened.
"""
from __future__ import annotations

import pandas as pd
import pytest

from core.backtesting.engine import (
    ExcursionTracker,
    apply_slippage,
    duration_breakdown,
    hits,
    pnl_pct,
)
from core.backtesting.engine import resolve_same_bar


def bar(o, h, l, c, v=1000):
    return pd.Series({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v})


# --------------------------------------------------------------------------
# P&L
# --------------------------------------------------------------------------
@pytest.mark.parametrize("direction,entry,exit_,expected", [
    ("LONG", 100.0, 110.0, 10.0),
    ("LONG", 100.0, 90.0, -10.0),
    ("SHORT", 100.0, 90.0, 11.11111111),   # shorts gain as price falls
    ("SHORT", 100.0, 110.0, -9.09090909),
])
def test_pnl_pct(direction, entry, exit_, expected):
    assert pnl_pct(direction, entry, exit_) == pytest.approx(expected, rel=1e-6)


def test_pnl_pct_guards_bad_prices_instead_of_raising():
    """A feed returning 0.0 must not raise inside position management."""
    assert pnl_pct("LONG", 0.0, 10.0) == 0.0
    assert pnl_pct("SHORT", 100.0, 0.0) == 0.0


def test_live_and_backtest_pnl_agree():
    """A trade must score the same live as it does in a backtest."""
    from core.paper_trading.store import pnl_pct as live_pnl
    for d, e, x in (("LONG", 100, 107.5), ("SHORT", 250, 240), ("LONG", 64000, 63000)):
        assert live_pnl(d, e, x) == pytest.approx(pnl_pct(d, e, x))


# --------------------------------------------------------------------------
# Slippage
# --------------------------------------------------------------------------
def test_slippage_is_always_adverse_to_the_position():
    """A stop becoming a market order fills WORSE, in the direction that hurts:
    below the level for a long, above it for a short."""
    assert apply_slippage(100.0, "LONG", 0.0015) == pytest.approx(99.85)
    assert apply_slippage(100.0, "SHORT", 0.0015) == pytest.approx(100.15)


def test_slippage_can_be_favourable_when_asked():
    assert apply_slippage(100.0, "LONG", 0.0015, adverse=False) == pytest.approx(100.15)


# --------------------------------------------------------------------------
# Level detection -- High/Low, never Close
# --------------------------------------------------------------------------
def test_stop_triggered_by_wick_even_when_close_recovers():
    """Opens 100, wicks to 97, closes 101. A stop at 98 WAS hit."""
    row = bar(100, 101.5, 97, 101)
    assert hits(row, "LONG", 98.0, "stop") is True
    assert row["Close"] > 98.0  # a close-only check would have missed it


def test_target_triggered_by_wick():
    row = bar(100, 112, 99, 101)
    assert hits(row, "LONG", 110.0, "target") is True


def test_short_levels_are_mirrored():
    row = bar(100, 106, 99, 100)
    assert hits(row, "SHORT", 105.0, "stop") is True    # stop ABOVE for a short
    assert hits(row, "SHORT", 98.0, "target") is False  # target BELOW, not reached


def test_untouched_level_is_not_a_hit():
    row = bar(100, 101, 99, 100)
    assert hits(row, "LONG", 98.0, "stop") is False
    assert hits(row, "LONG", 110.0, "target") is False


# --------------------------------------------------------------------------
# Same-bar ambiguity
# --------------------------------------------------------------------------
def test_same_bar_ambiguity_defaults_to_the_stop():
    """One bar covering both levels cannot say which came first. Assuming the
    adverse outcome is the only choice that cannot flatter the win rate."""
    assert resolve_same_bar(None, "BTCUSDT", "crypto", pd.Timestamp("2026-08-01"),
                            "LONG", stop=98.0, target=110.0) == "stop"


def test_finer_bars_resolve_ambiguity_in_favour_of_whichever_came_first():
    class Loader:
        def __call__(self, symbol, asset_class, start, end):
            idx = pd.date_range(start, periods=3, freq="1min")
            # target touched on the first finer bar, stop only on the third
            return pd.DataFrame(
                {"Open": [100, 100, 100], "High": [110, 101, 101],
                 "Low": [99, 99, 97], "Close": [109, 100, 98], "Volume": [1, 1, 1]},
                index=idx,
            )

    from core.backtesting.engine import FinerBarTieBreaker
    tb = FinerBarTieBreaker(loader=Loader(), interval_delta=pd.Timedelta("15min"))
    verdict = resolve_same_bar(tb, "BTCUSDT", "crypto", pd.Timestamp("2026-08-01"),
                               "LONG", stop=97.5, target=110.0)
    assert verdict == "target"
    assert tb.resolved == 1


def test_tie_breaker_falls_back_to_stop_when_finer_data_missing():
    """Very common for stocks -- yfinance serves 1m for only ~30 days."""
    from core.backtesting.engine import FinerBarTieBreaker
    tb = FinerBarTieBreaker(loader=lambda *a, **k: None, interval_delta=pd.Timedelta("15min"))
    verdict = resolve_same_bar(tb, "AAPL", "stocks", pd.Timestamp("2020-01-01"),
                               "LONG", stop=98.0, target=110.0)
    assert verdict == "stop"
    assert tb.unresolved == 1


# --------------------------------------------------------------------------
# Excursion tracking
# --------------------------------------------------------------------------
def test_excursion_tracks_worst_and_best_unrealised_points():
    tracker = ExcursionTracker(direction="LONG", entry_price=100.0)
    tracker.update(pd.Timestamp("2026-08-01 10:00"), bar(100, 103, 99, 102))
    tracker.update(pd.Timestamp("2026-08-01 10:15"), bar(102, 105, 96, 97))
    assert tracker.mfe_pct == pytest.approx(5.0)
    assert tracker.mae_pct == pytest.approx(-4.0)
    assert tracker.mae_time == pd.Timestamp("2026-08-01 10:15")


def test_duration_breakdown_reports_all_three_units():
    out = duration_breakdown(pd.Timestamp("2026-08-01 10:00"), pd.Timestamp("2026-08-01 13:30"))
    assert out["minutes"] == pytest.approx(210.0)
    assert out["hours"] == pytest.approx(3.5)


def test_duration_breakdown_handles_open_position():
    assert duration_breakdown(None, None) == {"minutes": 0.0, "hours": 0.0, "days": 0.0}
