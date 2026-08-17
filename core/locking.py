"""Cross-process advisory locks, and the one-instance-per-tenant guarantee.

WHY THIS EXISTS
---------------
Every tenant's trade store is written read-modify-write: `load_trades()` ->
mutate the whole list -> `save_trades()`. That write is atomic (temp file +
`os.replace`), which prevents a TORN file -- it does not prevent a LOST
UPDATE. Two processes running the same model each load the same list, apply
different transitions, and whichever saves second silently erases the other's
work. Both also send their Telegram alerts, so the chat shows two exits while
the store remembers one.

Nothing prevented that. The supervisor launches one child per model per tick,
but a model can equally be started by hand (`python live_runner.py`), by a
leftover Task Scheduler entry, or by a second supervisor -- and the registry's
`enabled=False` guard only protects a model somebody deliberately paused.

`TenantLock` closes it: a runner takes the lock for its model key before doing
any work, and a second instance refuses to start and names the PID holding it.

WHY A FILE LOCK
---------------
The participants do not share a parent process, so `threading.Lock` and
`multiprocessing.Lock` are both unavailable. The only rendezvous point they
genuinely share is the filesystem. On Windows the primitive is
`msvcrt.locking`, on POSIX `fcntl.flock`; both are advisory locks released by
the OS if the holder dies -- which matters because the supervisor's phase
timeout kills children routinely, and a killed tenant must not wedge its own
model forever.

THE LOCK FILE STAYS EMPTY
-------------------------
`msvcrt.locking` locks a byte RANGE at the current file position, so a lock
file that is written to is a lock file whose byte ranges drift. The owner's
identity therefore lives in a separate `<name>.owner.json` sidecar, written
after the lock is held and removed on release. A sidecar left behind by a
killed process is harmless: the OS has already released the real lock, and the
next acquirer overwrites the sidecar.

This module was factored out of `core.data.rate_governor`, which needs exactly
the same primitive for its token bucket and now imports it from here.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sys
import time

_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    import msvcrt
else:  # pragma: no cover -- this repo runs on Windows
    import fcntl


class FileLock:
    """Advisory cross-process lock on `path`, released by the OS if the holder dies.

    Raises `TimeoutError` if the lock cannot be taken within `timeout`. What to
    do about that is the CALLER's decision and the two callers here differ: the
    rate governor proceeds ungoverned (a rate risk beats an outage), while a
    tenant runner refuses to start (a duplicate writer corrupts state).

    The file is locked at position 0 and unlocked at position 0. The original
    version in `rate_governor` locked at the append-mode position -- harmless
    while the lock file was always empty, and a latent mismatch the moment one
    was not.
    """

    def __init__(self, path: str, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self._handle = None

    def acquire(self) -> "FileLock":
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._handle = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._handle.seek(0)
                if _IS_WINDOWS:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    try:
                        self._handle.close()
                    except OSError:
                        pass
                    self._handle = None
                    raise TimeoutError(self.path)
                time.sleep(0.01)

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if _IS_WINDOWS:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._handle.close()
        except OSError:
            pass
        self._handle = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> bool:
        self.release()
        return False


# ---------------------------------------------------------------------------
# One instance per tenant
# ---------------------------------------------------------------------------
class TenantBusy(RuntimeError):
    """Another process already holds this model's lock.

    Carries the recorded owner so the caller can say WHICH process is in the
    way -- "already running" without a PID sends an operator hunting through
    Task Manager.
    """

    def __init__(self, key: str, owner: dict | None) -> None:
        self.key = key
        self.owner = owner or {}
        pid = self.owner.get("pid", "unknown")
        started = self.owner.get("started_at", "unknown time")
        super().__init__(
            f"{key} is already running as PID {pid} (started {started}). "
            f"Refusing to start a second instance: both would read-modify-write "
            f"the same trade store and the second save would erase the first."
        )


def tenant_lock_path(state_dir: str, key: str) -> str:
    return os.path.join(state_dir, f"tenant_{key}.lock")


def tenant_owner_path(state_dir: str, key: str) -> str:
    return os.path.join(state_dir, f"tenant_{key}.owner.json")


def read_tenant_owner(state_dir: str, key: str) -> dict | None:
    """Who last claimed this tenant's lock, or None.

    Advisory only. The lock itself is the authority -- this file can outlive a
    killed process, so it answers "who was it" and never "is it running".
    """
    try:
        with open(tenant_owner_path(state_dir, key), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


class TenantLock:
    """Exclusive right to run one model, for the lifetime of a `with` block.

    Usage:

        with TenantLock(STATE_DIR, "livne"):
            run_cycle(...)

    Raises `TenantBusy` immediately if another process holds it. `timeout`
    defaults to 0 for exactly that reason: a tenant that has to WAIT for
    another instance is a tenant running at the wrong time, and the supervisor
    will call it again on the next tick anyway.
    """

    def __init__(self, state_dir: str, key: str, timeout: float = 0.0) -> None:
        self.state_dir = state_dir
        self.key = key
        self._lock = FileLock(tenant_lock_path(state_dir, key), timeout=timeout)
        self._acquired = False

    def _write_owner(self) -> None:
        payload = {
            "model": self.key,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": dt.datetime.now().isoformat(timespec="seconds"),
            "argv": " ".join(sys.argv[:4]),
        }
        try:
            with open(tenant_owner_path(self.state_dir, self.key), "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
        except OSError:
            # The lock is what enforces exclusivity; the sidecar is diagnostics.
            # Failing to write it must not stop a cycle that legitimately owns
            # the lock.
            pass

    def _clear_owner(self) -> None:
        try:
            os.remove(tenant_owner_path(self.state_dir, self.key))
        except OSError:
            pass

    def acquire(self) -> "TenantLock":
        try:
            self._lock.acquire()
        except TimeoutError:
            raise TenantBusy(self.key, read_tenant_owner(self.state_dir, self.key)) from None
        self._acquired = True
        self._write_owner()
        return self

    def release(self) -> None:
        if not self._acquired:
            return
        self._clear_owner()
        self._lock.release()
        self._acquired = False

    def __enter__(self) -> "TenantLock":
        return self.acquire()

    def __exit__(self, *exc) -> bool:
        self.release()
        return False
