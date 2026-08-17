"""Horizon's calibrations, driven through its REAL code in a subprocess.

Horizon is a recalibration of Livne, and the only thing that makes a
recalibration meaningful is that the numbers actually changed the behaviour.
Asserting the constants would prove nothing -- a constant nothing reads is
just a comment. So every test here drives the model's own functions and
observes the decision.

The subprocess is mandatory, not stylistic: `models/horizon` and
`models/livne` both define top-level `config`, `ta`, `risk` and
`paper_trading`, so pytest can never import them into one interpreter. This is
the same harness `tests/test_catch_up.py` uses to exercise both live models'
real `apply_bar`.

These are MODEL tests, deliberately separate from the platform contract in
`tests/test_model_contract.py`: that suite asserts what every tenant must do,
this one asserts what this tenant was asked to do differently.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HORIZON = os.path.join(REPO_ROOT, "models", "horizon")
LIVNE = os.path.join(REPO_ROOT, "models", "livne")


def _run(script: str, cwd: str) -> dict:
    """Execute a snippet inside a model's own sys.path root and read its JSON."""
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=cwd, capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        pytest.fail(f"harness failed in {os.path.basename(cwd)}:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


# ==========================================================================
# 1. Entry quality: two-bar formations only
# ==========================================================================
_FORMATION_PROBE = '''
    import json, sys
    sys.path.insert(0, ".")
    import pandas as pd
    from ta.candles import detect_reversal_formation

    def frame(rows):
        return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"])

    # A textbook bullish HAMMER: small body at the top, long lower wick.
    hammer = frame([
        {"Open": 100, "High": 101, "Low": 99.5, "Close": 100.5, "Volume": 1000},
        {"Open": 100, "High": 101, "Low": 99.5, "Close": 100.5, "Volume": 1000},
        {"Open": 100.2, "High": 100.6, "Low": 97.0, "Close": 100.4, "Volume": 1500},
    ])
    # A textbook bullish ENGULFING: down bar, then a bar that engulfs it.
    engulfing = frame([
        {"Open": 100, "High": 101, "Low": 99.5, "Close": 100.5, "Volume": 1000},
        {"Open": 100.5, "High": 100.8, "Low": 99.0, "Close": 99.2, "Volume": 1000},
        {"Open": 99.0, "High": 102.0, "Low": 98.9, "Close": 101.5, "Volume": 1800},
    ])

    out = {}
    for name, df in (("hammer", hammer), ("engulfing", engulfing)):
        found, pattern, conviction, _offset = detect_reversal_formation(df, "LONG")
        out[name] = {"found": bool(found), "pattern": pattern, "conviction": conviction}
    print(json.dumps(out))
'''


def test_horizon_rejects_single_bar_formations():
    """The calibration: hammers, shooting stars and pin bars no longer trigger.

    They are the cheapest formations to print on a 15m chart, carry the lowest
    conviction weights, and are the population most associated with the
    LOSS_SL drag this model exists to reduce.
    """
    out = _run(_FORMATION_PROBE, HORIZON)
    assert out["hammer"]["found"] is False, \
        f"a hammer still triggers Horizon: {out['hammer']}"


def test_horizon_still_accepts_two_bar_formations():
    """A filter that rejected everything would also 'reduce losses'."""
    out = _run(_FORMATION_PROBE, HORIZON)
    assert out["engulfing"]["found"] is True
    assert out["engulfing"]["pattern"] in ("engulfing", "doji_then_engulfing")
    assert out["engulfing"]["conviction"] >= 0.85


def test_livne_still_accepts_the_hammer_it_always_did():
    """THE isolation guarantee, asserted rather than assumed: the same input
    through Livne's own code must behave exactly as it did before Horizon
    existed."""
    out = _run(_FORMATION_PROBE, LIVNE)
    assert out["hammer"]["found"] is True, "Horizon's calibration leaked into Livne"
    assert out["hammer"]["pattern"] == "hammer"


# ==========================================================================
# 2. Per-asset-class risk
# ==========================================================================
_RISK_PROBE = '''
    import json, sys
    sys.path.insert(0, ".")
    from config import risk_params, emergency_volume_mult
    from risk.risk_manager import liquidity_sweep_buffer

    out = {
        "crypto": risk_params("crypto"),
        "stocks": risk_params("stocks"),
        "unknown": risk_params("forex"),
        "buffer_crypto": liquidity_sweep_buffer(100.0, 1.0, "crypto"),
        "buffer_stocks": liquidity_sweep_buffer(100.0, 1.0, "stocks"),
        "buffer_default": liquidity_sweep_buffer(100.0, 1.0),
        "emergency_crypto": emergency_volume_mult("crypto"),
        "emergency_stocks": emergency_volume_mult("stocks"),
    }
    print(json.dumps(out))
'''


def test_crypto_gets_a_wider_stop_buffer_than_stocks():
    """Crypto wicks further inside one 15m bar; an equity-sized cushion sits
    inside its ordinary noise and gets swept by moves that were never a
    reversal."""
    out = _run(_RISK_PROBE, HORIZON)
    assert out["buffer_crypto"] > out["buffer_stocks"]
    assert out["crypto"]["stop_atr_buffer_mult"] == 0.45
    assert out["stocks"]["stop_atr_buffer_mult"] == 0.25


def test_crypto_tp1_is_pulled_in_to_match_the_wider_stop():
    """A wider stop with an unchanged target band pushes R:R under the floor
    and starves the funnel -- the calibration has to move both."""
    out = _run(_RISK_PROBE, HORIZON)
    assert out["crypto"]["tp1_max_rr"] < out["stocks"]["tp1_max_rr"]
    assert out["crypto"]["stop_max_atr_mult"] > out["stocks"]["stop_max_atr_mult"], \
        "the distance cap must widen with the buffer, or it undoes it"


def test_stocks_keep_livne_s_tighter_structure():
    out = _run(_RISK_PROBE, HORIZON)
    assert out["stocks"] == {"stop_atr_buffer_mult": 0.25, "tp1_min_rr": 1.0,
                             "tp1_max_rr": 2.0, "stop_max_atr_mult": 2.5}


def test_an_unknown_asset_class_falls_back_to_the_conservative_profile():
    """An unrecognised class must never raise inside position management."""
    out = _run(_RISK_PROBE, HORIZON)
    assert out["unknown"] == out["stocks"]


def test_emergency_exit_is_calibrated_per_venue():
    """Crypto is spiky around the clock, so a shared 3.0x fires inside healthy
    trends and cuts winners; equities concentrate volume into the open and
    close, where the same ratio is rarer and more meaningful."""
    out = _run(_RISK_PROBE, HORIZON)
    assert out["emergency_crypto"] > out["emergency_stocks"]
    assert out["emergency_crypto"] == 4.5
    assert out["emergency_stocks"] == 2.6


# ==========================================================================
# 3. Session filter
# ==========================================================================
_SESSION_PROBE = '''
    import json, sys, datetime as dt
    from zoneinfo import ZoneInfo
    sys.path.insert(0, ".")
    from utils.session import entries_blocked

    ET = ZoneInfo("America/New_York")
    def at(hour, minute):
        return dt.datetime(2026, 8, 17, hour, minute, tzinfo=ET)

    out = {}
    for label, when in (("open_0945", at(9, 45)), ("pre_1129", at(11, 29)),
                        ("trough_1200", at(12, 0)), ("edge_1130", at(11, 30)),
                        ("edge_1330", at(13, 30)), ("afternoon_1500", at(15, 0))):
        blocked_stock, reason = entries_blocked("stocks", when)
        blocked_crypto, _ = entries_blocked("crypto", when)
        out[label] = {"stocks": blocked_stock, "crypto": blocked_crypto, "reason": reason}
    print(json.dumps(out))
'''


def test_equity_entries_are_blocked_in_the_midday_trough():
    out = _run(_SESSION_PROBE, HORIZON)
    assert out["trough_1200"]["stocks"] is True
    assert "midday" in out["trough_1200"]["reason"]


def test_the_window_boundaries_are_inclusive_start_exclusive_end():
    """13:30 belongs to the afternoon, not to the trough."""
    out = _run(_SESSION_PROBE, HORIZON)
    assert out["edge_1130"]["stocks"] is True
    assert out["edge_1330"]["stocks"] is False


def test_the_rest_of_the_session_is_untouched():
    out = _run(_SESSION_PROBE, HORIZON)
    assert out["open_0945"]["stocks"] is False
    assert out["pre_1129"]["stocks"] is False
    assert out["afternoon_1500"]["stocks"] is False


def test_crypto_is_never_session_gated():
    """It has no session, and no equivalent structural trough."""
    out = _run(_SESSION_PROBE, HORIZON)
    assert all(v["crypto"] is False for v in out.values())


# ==========================================================================
# 4. Stagnation exit
# ==========================================================================
_STAGNATION_PROBE = '''
    import json, sys, datetime as dt
    sys.path.insert(0, ".")
    from paper_trading.tracker import apply_bar

    T0 = dt.datetime(2026, 8, 17, 12, 0, 0)

    def trade(**kw):
        base = dict(id="s1", ticker="TEST", asset_class="crypto", direction="LONG",
                    entry_zone=[99.0, 101.0], stop_loss=95.0, initial_stop_loss=95.0,
                    target1=110.0, target2=120.0, current_status="ACTIVE",
                    signal_date=(T0 - dt.timedelta(hours=3)).isoformat(),
                    entry_date=(T0 - dt.timedelta(hours=3)).isoformat(),
                    tp1_close_pct=0.5, breakeven_price=100.15, tp1_hit=False,
                    tp1_exit_price=None, remaining_size_pct=1.0,
                    emergency_exit_reason=None)
        base.update(kw); return base

    out = {}

    # 6+ bars elapsed, price going nowhere -> cut.
    t = trade()
    moved = apply_bar(t, 101.0, 99.5, T0)
    out["stale_flat"] = {"moved": moved, "status": t["current_status"],
                         "pnl": t.get("pnl_pct"), "reason": t.get("stagnation_exit_reason")}

    # Same elapsed time, but price has travelled most of the way to TP1 ->
    # this is a slow winner and must be left alone.
    t = trade()
    moved = apply_bar(t, 108.0, 99.5, T0)
    out["stale_progressing"] = {"moved": moved, "status": t["current_status"]}

    # Not enough time elapsed yet -> left alone regardless.
    t = trade(entry_date=(T0 - dt.timedelta(minutes=30)).isoformat())
    moved = apply_bar(t, 101.0, 99.5, T0)
    out["fresh"] = {"moved": moved, "status": t["current_status"]}

    # A bar that also hit the STOP must be graded as a stop, never the milder
    # stagnation outcome.
    t = trade()
    moved = apply_bar(t, 101.0, 90.0, T0)
    out["stale_but_stopped"] = {"moved": moved, "status": t["current_status"]}

    # A bar that reached TP1 must bank the partial, not cut.
    t = trade()
    moved = apply_bar(t, 111.0, 99.5, T0)
    out["stale_but_tp1"] = {"moved": moved, "status": t["current_status"]}

    # Already past TP1 -> half banked, breakeven stop, nothing left to protect.
    t = trade(current_status="PARTIAL_TP1", tp1_hit=True, tp1_exit_price=110.0,
              remaining_size_pct=0.5, stop_loss=100.15)
    moved = apply_bar(t, 101.0, 100.5, T0)
    out["partial"] = {"moved": moved, "status": t["current_status"]}

    print(json.dumps(out))
'''


def test_a_directionless_position_is_cut_before_it_becomes_a_full_stop():
    """The headline calibration: Livne rides these to LOSS_SL."""
    out = _run(_STAGNATION_PROBE, HORIZON)
    assert out["stale_flat"]["moved"] is True
    assert out["stale_flat"]["status"] == "STAGNATION_EXIT"
    assert out["stale_flat"]["pnl"] is not None
    assert "no progress" in (out["stale_flat"]["reason"] or "")


def test_a_slow_winner_is_not_cut():
    """The rule that keeps this from being a plain time-stop: a position that
    has genuinely travelled toward its target is making progress."""
    out = _run(_STAGNATION_PROBE, HORIZON)
    assert out["stale_progressing"]["status"] == "ACTIVE"


def test_a_young_position_is_never_cut():
    out = _run(_STAGNATION_PROBE, HORIZON)
    assert out["fresh"]["status"] == "ACTIVE"


def test_a_stop_on_the_same_bar_still_grades_as_a_stop():
    """Grading a stopped-out bar as the milder stagnation exit would flatter
    the results -- the one thing a paper-trading system must never do."""
    out = _run(_STAGNATION_PROBE, HORIZON)
    assert out["stale_but_stopped"]["status"] == "LOSS_SL"


def test_reaching_tp1_still_banks_the_partial():
    out = _run(_STAGNATION_PROBE, HORIZON)
    assert out["stale_but_tp1"]["status"] == "PARTIAL_TP1"


def test_a_partial_position_is_out_of_scope():
    out = _run(_STAGNATION_PROBE, HORIZON)
    assert out["partial"]["status"] == "PARTIAL_TP1"


def test_livne_has_no_stagnation_exit():
    """Isolation again: the new rule must exist only in Horizon."""
    out = _run(_STAGNATION_PROBE.replace('asset_class="crypto"', 'asset_class="crypto"'), LIVNE)
    assert out["stale_flat"]["status"] == "ACTIVE", \
        "Horizon's stagnation exit leaked into Livne"
    assert out["stale_flat"]["moved"] is False
