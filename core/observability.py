"""Per-tenant logs and heartbeats.

Two problems this fixes, both of which only bite once several tenants run:

  LOGS. The supervisor launched every child with inherited stdout/stderr, so N
  tenants interleaved into one stream with nothing identifying which line came
  from which. With two models that is untidy; with ten it makes an incident
  unreadable. Each tenant now gets `runtime/logs/<key>.log`.

  HEARTBEATS. A tenant whose cycles silently stopped was invisible until
  somebody noticed the absence of alerts -- and absence of alerts is also what
  a quiet market looks like. Each cycle now stamps a heartbeat, so "ran and
  found nothing" is distinguishable from "has not run since Tuesday".

ROTATION HAPPENS BEFORE THE CHILD OPENS THE FILE, deliberately. A child process
holds the log open for its whole cycle, and renaming a file that another
process has open fails on Windows with PermissionError. The supervisor is the
only rotator, and it rotates at the moment no child holds the handle -- just
before launching the next one. That is why this is a plain size check rather
than `logging.RotatingFileHandler`, which assumes the rotating process is also
the writing process.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile

DEFAULT_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per tenant log
DEFAULT_BACKUPS = 3                   # <key>.log.1 .. .log.3


# ---------------------------------------------------------------------------
# Log rotation
# ---------------------------------------------------------------------------
def log_path(logs_dir: str, key: str) -> str:
    return os.path.join(logs_dir, f"{key}.log")


def rotate_if_needed(path: str, max_bytes: int = DEFAULT_MAX_BYTES,
                     backups: int = DEFAULT_BACKUPS) -> bool:
    """Rotate `path` when it exceeds `max_bytes`. Returns True if it rotated.

    `<key>.log` -> `<key>.log.1`, `.1` -> `.2`, and the oldest is discarded.

    Never raises. A log that cannot be rotated (locked by a viewer, permissions)
    must not stop a tenant from running -- it just keeps growing until the next
    attempt succeeds, and the maintenance sweep will age it out regardless.
    """
    try:
        if max_bytes <= 0 or not os.path.exists(path):
            return False
        if os.path.getsize(path) < max_bytes:
            return False

        # Walk downwards so each slot is free before something moves into it.
        oldest = f"{path}.{backups}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for index in range(backups - 1, 0, -1):
            src, dst = f"{path}.{index}", f"{path}.{index + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")
        return True
    except OSError as exc:
        print(f"[observability] could not rotate {path}: {exc}", file=sys.stderr)
        return False


def open_tenant_log(logs_dir: str, key: str, max_bytes: int = DEFAULT_MAX_BYTES,
                    backups: int = DEFAULT_BACKUPS):
    """Rotate if needed, then return an append-mode handle for a child's
    stdout/stderr. Returns None if the log cannot be opened, in which case the
    caller should fall back to inherited output rather than skip the cycle --
    losing log capture is an inconvenience; skipping a trading cycle is not.
    """
    try:
        os.makedirs(logs_dir, exist_ok=True)
        path = log_path(logs_dir, key)
        rotate_if_needed(path, max_bytes, backups)
        return open(path, "a", encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[observability] could not open log for {key}: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------
def heartbeat_path(state_dir: str, key: str) -> str:
    return os.path.join(state_dir, f"heartbeat_{key}.json")


def write_heartbeat(state_dir: str, key: str, *, status: str = "ok",
                    detail: str = "", duration_seconds: float | None = None,
                    now: dt.datetime | None = None, extra: dict | None = None) -> bool:
    """Stamp `key`'s heartbeat. Returns True on success; never raises.

    Written atomically (temp + os.replace) for the same reason every other
    state file here is: a reader -- the supervisor, or `fleet_status.py` --
    must never see a half-written file and conclude the tenant is broken.

    `extra` merges additional fields into the payload. The supervisor uses it
    to carry escalation state (`failure_streak`, `alerted`) in the heartbeat
    itself rather than in a second file: the streak has to survive a supervisor
    restart, or restarting during an outage would re-arm the alert and page
    somebody about a failure they already know about.
    """
    moment = now or dt.datetime.now()
    payload = {
        "model": key,
        "last_run": moment.isoformat(timespec="seconds"),
        "status": status,
        "detail": detail,
        "duration_seconds": round(duration_seconds, 1) if duration_seconds is not None else None,
    }
    if extra:
        payload.update(extra)
    try:
        os.makedirs(state_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, heartbeat_path(state_dir, key))
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return True
    except OSError as exc:
        print(f"[observability] could not write heartbeat for {key}: {exc}", file=sys.stderr)
        return False


def read_heartbeat(state_dir: str, key: str) -> dict | None:
    """The last heartbeat, or None if absent/unreadable. A missing heartbeat is
    a legitimate state -- a tenant that has never run under this supervisor --
    not an error."""
    try:
        with open(heartbeat_path(state_dir, key), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def heartbeat_age(state_dir: str, key: str, now: dt.datetime | None = None) -> dt.timedelta | None:
    """How long since `key` last reported. None if it never has."""
    beat = read_heartbeat(state_dir, key)
    if not beat or not beat.get("last_run"):
        return None
    try:
        last = dt.datetime.fromisoformat(beat["last_run"])
    except (TypeError, ValueError):
        return None
    return (now or dt.datetime.now()) - last


def is_stale(state_dir: str, key: str, interval_minutes: float,
             grace: float = 2.5, now: dt.datetime | None = None) -> bool:
    """Whether `key` has missed its schedule.

    `grace` multiplies the interval before the tenant is called stale. A cycle
    legitimately overruns sometimes -- a slow provider, a long watchlist -- and
    warning on the first late beat would train the operator to ignore the
    warning. 2.5x means roughly "missed two cycles in a row".

    A tenant that has never reported is NOT stale: it may have been registered
    seconds ago, and there is nothing to compare against.
    """
    age = heartbeat_age(state_dir, key, now)
    if age is None:
        return False
    return age.total_seconds() > max(interval_minutes, 0.1) * 60 * grace


def format_age(age: dt.timedelta | None) -> str:
    """Compact relative age: `12s`, `4m`, `3h 20m`, `2d 4h`, or `never`."""
    if age is None:
        return "never"
    seconds = int(age.total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"
