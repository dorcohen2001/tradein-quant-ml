"""One instance per tenant, and the elapsed-time refresh window.

Both of these exist to protect the trade store, from two different angles:

  LOCKING     stops a second process writing the same store at all. The store
              is read-modify-write, so a concurrent writer does not corrupt the
              file (every save is atomic) -- it silently erases the other
              process's transitions, which is worse, because nothing looks
              broken afterwards.

  LOOKBACK    stops the live refresh examining less price action than actually
              elapsed. The old window was the model's nominal cycle interval,
              which is not the same thing as the time since the last check.

The locking tests spawn REAL subprocesses. A thread would share the file
handles and the OS lock table, so it would prove nothing about the case that
actually happens: two independent `python live_runner.py` invocations.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import textwrap

import pytest

from core.locking import FileLock, TenantBusy, TenantLock, read_tenant_owner, tenant_owner_path
from core.paper_trading.catch_up import refresh_lookback_minutes

NOW = dt.datetime(2026, 8, 15, 12, 0, 0)


def _trade(**kw) -> dict:
    base = {"id": "t1", "ticker": "BTCUSDT", "asset_class": "crypto",
            "direction": "LONG", "current_status": "ACTIVE"}
    base.update(kw)
    return base


# ==========================================================================
# FileLock
# ==========================================================================
def test_lock_is_exclusive_within_a_process(tmp_path):
    path = str(tmp_path / "a.lock")
    with FileLock(path):
        with pytest.raises(TimeoutError):
            FileLock(path, timeout=0.05).acquire()


def test_lock_is_reusable_after_release(tmp_path):
    path = str(tmp_path / "a.lock")
    for _ in range(3):
        with FileLock(path):
            pass
    with FileLock(path, timeout=0.05):
        pass  # still acquirable -- release actually released


def test_failed_acquire_leaves_no_open_handle(tmp_path):
    """A failed acquire must not leak the file object it opened to try.

    Leaked handles are invisible until Windows refuses to delete the runtime
    directory, or the interpreter prints a ResourceWarning during GC that looks
    like a second fault.
    """
    path = str(tmp_path / "a.lock")
    with FileLock(path):
        loser = FileLock(path, timeout=0.05)
        with pytest.raises(TimeoutError):
            loser.acquire()
        assert loser._handle is None


# ==========================================================================
# TenantLock -- across real processes
# ==========================================================================
_HOLDER = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, sys.argv[3])
    from core.locking import TenantLock
    with TenantLock(sys.argv[1], sys.argv[2]):
        print("HELD", os.getpid(), flush=True)
        time.sleep(float(sys.argv[4]))
""")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def holder(tmp_path):
    """A real second process holding the lock for `livne`."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(tmp_path), "livne", REPO_ROOT, "30"],
        stdout=subprocess.PIPE, text=True,
    )
    line = proc.stdout.readline()  # blocks until the child confirms it holds it
    assert line.startswith("HELD"), f"holder failed to start: {line!r}"
    yield proc, int(line.split()[1])
    proc.kill()
    proc.wait(timeout=10)


def test_second_instance_is_refused(tmp_path, holder):
    with pytest.raises(TenantBusy):
        TenantLock(str(tmp_path), "livne").acquire()


def test_refusal_names_the_blocking_pid(tmp_path, holder):
    _proc, pid = holder
    with pytest.raises(TenantBusy) as excinfo:
        TenantLock(str(tmp_path), "livne").acquire()
    assert str(pid) in str(excinfo.value)
    assert excinfo.value.owner["pid"] == pid


def test_a_different_model_is_unaffected(tmp_path, holder):
    """The lock is per tenant. Livne being busy must not stop Biton -- that
    would turn a safety check into exactly the cross-model coupling the
    supervisor was built to remove."""
    with TenantLock(str(tmp_path), "biton"):
        pass


def test_lock_is_released_when_the_holder_is_killed(tmp_path, holder):
    """The supervisor's phase timeout kills children routinely. An advisory OS
    lock is released on process death; a hand-rolled PID file would not be, and
    one killed cycle would wedge the tenant forever."""
    proc, _pid = holder
    proc.kill()
    proc.wait(timeout=10)
    with TenantLock(str(tmp_path), "livne", timeout=5.0):
        pass


def test_owner_sidecar_is_written_and_cleared(tmp_path):
    with TenantLock(str(tmp_path), "livne"):
        owner = read_tenant_owner(str(tmp_path), "livne")
        assert owner["pid"] == os.getpid()
        assert owner["model"] == "livne"
    assert not os.path.exists(tenant_owner_path(str(tmp_path), "livne"))


def test_stale_owner_file_does_not_block_acquisition(tmp_path):
    """The sidecar can outlive a killed process. The OS lock is the authority;
    a leftover file must never be mistaken for a live holder."""
    os.makedirs(tmp_path, exist_ok=True)
    with open(tenant_owner_path(str(tmp_path), "livne"), "w", encoding="utf-8") as fh:
        fh.write('{"pid": 999999, "started_at": "2020-01-01T00:00:00"}')
    with TenantLock(str(tmp_path), "livne"):
        assert read_tenant_owner(str(tmp_path), "livne")["pid"] == os.getpid()


# ==========================================================================
# Refresh lookback window
# ==========================================================================
def test_healthy_cycle_asks_for_the_floor():
    """Steady state must be byte-identical to the old fixed-interval behaviour,
    or this change would alter every healthy cycle's data request."""
    trade = _trade(last_checked_at=(NOW - dt.timedelta(minutes=2)).isoformat())
    assert refresh_lookback_minutes(trade, NOW, 2, 30) == 2


def test_a_slow_cycle_widens_the_window():
    """The whole point: an 8-minute cycle must look back 8 minutes, not 2."""
    trade = _trade(last_checked_at=(NOW - dt.timedelta(minutes=8)).isoformat())
    assert refresh_lookback_minutes(trade, NOW, 2, 30) == pytest.approx(8, abs=0.01)


def test_the_window_stops_at_the_catch_up_floor():
    """Past the ceiling the bar-by-bar replay owns the gap. Flattening six hours
    into one high/low would resolve at the wrong price and stamp the wrong
    time -- the exact failure catch-up exists to prevent."""
    trade = _trade(last_checked_at=(NOW - dt.timedelta(hours=6)).isoformat())
    assert refresh_lookback_minutes(trade, NOW, 2, 30) == 30


def test_a_trade_with_no_timestamp_gets_the_ceiling():
    """Assuming "just checked" is what hid the gap in the first place."""
    assert refresh_lookback_minutes(_trade(), NOW, 2, 30) == 30


def test_a_future_timestamp_gets_the_ceiling():
    """Clock skew or a hand-edited record must not produce a negative window."""
    trade = _trade(last_checked_at=(NOW + dt.timedelta(hours=1)).isoformat())
    assert refresh_lookback_minutes(trade, NOW, 2, 30) == 30


def test_falls_back_through_the_same_timestamps_catch_up_uses():
    """No `last_checked_at` yet, but the trade entered 5 minutes ago: that is a
    real "last known evaluation" and is preferred over the ceiling."""
    trade = _trade(entry_date=(NOW - dt.timedelta(minutes=5)).isoformat())
    assert refresh_lookback_minutes(trade, NOW, 2, 30) == pytest.approx(5, abs=0.01)


def test_floor_is_never_below_one_minute():
    """A sub-minute floor would request zero 1m candles."""
    trade = _trade(last_checked_at=NOW.isoformat())
    assert refresh_lookback_minutes(trade, NOW, 0.1, 30) == 1.0


def test_ceiling_below_floor_does_not_invert_the_window():
    trade = _trade(last_checked_at=(NOW - dt.timedelta(minutes=90)).isoformat())
    assert refresh_lookback_minutes(trade, NOW, 15, 5) == 15
