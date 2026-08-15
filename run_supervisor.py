"""Top-level process supervisor: runs every registered model independently.

WHAT THIS REPLACES
------------------
Livne's runner used to launch Biton as two subprocesses inside its own cycle.
That worked, but it made one model the scheduler for another:

  * Livne dying, crashing, or simply not being started stopped Biton entirely.
  * Livne's `--asset-class` / `--top-n` were forwarded to Biton, so one
    model's CLI silently governed another model's scan scope.
  * Biton's cadence was a gate object living inside Livne's loop, so changing
    Livne's timeframe risked multiplying Biton's API load as a side effect.
  * Adding a third model meant editing Livne.

Here, no model knows any other exists. The supervisor reads `core.registry`
and gives each model its own scheduler thread, its own interval, its own
timeout, and its own CLI arguments.

ISOLATION MODEL
---------------
One thread per model; each thread launches that model's `live_runner.py` as a
`--once` subprocess when its interval comes due, and waits for it with a
timeout. Threads are independent, so a model that is slow, hung, or crashing
cannot delay another model's cycle -- which a sequential loop could not
guarantee.

The timeout is the load-bearing part. `subprocess.run` without one blocks
forever, and a hung child (typically a Binance/Yahoo socket that never
returns) is NOT an exception, so no try/except can rescue it. On timeout the
child is killed and that model simply picks up its next cycle.

Running models concurrently is safe by construction: separate processes,
separate import namespaces, per-model trade stores, per-model report-state
markers, per-model funnel prefixes, per-model chart filenames, and a yfinance
cache whose writes are atomic.

WHY `--once` PER TICK rather than one long-lived child
------------------------------------------------------
A per-tick child can be bounded by a timeout; a long-lived child that wedges
mid-cycle cannot be distinguished from one that is merely idle between cycles.
The trade-off is that in-memory state does not survive between cycles, which
is why interval gates that must outlive a process are file-persisted (see
`core.scheduling.PersistentIntervalGate`).

Usage:
    python run_supervisor.py                      # every registered model, forever
    python run_supervisor.py --models livne       # just one
    python run_supervisor.py --once               # one cycle each, then exit
    python run_supervisor.py --dry-run            # print the schedule, launch nothing
    python run_supervisor.py --asset-class crypto # forwarded to every model

Any of `--asset-class`, `--top-n`, `--candidate-pool`, `--skip-scan` that you
do NOT pass is simply not forwarded, so each model falls back to its own
config's defaults instead of inheriting another model's.

Stop with Ctrl+C; in-flight children are given a chance to exit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.maintenance import sweep_runtime  # noqa: E402
from core.registry import ModelConfig, all_models, get_model  # noqa: E402
from core.scheduling import PersistentIntervalGate  # noqa: E402
from core.settings import (  # noqa: E402
    CHART_RETENTION_DAYS,
    MAINTENANCE_INTERVAL_MINUTES,
    RUNTIME_CACHE_RETENTION_DAYS,
)

RUNTIME_DIR = os.path.join(REPO_ROOT, "runtime")

_stop = threading.Event()
# stderr is shared by every thread; without this, two models logging at the
# same instant interleave mid-line and the output becomes unreadable.
_print_lock = threading.Lock()


def _log(msg: str) -> None:
    with _print_lock:
        print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", file=sys.stderr, flush=True)


def _child_args(model: ModelConfig, args) -> list[str]:
    """The command line for one model's single cycle.

    Only flags the user EXPLICITLY passed to the supervisor are forwarded.
    Anything omitted is left to the model's own config defaults -- that is the
    whole point of decoupling: Livne's `--top-n` must never silently become
    Biton's.
    """
    cmd = [sys.executable, model.live_runner, "--once"]
    if args.asset_class is not None:
        cmd += ["--asset-class", args.asset_class]
    if args.top_n is not None:
        cmd += ["--top-n", str(args.top_n)]
    if args.candidate_pool is not None:
        cmd += ["--candidate-pool", str(args.candidate_pool)]
    if args.skip_scan:
        cmd.append("--skip-scan")
    return cmd


def run_model_cycle(model: ModelConfig, args) -> str:
    """Run one cycle of one model. Never raises -- returns a short outcome
    string for logging. Every failure mode here is contained to this model."""
    if not model.exists:
        return f"skipped (no live_runner at {model.live_runner})"

    cmd = _child_args(model, args)
    started = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=model.model_dir,  # each model's own directory is its sys.path root
            timeout=model.phase_timeout_seconds,
        )
        elapsed = time.monotonic() - started
        if result.returncode != 0:
            return f"exit code {result.returncode} after {elapsed:.0f}s (other models unaffected)"
        return f"ok in {elapsed:.0f}s"
    except subprocess.TimeoutExpired:
        # subprocess.run has already killed the child before raising.
        return (f"TIMEOUT after {model.phase_timeout_seconds}s -- child killed; "
                f"retries next cycle (other models unaffected)")
    except Exception as exc:  # noqa: BLE001 -- a supervisor must never die of a child
        return f"failed to launch: {exc} (other models unaffected)"


def _supervise(model: ModelConfig, args) -> None:
    """One model's scheduler loop: run immediately, then every interval.

    Fires immediately on start rather than waiting out a full interval, so a
    freshly started supervisor manages open positions right away instead of
    leaving them unattended for up to `cycle_interval_minutes`.
    """
    interval = max(float(model.cycle_interval_minutes), 0.1) * 60.0
    while not _stop.is_set():
        cycle_started = time.monotonic()
        _log(f"[{model.key}] cycle start")
        outcome = run_model_cycle(model, args)
        _log(f"[{model.key}] cycle done: {outcome}")

        if args.once or _stop.is_set():
            return

        # Schedule from cycle START, so a slow cycle does not push the model's
        # effective cadence out indefinitely. If a cycle overran the interval,
        # the next one begins immediately.
        sleep_for = max(0.0, interval - (time.monotonic() - cycle_started))
        if sleep_for:
            _log(f"[{model.key}] next cycle in {sleep_for / 60:.1f} min")
        # Wait on the stop event rather than time.sleep, so Ctrl+C is responsive
        # instead of blocking for a full interval.
        _stop.wait(sleep_for)


def _maintenance_loop() -> None:
    """Age-based retention for derived runtime artifacts.

    Lives in the supervisor rather than in a model because it is not any one
    model's concern -- the yfinance cache and the charts directory are shared.
    Gated on a persisted marker so restarting the supervisor does not re-run
    the sweep every time, and wrapped so a housekeeping failure can never take
    down the process that keeps the models running.
    """
    gate = PersistentIntervalGate(
        MAINTENANCE_INTERVAL_MINUTES,
        state_path=os.path.join(RUNTIME_DIR, "state", "gates_supervisor.json"),
        name="runtime_sweep",
    )
    while not _stop.is_set():
        try:
            if gate.due():
                _log("[maintenance] running retention sweep")
                sweep_runtime(RUNTIME_DIR, RUNTIME_CACHE_RETENTION_DAYS, CHART_RETENTION_DAYS)
        except Exception as exc:  # noqa: BLE001
            _log(f"[maintenance] sweep failed (models unaffected): {exc}")
        _stop.wait(300)  # re-check every 5 min; the gate decides if it actually runs


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run every registered TRADEIN model independently",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--models", help="comma-separated subset of model keys (default: all registered)")
    ap.add_argument("--once", action="store_true", help="run one cycle per model, then exit")
    ap.add_argument("--dry-run", action="store_true", help="print the schedule and the exact commands, launch nothing")
    ap.add_argument("--asset-class", choices=["stocks", "crypto", "both"], default=None,
                    help="forwarded to every model (default: each model's own config)")
    ap.add_argument("--top-n", type=int, default=None,
                    help="forwarded to every model (default: each model's own config)")
    ap.add_argument("--candidate-pool", type=int, default=None,
                    help="forwarded to every model (default: each model's own config)")
    ap.add_argument("--skip-scan", action="store_true",
                    help="forwarded to every model: refresh open positions only")
    args = ap.parse_args()

    if args.models:
        try:
            models = tuple(get_model(k.strip()) for k in args.models.split(",") if k.strip())
        except KeyError as exc:
            print(f"[supervisor] {exc}", file=sys.stderr)
            return 1
    else:
        models = all_models()

    if not models:
        print("[supervisor] no models registered -- nothing to run.", file=sys.stderr)
        return 1

    missing = [m.key for m in models if not m.exists]
    if missing:
        _log(f"[supervisor] WARNING: registered but not on disk, will be skipped: {', '.join(missing)}")

    _log(f"[supervisor] {len(models)} model(s): "
         + ", ".join(f"{m.key} every {m.cycle_interval_minutes}min "
                     f"(timeout {m.phase_timeout_seconds}s, route {m.telegram_route})" for m in models))

    if args.dry_run:
        for m in models:
            print(f"  {m.key:8} cwd={m.model_dir}")
            print(f"           cmd={' '.join(_child_args(m, args))}")
        _log("[supervisor] dry run -- nothing launched.")
        return 0

    threads = [
        threading.Thread(target=_supervise, args=(m, args), name=f"supervise-{m.key}", daemon=True)
        for m in models
    ]
    # Housekeeping runs alongside, never inside, a model cycle -- so a sweep
    # cannot delay position management. Skipped under --once, which is a
    # one-shot invocation rather than a long-running deployment.
    if not args.once:
        threads.append(threading.Thread(target=_maintenance_loop, name="maintenance", daemon=True))
    for t in threads:
        t.start()

    try:
        # Join with a timeout in a loop rather than a bare join: a bare join on
        # the main thread swallows KeyboardInterrupt on Windows.
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=0.5)
    except KeyboardInterrupt:
        _log("[supervisor] stopping (Ctrl+C) -- waiting for in-flight cycles...")
        _stop.set()
        for t in threads:
            t.join(timeout=30)
        _log("[supervisor] stopped.")
        return 0

    _log("[supervisor] all model schedules finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
