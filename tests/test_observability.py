"""Per-tenant logs, rotation, heartbeats, staleness, and the fleet view.

Log rotation is tested against real files rather than mocks, because the whole
reason rotation is done by the supervisor BEFORE launching a child (rather than
by `logging.RotatingFileHandler`) is a Windows file-locking constraint that a
mock would hide.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import os
import subprocess
import sys
import textwrap

import pytest

from core.observability import (
    DEFAULT_BACKUPS,
    format_age,
    heartbeat_age,
    is_stale,
    log_path,
    open_tenant_log,
    read_heartbeat,
    rotate_if_needed,
    write_heartbeat,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = dt.datetime(2026, 8, 15, 12, 0, 0)


# ==========================================================================
# Log rotation
# ==========================================================================
def test_small_log_is_not_rotated(tmp_path):
    path = tmp_path / "livne.log"
    path.write_text("short", encoding="utf-8")
    assert rotate_if_needed(str(path), max_bytes=1024) is False
    assert path.read_text(encoding="utf-8") == "short"


def test_oversized_log_rotates_to_dot_one(tmp_path):
    path = tmp_path / "livne.log"
    path.write_text("x" * 2048, encoding="utf-8")
    assert rotate_if_needed(str(path), max_bytes=1024) is True
    assert not path.exists()
    assert (tmp_path / "livne.log.1").read_text(encoding="utf-8") == "x" * 2048


def test_rotation_shifts_backups_and_discards_the_oldest(tmp_path):
    path = tmp_path / "livne.log"
    for index in range(1, DEFAULT_BACKUPS + 1):
        (tmp_path / f"livne.log.{index}").write_text(f"gen{index}", encoding="utf-8")
    path.write_text("y" * 2048, encoding="utf-8")

    rotate_if_needed(str(path), max_bytes=1024, backups=DEFAULT_BACKUPS)

    assert (tmp_path / "livne.log.1").read_text(encoding="utf-8") == "y" * 2048
    assert (tmp_path / "livne.log.2").read_text(encoding="utf-8") == "gen1"
    assert (tmp_path / "livne.log.3").read_text(encoding="utf-8") == "gen2"
    # gen3 fell off the end -- exactly `backups` files are kept.
    assert not (tmp_path / f"livne.log.{DEFAULT_BACKUPS + 1}").exists()


def test_rotation_never_raises_on_a_missing_file(tmp_path):
    assert rotate_if_needed(str(tmp_path / "nope.log"), max_bytes=1) is False


def test_zero_max_bytes_disables_rotation(tmp_path):
    path = tmp_path / "livne.log"
    path.write_text("x" * 5000, encoding="utf-8")
    assert rotate_if_needed(str(path), max_bytes=0) is False
    assert path.exists()


def test_open_tenant_log_appends_and_rotates(tmp_path):
    logs = str(tmp_path / "logs")
    handle = open_tenant_log(logs, "livne", max_bytes=1024)
    assert handle is not None
    handle.write("first cycle\n")
    handle.close()

    # Grow it past the threshold, then reopen: rotation happens on open, which
    # is the moment no child process holds the handle.
    with open(log_path(logs, "livne"), "a", encoding="utf-8") as fh:
        fh.write("z" * 2048)
    handle = open_tenant_log(logs, "livne", max_bytes=1024)
    assert handle is not None
    handle.write("second cycle\n")
    handle.close()

    assert os.path.exists(os.path.join(logs, "livne.log.1"))
    assert "second cycle" in open(log_path(logs, "livne"), encoding="utf-8").read()


def test_each_tenant_gets_its_own_log(tmp_path):
    logs = str(tmp_path / "logs")
    for key in ("livne", "biton", "client_alpha"):
        handle = open_tenant_log(logs, key)
        handle.write(f"{key} ran\n")
        handle.close()
    for key in ("livne", "biton", "client_alpha"):
        assert f"{key} ran" in open(os.path.join(logs, f"{key}.log"), encoding="utf-8").read()


# ==========================================================================
# Heartbeats
# ==========================================================================
def test_heartbeat_round_trip(tmp_path):
    state = str(tmp_path)
    assert write_heartbeat(state, "livne", status="ok", duration_seconds=12.34, now=NOW) is True
    beat = read_heartbeat(state, "livne")
    assert beat["model"] == "livne"
    assert beat["status"] == "ok"
    assert beat["duration_seconds"] == 12.3
    assert beat["last_run"].startswith("2026-08-15T12:00")


def test_heartbeat_write_is_atomic(tmp_path):
    write_heartbeat(str(tmp_path), "livne", now=NOW)
    write_heartbeat(str(tmp_path), "livne", now=NOW)
    assert [p.name for p in tmp_path.iterdir()] == ["heartbeat_livne.json"]


def test_heartbeats_are_per_tenant(tmp_path):
    write_heartbeat(str(tmp_path), "livne", status="ok", now=NOW)
    write_heartbeat(str(tmp_path), "client_alpha", status="error", now=NOW)
    assert read_heartbeat(str(tmp_path), "livne")["status"] == "ok"
    assert read_heartbeat(str(tmp_path), "client_alpha")["status"] == "error"


def test_missing_or_corrupt_heartbeat_reads_as_none(tmp_path):
    assert read_heartbeat(str(tmp_path), "ghost") is None
    (tmp_path / "heartbeat_bad.json").write_text("{ truncated", encoding="utf-8")
    assert read_heartbeat(str(tmp_path), "bad") is None
    assert heartbeat_age(str(tmp_path), "bad") is None


def test_staleness_needs_a_missed_schedule_not_just_a_late_cycle(tmp_path):
    """Grace is 2.5x the interval on purpose. Warning on the first late beat
    would train the operator to ignore the warning."""
    state = str(tmp_path)
    write_heartbeat(state, "livne", now=NOW)

    assert is_stale(state, "livne", 2, now=NOW + dt.timedelta(minutes=2)) is False
    assert is_stale(state, "livne", 2, now=NOW + dt.timedelta(minutes=4)) is False
    assert is_stale(state, "livne", 2, now=NOW + dt.timedelta(minutes=30)) is True


def test_a_tenant_that_never_ran_is_not_stale(tmp_path):
    """It may have been registered seconds ago; there is nothing to compare."""
    assert is_stale(str(tmp_path), "brand_new", 5) is False


@pytest.mark.parametrize("delta,expected", [
    (dt.timedelta(seconds=12), "12s"),
    (dt.timedelta(minutes=4), "4m"),
    (dt.timedelta(hours=3, minutes=20), "3h 20m"),
    (dt.timedelta(days=2, hours=4), "2d 4h"),
    (None, "never"),
])
def test_age_formatting(delta, expected):
    assert format_age(delta) == expected


# ==========================================================================
# Supervisor integration: real child, real log, real heartbeat
# ==========================================================================
CHILD = textwrap.dedent('''
    import sys
    print("stdout from the tenant")
    print("stderr from the tenant", file=sys.stderr)
''')


def _sandbox_model(tmp_path, key, body, timeout=60):
    from core.registry import ModelConfig
    model_dir = tmp_path / key
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "live_runner.py").write_text(body, encoding="utf-8")

    class _Sandboxed(ModelConfig):
        @property
        def model_dir(self):
            return str(model_dir)

        @property
        def live_runner(self):
            return str(model_dir / "live_runner.py")

    return _Sandboxed(
        key=key, label=key, cycle_interval_minutes=5,
        closed_statuses=("WIN_TP1",), update_statuses=(), closed_with_pnl=("WIN_TP1",),
        phase_timeout_seconds=timeout,
    )


class _Args:
    asset_class = None
    top_n = None
    candidate_pool = None
    skip_scan = False
    once = True


def test_supervisor_redirects_child_output_and_stamps_a_heartbeat(tmp_path, monkeypatch):
    """Both streams land in the tenant's own log, and the cycle is recorded --
    the two things that make N concurrent tenants debuggable."""
    sys.path.insert(0, REPO_ROOT)
    supervisor = importlib.import_module("run_supervisor")

    logs = tmp_path / "logs"
    state = tmp_path / "state"
    monkeypatch.setattr(supervisor, "LOGS_DIR", str(logs))
    monkeypatch.setattr(supervisor, "STATE_DIR", str(state))

    model = _sandbox_model(tmp_path, "client_probe", CHILD)
    outcome = supervisor.run_model_cycle(model, _Args())
    assert outcome.startswith("ok in")

    written = (logs / "client_probe.log").read_text(encoding="utf-8")
    assert "stdout from the tenant" in written
    assert "stderr from the tenant" in written      # stderr is merged into the same log
    assert "client_probe ->" in written             # the cycle banner

    beat = json.loads((state / "heartbeat_client_probe.json").read_text(encoding="utf-8"))
    assert beat["status"] == "ok"
    assert beat["duration_seconds"] is not None


def test_supervisor_records_a_failing_child_in_the_heartbeat(tmp_path, monkeypatch):
    sys.path.insert(0, REPO_ROOT)
    supervisor = importlib.import_module("run_supervisor")
    monkeypatch.setattr(supervisor, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(supervisor, "STATE_DIR", str(tmp_path / "state"))

    model = _sandbox_model(tmp_path, "client_boom", "raise SystemExit(3)\n")
    outcome = supervisor.run_model_cycle(model, _Args())
    assert "exit code 3" in outcome

    beat = read_heartbeat(str(tmp_path / "state"), "client_boom")
    assert beat["status"] == "error"


# ==========================================================================
# Fleet status
# ==========================================================================
def test_fleet_status_runs_and_covers_every_model():
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "scripts", "fleet_status.py"), "--json"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    rows = json.loads(proc.stdout)

    from core.registry import all_models
    assert {r["key"] for r in rows} == {m.key for m in all_models()}
    for row in rows:
        assert set(row) >= {"key", "label", "stage", "enabled", "interval_minutes",
                            "health", "open_trades", "route"}
        assert row["store_error"] is None, f"{row['key']} store unreadable"


def test_fleet_status_renders_plain_ascii_for_cmd():
    """cmd.exe on a non-UTF-8 codepage turns box-drawing and coloured glyphs
    into mojibake, so the default output must stay ASCII."""
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "scripts", "fleet_status.py")],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=300,
    )
    assert proc.returncode == 0
    assert proc.stdout.isascii(), "default output must be ASCII-safe for cmd.exe"
    assert "MODEL" in proc.stdout and "HEALTH" in proc.stdout
