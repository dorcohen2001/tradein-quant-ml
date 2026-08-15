"""Interval gates that survive process restarts.

`live_runner` has always had a "fire at most once per N minutes, regardless of
how often you are called" primitive. It was an in-memory counter, which was
correct while Livne ran as one long-lived process holding its own loop.

Under the supervisor that assumption breaks. Each model is now launched as a
fresh `--once` subprocess per tick, so an in-memory `last fired` resets on
every invocation and a gate degrades to "fire every time". For the funnel
export that is not cosmetic: it re-runs the entire gate chain for every
watchlist ticker, so at Livne's 2-minute cadence an always-open gate means
~30 reports/hour, churning through the 50-file retention in under two hours
and roughly doubling scan cost for a purely diagnostic artifact.

`PersistentIntervalGate` keeps the same semantics and stores the last-fire
timestamp in a small JSON file, exactly as the daily/weekly report markers
already do (and for exactly the same reason -- see
`core.reporting.summary.load_report_state`).

Failure policy: a gate is a THROTTLE, not a safety control. If its state file
is missing, corrupt or unwritable, the gate opens rather than jamming shut --
a duplicate diagnostic export is a nuisance, whereas a gate stuck closed
silently disables the thing it guards, which is much harder to notice.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile


class IntervalGate:
    """Fires at most once per `minutes`, independent of the caller's cadence.

    First call always fires, so a freshly started runner does its work
    immediately rather than waiting out a full interval.
    """

    def __init__(self, minutes: float) -> None:
        self.interval = dt.timedelta(minutes=minutes)
        self.last: dt.datetime | None = None

    def due(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now()
        if self.last is None or (now - self.last) >= self.interval:
            self.last = now
            return True
        return False


class PersistentIntervalGate(IntervalGate):
    """An `IntervalGate` whose last-fire time is stored on disk, so it holds
    across the process exits that `--once` scheduling implies.

    `name` keys the entry inside `state_path`, so several gates can share one
    small file without stepping on each other.
    """

    def __init__(self, minutes: float, state_path: str, name: str) -> None:
        super().__init__(minutes)
        self.state_path = state_path
        self.name = name

    def _read_state(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            return state if isinstance(state, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _write_state(self, state: dict) -> None:
        """Atomic (temp file + os.replace), matching the trade store and report
        state, so a crash mid-write cannot leave a truncated file behind."""
        directory = os.path.dirname(self.state_path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, self.state_path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def due(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now()
        state = self._read_state()

        raw = state.get(self.name)
        last: dt.datetime | None = None
        if raw:
            try:
                last = dt.datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                last = None  # unparseable marker -> treat as never fired

        if last is not None and (now - last) < self.interval:
            self.last = last
            return False

        state[self.name] = now.isoformat(timespec="seconds")
        try:
            self._write_state(state)
        except OSError as exc:
            # Fire anyway (see the module docstring): an unwritable state file
            # must not silently disable the gated work forever.
            print(f"[scheduling] could not persist gate {self.name!r} ({exc}); "
                  f"firing anyway.", file=sys.stderr)
        self.last = now
        return True
