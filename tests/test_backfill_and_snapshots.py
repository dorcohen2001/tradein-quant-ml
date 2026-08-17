"""The `last_checked_at` migration, and the supervisor's automated snapshots.

Both are recovery mechanisms. The migration protects the FIRST run after the
catch-up feature reached a store written before it existed; the snapshot loop
protects every run after that, because `runtime/` is gitignored and a code
rollback restores no trades.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.paper_trading.store import load_trades  # noqa: E402
from scripts.backfill_last_checked import backfill_store  # noqa: E402

NOW = dt.datetime(2026, 8, 15, 23, 0, 0)


def _store(tmp_path, trades) -> str:
    path = str(tmp_path / "trades_test.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(trades, fh)
    return path


def _trade(status, **kw) -> dict:
    base = {"id": f"t-{status}", "ticker": "BTCUSDT", "asset_class": "crypto",
            "direction": "LONG", "current_status": status,
            "signal_date": "2026-08-01T10:00:00"}
    base.update(kw)
    return base


# ==========================================================================
# Backfill
# ==========================================================================
def test_open_positions_are_stamped(tmp_path):
    path = _store(tmp_path, [_trade("PENDING"), _trade("ACTIVE"), _trade("PARTIAL_TP1")])
    counts = backfill_store(path, NOW, apply=True)

    assert counts == {"total": 3, "open": 3, "already": 0, "filled": 3}
    assert all(t["last_checked_at"] == NOW.isoformat(timespec="seconds")
               for t in load_trades(path))


def test_closed_positions_are_never_stamped(tmp_path):
    """Nothing re-examines a closed trade. Writing a "last checked" time onto a
    trade that closed in July would put a false statement in the store."""
    path = _store(tmp_path, [_trade("WIN_TP2"), _trade("LOSS_SL"), _trade("EXPIRED"),
                             _trade("LOSS_SL_BE"), _trade("EMERGENCY_EXIT")])
    counts = backfill_store(path, NOW, apply=True)

    assert counts["open"] == 0 and counts["filled"] == 0
    assert not any("last_checked_at" in t for t in load_trades(path))


def test_an_existing_stamp_is_never_overwritten(tmp_path):
    """That value describes a REAL gap. Overwriting it with `now` would erase
    the evidence of an outage the catch-up is supposed to find and replay."""
    earlier = "2026-08-14T09:00:00"
    path = _store(tmp_path, [_trade("ACTIVE", last_checked_at=earlier), _trade("ACTIVE")])
    counts = backfill_store(path, NOW, apply=True)

    assert counts == {"total": 2, "open": 2, "already": 1, "filled": 1}
    stamps = sorted(t["last_checked_at"] for t in load_trades(path))
    assert stamps == [earlier, NOW.isoformat(timespec="seconds")]


def test_running_twice_is_a_no_op(tmp_path):
    path = _store(tmp_path, [_trade("ACTIVE")])
    backfill_store(path, NOW, apply=True)
    second = backfill_store(path, NOW + dt.timedelta(hours=1), apply=True)

    assert second["filled"] == 0
    assert load_trades(path)[0]["last_checked_at"] == NOW.isoformat(timespec="seconds")


def test_dry_run_changes_nothing_on_disk(tmp_path):
    path = _store(tmp_path, [_trade("ACTIVE")])
    before = open(path, encoding="utf-8").read()

    counts = backfill_store(path, NOW, apply=False)

    assert counts["filled"] == 1
    assert open(path, encoding="utf-8").read() == before


def test_nothing_else_about_a_trade_is_touched(tmp_path):
    """A migration over live positions must be provably minimal."""
    original = _trade("ACTIVE", entry_zone=[99.0, 101.0], stop_loss=95.0,
                      pnl_pct=None, telegram_message_id=4711)
    path = _store(tmp_path, [original])
    backfill_store(path, NOW, apply=True)

    after = load_trades(path)[0]
    assert {k: v for k, v in after.items() if k != "last_checked_at"} == original


def test_a_corrupt_store_raises_rather_than_being_rewritten(tmp_path):
    """`load_trades` refuses to parse a broken store precisely so the next save
    cannot overwrite recoverable history. The migration must inherit that."""
    path = str(tmp_path / "trades_bad.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    with pytest.raises(RuntimeError):
        backfill_store(path, NOW, apply=True)


# ==========================================================================
# Supervisor snapshots
# ==========================================================================
def _supervisor():
    return importlib.import_module("run_supervisor")


def test_snapshot_is_verified_after_it_is_written(monkeypatch):
    """A backup that was never read back is a guess. The loop must verify."""
    supervisor = _supervisor()
    calls = {}

    def fake_create(mode, retention):
        calls["create"] = (mode, retention)
        return "arch.zip"

    def fake_verify(path):
        calls["verify"] = path
        return True

    import scripts.backup_manager as bm
    monkeypatch.setattr(bm, "create_backup", fake_create)
    monkeypatch.setattr(bm, "verify_snapshot", fake_verify)

    supervisor._take_runtime_snapshot()

    assert calls["create"][0] == "runtime"
    assert calls["verify"] == "arch.zip"


def test_snapshot_uses_the_configured_retention(monkeypatch):
    supervisor = _supervisor()
    seen = {}

    def fake_create(mode, retention):
        seen["retention"] = retention
        return "a.zip"

    import scripts.backup_manager as bm
    monkeypatch.setattr(bm, "create_backup", fake_create)
    monkeypatch.setattr(bm, "verify_snapshot", lambda path: True)

    supervisor._take_runtime_snapshot()

    assert seen["retention"] == supervisor.RUNTIME_BACKUP_RETENTION


def test_a_failing_snapshot_does_not_stop_the_sweep_or_the_models(monkeypatch, tmp_path):
    """Housekeeping must never be able to take down the process that keeps the
    tenants running -- and a failing sweep must not suppress the backup either,
    which is why they are wrapped separately."""
    supervisor = _supervisor()

    import scripts.backup_manager as bm
    monkeypatch.setattr(bm, "create_backup",
                        lambda mode, retention: (_ for _ in ()).throw(OSError("disk full")))

    # The loop body is what must survive; run one pass with both gates open.
    monkeypatch.setattr(supervisor, "RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor._stop, "wait", lambda _t: supervisor._stop.set())
    try:
        supervisor._maintenance_loop()  # must return, not raise
    finally:
        supervisor._stop.clear()


def test_the_two_gates_do_not_share_a_marker(tmp_path):
    """Sweep and backup are keyed separately inside one gate file. Sharing a
    name would mean the daily sweep suppressed the 6-hourly snapshot."""
    from core.scheduling import PersistentIntervalGate

    path = str(tmp_path / "gates.json")
    sweep = PersistentIntervalGate(1440, state_path=path, name="runtime_sweep")
    backup = PersistentIntervalGate(360, state_path=path, name="runtime_backup")

    assert sweep.due() is True
    assert backup.due() is True      # not suppressed by the sweep having fired
    assert sweep.due() is False
    assert backup.due() is False
