"""Provider failure accounting: one loud line instead of forty quiet ones.

THE PROBLEM
-----------
The data layer is full of deliberate containment boundaries -- one bad ticker
must not abort a 70-symbol scan -- but several of them swallowed the failure
entirely:

    except Exception:
        return None

That is right about the control flow and wrong about the reporting. A delisted
ticker and a total Yahoo outage produce the identical observable: `None`,
silently, forty times in a row, and a scan that finds nothing looks exactly
like a quiet market.

WHAT THIS ADDS
--------------
A per-provider consecutive-failure counter with rate-limited logging:

  * the FIRST failure after a healthy period logs in full, with the exception
    TYPE (`YFRateLimitError` reads very differently from `KeyError`);
  * subsequent failures are counted, not printed, so one outage cannot flood a
    tenant log;
  * every `_SUMMARY_EVERY` failures a summary line names the streak, which is
    what turns "lots of tickers are failing" into "this provider is down";
  * the first success after a streak logs a recovery line, so a log reader can
    bound the outage without guessing.

Counters are per PROCESS, which is the right scope: a tenant runs as its own
`--once` process, so a streak here means "this provider is failing for this
tenant, right now", not an accumulated historical total. Nothing is persisted;
this is diagnostics, and a diagnostic that needs its own state file to be
correct is a liability during an incident.
"""
from __future__ import annotations

import sys
import threading

# After the first, log one line per this many consecutive failures. 10 is
# roughly "an eighth of a watchlist" -- frequent enough to notice inside one
# cycle, rare enough that a log stays readable.
_SUMMARY_EVERY = 10

_lock = threading.Lock()
_streaks: dict[str, int] = {}


def note_failure(provider: str, operation: str, exc: BaseException,
                 subject: str = "") -> int:
    """Record one failed provider call. Returns the consecutive-failure count.

    Called from inside an `except` block that is about to degrade gracefully;
    it never raises and never changes the caller's control flow.
    """
    with _lock:
        streak = _streaks.get(provider, 0) + 1
        _streaks[provider] = streak

    label = f"{operation}({subject})" if subject else operation
    if streak == 1:
        print(f"[data] {provider} {label} failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
    elif streak % _SUMMARY_EVERY == 0:
        print(f"[data] {provider} has now failed {streak} consecutive call(s); "
              f"latest {label}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return streak


def note_success(provider: str) -> None:
    """Record a successful provider call, closing any open failure streak."""
    with _lock:
        streak = _streaks.get(provider, 0)
        if not streak:
            return
        _streaks[provider] = 0
    if streak >= _SUMMARY_EVERY:
        print(f"[data] {provider} recovered after {streak} consecutive failure(s)",
              file=sys.stderr)


def failure_streak(provider: str) -> int:
    """Current consecutive-failure count for `provider`. Read-only."""
    with _lock:
        return _streaks.get(provider, 0)


def snapshot() -> dict[str, int]:
    """Every provider's current streak, for diagnostics and tests."""
    with _lock:
        return dict(_streaks)


def reset() -> None:
    """Clear all counters. Tests only."""
    with _lock:
        _streaks.clear()
