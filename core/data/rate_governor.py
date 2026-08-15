"""Cross-process API rate limiting.

THE PROBLEM
-----------
Every tenant runs as its own OS process, and the supervisor fires all due
tenants at the same instant. Rate limits, however, are enforced per IP -- so
they are shared by every process on the machine while nothing coordinated them.
`BinanceKlineProvider` paces itself at 0.12s between requests, but that pacing
is per-process: ten tenants each politely pacing themselves still deliver ten
times the intended aggregate rate, in an aligned burst.

WHY A FILE LOCK RATHER THAN A LOCK OBJECT
------------------------------------------
`threading.Lock` and `multiprocessing.Lock` both require the participants to
share a parent process. Tenants do not: the supervisor spawns them, but a
tenant may equally be launched by hand or by Task Scheduler, and a backtest run
from a second terminal must respect the same budget. The only rendezvous point
every one of them genuinely shares is the filesystem.

The bucket state therefore lives in `runtime/state/ratelimit_<provider>.json`,
guarded by a lock file. On Windows the lock is `msvcrt.locking`, on POSIX
`fcntl.flock`; both are advisory locks released automatically if the holder
dies, which matters because a killed tenant (the supervisor's phase timeout
does exactly that) must not wedge the bucket for everyone else.

TOKEN BUCKET, NOT A FIXED DELAY
--------------------------------
A fixed sleep between calls wastes the budget when traffic is bursty, which is
precisely the shape here: a tenant does nothing for two minutes, then wants
thirty frames at once. A bucket refills continuously at `rate` tokens/second up
to `capacity`, so an idle tenant may burst up to the capacity it has accrued
and a busy fleet degrades to the sustained rate. Binance's own limit is
expressed in request WEIGHT, not calls, so `acquire` takes a weight.

FAILURE POLICY
--------------
Never fatal. If the state file is corrupt, unwritable, or the lock cannot be
taken, `acquire` logs once and allows the call. A governor that blocks market
data because it cannot write a JSON file has caused a worse outage than the one
it was preventing -- and the providers underneath still have their own retry and
backoff for a real 429.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass

_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    import msvcrt
else:  # pragma: no cover -- this repo runs on Windows
    import fcntl


@dataclass(frozen=True)
class Quota:
    """A provider's sustained rate and burst capacity.

    `rate` is tokens per SECOND and `capacity` is the most that can accrue, so
    `capacity` is the largest burst allowed after an idle period.
    """

    name: str
    rate: float
    capacity: float
    # Smallest gap between two calls regardless of tokens. Yahoo has no
    # published quota but does throttle aggressive callers, so it gets a floor
    # on spacing in addition to its bucket.
    min_interval: float = 0.0


# Binance spot REST allows 1200 request-weight per minute per IP. A 1000-candle
# klines call costs weight 2. 900/60 = 15 weight/s is a deliberate ~25% headroom
# below the published ceiling, because this governor cannot see requests made by
# anything outside it (a browser, a manual curl, another tool).
BINANCE = Quota(name="binance", rate=15.0, capacity=120.0)

# Yahoo publishes no limit and enforces one anyway. 2 requests/second sustained
# with a 0.2s floor between calls is comfortably below where yfinance users
# report throttling, and matches what the existing per-process pacing achieved
# when only one model ran.
YAHOO = Quota(name="yahoo", rate=2.0, capacity=10.0, min_interval=0.2)

QUOTAS = {q.name: q for q in (BINANCE, YAHOO)}

_STATE_DIR_ENV = "TRADEIN_RATELIMIT_DIR"
_warned: set[str] = set()


def _state_dir() -> str:
    """Where bucket state lives. Overridable by env so tests get an isolated
    directory instead of contending with a live supervisor's buckets."""
    override = os.environ.get(_STATE_DIR_ENV)
    if override:
        return override
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, "runtime", "state")


def _paths(provider: str) -> tuple[str, str]:
    directory = _state_dir()
    return (os.path.join(directory, f"ratelimit_{provider}.json"),
            os.path.join(directory, f"ratelimit_{provider}.lock"))


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"[rate] {message}", file=sys.stderr)


class _FileLock:
    """Advisory cross-process lock, released by the OS if the holder dies.

    A killed tenant -- which the supervisor's phase timeout produces routinely --
    must never leave the bucket permanently locked for every other tenant.
    """

    def __init__(self, path: str, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self._handle = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._handle = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if _IS_WINDOWS:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    # Give up the lock rather than the request: proceeding
                    # unsynchronised is a rate risk, blocking forever is an outage.
                    _warn_once(f"lock:{self.path}",
                               f"could not lock {os.path.basename(self.path)} within "
                               f"{self.timeout}s; proceeding without synchronisation")
                    raise TimeoutError(self.path)
                time.sleep(0.01)

    def __exit__(self, *exc):
        try:
            if _IS_WINDOWS:
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._handle.close()
        except OSError:
            pass
        return False


def _read_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_state(path: str, state: dict) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(tmp, path)


def _take(quota: Quota, weight: float, now: float) -> float:
    """Consume `weight` from the shared bucket. Returns seconds to wait before
    the request may proceed (0.0 if it may go immediately).

    Tokens are allowed to go NEGATIVE, and that is the design: the caller waits
    out the debt rather than being refused. A refusal would push the decision
    back to a caller that has no better option than to retry anyway, and would
    turn a rate limit into dropped market data.
    """
    state_path, lock_path = _paths(quota.name)
    try:
        with _FileLock(lock_path):
            state = _read_state(state_path)
            tokens = float(state.get("tokens", quota.capacity))
            updated = float(state.get("updated", now))
            last_call = float(state.get("last_call", 0.0))

            elapsed = max(0.0, now - updated)
            tokens = min(quota.capacity, tokens + elapsed * quota.rate)

            tokens -= weight
            wait = 0.0 if tokens >= 0 else (-tokens) / quota.rate

            # Minimum spacing is applied on top of the bucket, measured from the
            # last granted call across ALL processes.
            if quota.min_interval > 0:
                gap = (last_call + quota.min_interval) - (now + wait)
                if gap > 0:
                    wait += gap

            state.update(tokens=tokens, updated=now, last_call=now + wait)
            _write_state(state_path, state)
            return wait
    except (TimeoutError, OSError) as exc:
        _warn_once(f"state:{quota.name}",
                   f"{quota.name} bucket unavailable ({exc}); allowing the call ungoverned")
        return 0.0


def acquire(provider: str, weight: float = 1.0, max_wait: float = 30.0,
            sleep: bool = True) -> float:
    """Reserve `weight` against `provider`'s shared budget.

    Returns the seconds waited. With `sleep=True` (the default) the wait has
    already happened when this returns, so the caller may issue its request
    immediately.

    `max_wait` bounds the delay. A tenant's whole cycle is bounded by the
    supervisor's phase timeout, so an unbounded wait here would simply be
    converted into a killed cycle -- which loses the request AND the rest of the
    cycle's work. Past the cap the call proceeds and takes its chances with the
    provider's own 429 handling, which already retries with backoff.
    """
    quota = QUOTAS.get(provider)
    if quota is None:
        return 0.0

    wait = _take(quota, weight, time.time())
    if wait <= 0:
        return 0.0
    if wait > max_wait:
        _warn_once(f"cap:{provider}",
                   f"{provider} bucket would require a {wait:.1f}s wait (cap {max_wait}s); "
                   f"proceeding and relying on provider backoff")
        wait = max_wait
    if sleep and wait > 0:
        time.sleep(wait)
    return wait


def snapshot(provider: str) -> dict:
    """Current bucket state, for diagnostics and tests. Read-only."""
    quota = QUOTAS.get(provider)
    if quota is None:
        return {}
    state_path, _ = _paths(provider)
    state = _read_state(state_path)
    now = time.time()
    tokens = float(state.get("tokens", quota.capacity))
    elapsed = max(0.0, now - float(state.get("updated", now)))
    return {
        "provider": provider,
        "tokens": round(min(quota.capacity, tokens + elapsed * quota.rate), 2),
        "capacity": quota.capacity,
        "rate_per_second": quota.rate,
        "min_interval": quota.min_interval,
    }


def reset(provider: str) -> None:
    """Refill the bucket. Tests only -- never call this from a running system,
    it discards the accounting every other process is relying on."""
    state_path, lock_path = _paths(provider)
    try:
        with _FileLock(lock_path, timeout=2.0):
            _write_state(state_path, {"tokens": QUOTAS[provider].capacity,
                                      "updated": time.time(), "last_call": 0.0})
    except (TimeoutError, OSError, KeyError):
        pass
