"""Daily/weekly summary generation, send-once state, and Markdown escaping.

The escaping tests exist because this bot has twice had an ENTIRE message
rejected by Telegram with a 400 "can't parse entities" -- once from a
snake_case pattern name (`pin_bar_rejection`) and once from a status
(`PARTIAL_TP1`). An unmatched `_` is not a cosmetic problem here: the alert
simply never arrives.
"""
from __future__ import annotations

import datetime as dt
import json
import os

from core.reporting.summary import (
    build_daily_summary,
    build_weekly_summary,
    daily_report_due,
    load_report_state,
    save_report_state,
    weekly_report_due,
)
from core.utils.telegram_notifier import escape_markdown, escape_markdown_code

LIVNE_CLOSED = ("WIN_TP2", "LOSS_SL", "LOSS_SL_BE", "EMERGENCY_EXIT", "EXPIRED")
LIVNE_UPDATE = ("PARTIAL_TP1",)
NOW = dt.datetime(2026, 8, 15, 21, 30, 0)


def _trade(**kw):
    base = {
        "id": "abc123", "ticker": "BTCUSDT", "asset_class": "crypto", "direction": "LONG",
        "entry_zone": [100.0, 102.0], "stop_loss": 95.0, "target1": 110.0, "target2": 120.0,
        "current_status": "ACTIVE", "signal_date": (NOW - dt.timedelta(hours=2)).isoformat(),
        "entry_date": (NOW - dt.timedelta(hours=1)).isoformat(),
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# Send-once state
# --------------------------------------------------------------------------
def test_report_state_round_trip(tmp_path):
    path = str(tmp_path / "state.json")
    save_report_state(path, {"last_daily_report_date": "2026-08-15"})
    assert load_report_state(path) == {"last_daily_report_date": "2026-08-15"}


def test_missing_or_corrupt_state_is_never_sent(tmp_path):
    """At worst this repeats one report; the alternative -- crashing the live
    loop or suppressing every future summary -- is strictly worse."""
    assert load_report_state(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{ truncated", encoding="utf-8")
    assert load_report_state(str(bad)) == {}


def test_state_write_is_atomic_leaving_no_temp_files(tmp_path):
    path = str(tmp_path / "state.json")
    save_report_state(path, {"a": 1})
    save_report_state(path, {"a": 2})
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
    assert json.loads((tmp_path / "state.json").read_text()) == {"a": 2}


def test_daily_due_uses_at_or_after_hour_not_equality():
    """`>=`, not `==`: a restart or an overrunning cycle must not skip the day
    entirely, and the date marker still keeps it to once."""
    assert daily_report_due({}, dt.datetime(2026, 8, 15, 21, 0), 21) is True
    assert daily_report_due({}, dt.datetime(2026, 8, 15, 23, 59), 21) is True
    assert daily_report_due({}, dt.datetime(2026, 8, 15, 20, 59), 21) is False


def test_daily_not_due_twice_same_day():
    state = {"last_daily_report_date": "2026-08-15"}
    assert daily_report_due(state, NOW, 21) is False
    assert daily_report_due(state, NOW + dt.timedelta(days=1), 21) is True


def test_weekly_due_only_on_configured_weekday():
    friday = dt.datetime(2026, 8, 14, 21, 30)   # weekday() == 4
    assert friday.weekday() == 4
    assert weekly_report_due({}, friday, 21, 4) is True
    assert weekly_report_due({}, friday + dt.timedelta(days=1), 21, 4) is False


# --------------------------------------------------------------------------
# Daily summary
# --------------------------------------------------------------------------
def test_daily_summary_lists_entries_and_closes():
    trades = [
        _trade(id="1", ticker="BTCUSDT"),
        _trade(id="2", ticker="ETHUSDT", current_status="WIN_TP2", pnl_pct=3.5,
               last_status_change=(NOW - dt.timedelta(hours=3)).isoformat()),
    ]
    out = build_daily_summary("Livne model", trades, NOW,
                              closed_statuses=LIVNE_CLOSED, update_statuses=LIVNE_UPDATE)
    assert "New entries* (2)" in out
    assert "Closed / updated* (1)" in out
    assert "BTCUSDT" in out and "ETHUSDT" in out
    assert "Net realized: *+3.50%*" in out


def test_daily_summary_reports_partial_tp1_locked_in_leg():
    """PARTIAL_TP1 is still open so it has no pnl_pct, but it HAS banked its
    first leg -- reporting a blank there hides realized profit. Weighted by
    tp1_close_pct: entry 101, TP1 111.1 is +10%, half closed -> +5.00%."""
    trades = [_trade(id="3", current_status="PARTIAL_TP1", tp1_hit=True,
                     tp1_exit_price=111.1, tp1_close_pct=0.5,
                     last_status_change=(NOW - dt.timedelta(hours=1)).isoformat())]
    out = build_daily_summary("Livne model", trades, NOW,
                              closed_statuses=LIVNE_CLOSED, update_statuses=LIVNE_UPDATE)
    assert "+5.00" in out


def test_daily_summary_window_excludes_older_activity():
    trades = [_trade(id="4", current_status="WIN_TP2", pnl_pct=9.9,
                     last_status_change=(NOW - dt.timedelta(hours=30)).isoformat(),
                     entry_date=(NOW - dt.timedelta(hours=30)).isoformat())]
    out = build_daily_summary("Livne model", trades, NOW,
                              closed_statuses=LIVNE_CLOSED, update_statuses=LIVNE_UPDATE)
    assert out.count("_none_") == 2


def test_daily_summary_empty_is_detectable_as_quiet():
    """The runner decides whether to send using this exact marker count."""
    out = build_daily_summary("Livne model", [], NOW,
                              closed_statuses=LIVNE_CLOSED, update_statuses=LIVNE_UPDATE)
    assert out.count("_none_") == 2


def test_biton_vocabulary_does_not_pick_up_livne_statuses():
    """Passing Biton's sets must not report a Livne-only status as closed."""
    trades = [_trade(id="5", current_status="LOSS_SL_BE", pnl_pct=-0.2,
                     last_status_change=NOW.isoformat())]
    out = build_daily_summary("Biton model", trades, NOW,
                              closed_statuses=("WIN_TP1", "WIN_TP2", "LOSS_SL", "EXPIRED"),
                              update_statuses=())
    assert "LOSS_SL_BE" not in out


# --------------------------------------------------------------------------
# Weekly summary
# --------------------------------------------------------------------------
def test_weekly_summary_aggregates_win_rate_and_net():
    trades = [
        _trade(id="a", current_status="WIN_TP2", pnl_pct=4.0, last_status_change=NOW.isoformat()),
        _trade(id="b", current_status="LOSS_SL", pnl_pct=-2.0, last_status_change=NOW.isoformat()),
        _trade(id="c", current_status="WIN_TP2", pnl_pct=6.0, last_status_change=NOW.isoformat()),
    ]
    out = build_weekly_summary("Livne model", trades, NOW, closed_statuses=LIVNE_CLOSED)
    assert "66.7%" in out       # 2 of 3
    assert "+8.00%" in out      # net


def test_weekly_summary_handles_no_closed_trades():
    out = build_weekly_summary("Livne model", [], NOW, closed_statuses=LIVNE_CLOSED)
    assert "_no closed trades this week_" in out


# --------------------------------------------------------------------------
# Markdown escaping
# --------------------------------------------------------------------------
def test_escape_markdown_neutralises_every_reserved_char():
    assert escape_markdown("pin_bar_rejection") == "pin\\_bar\\_rejection"
    assert escape_markdown("PARTIAL_TP1") == "PARTIAL\\_TP1"
    for ch in ("_", "*", "`", "["):
        assert f"\\{ch}" in escape_markdown(f"x{ch}y")


def test_escape_markdown_code_leaves_underscores_alone():
    """Inside a code span Telegram parses no nested entities, so escaping an
    underscore there would render a visible backslash: BRK_B -> BRK\\_B."""
    assert escape_markdown_code("BRK_B") == "BRK_B"
    assert escape_markdown_code("a`b") == "a\\`b"


def test_summary_rows_are_fenced_so_statuses_need_no_escaping():
    """Data rows go in a fenced block, which Telegram parses literally -- that
    is what makes LOSS_SL_BE and BRK_B safe in the table."""
    trades = [_trade(id="6", ticker="BRK_B", asset_class="stocks",
                     current_status="LOSS_SL_BE", pnl_pct=-1.0,
                     last_status_change=NOW.isoformat())]
    out = build_daily_summary("Livne model", trades, NOW,
                              closed_statuses=LIVNE_CLOSED, update_statuses=LIVNE_UPDATE)
    assert "```" in out
    assert "BRK_B" in out           # literal inside the fence, not escaped
    assert "LOSS_SL_BE" in out


def test_model_name_outside_the_fence_is_escaped():
    out = build_daily_summary("Weird_Model", [], NOW,
                              closed_statuses=LIVNE_CLOSED, update_statuses=LIVNE_UPDATE)
    assert "Weird\\_Model" in out
