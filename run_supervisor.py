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
and gives each ENABLED model its own scheduler thread, its own interval, its
own timeout, and its own CLI arguments.

Models marked `enabled=False` in the registry are not scheduled -- not even
when named via `--models`, since naming one on the command line is a weaker
statement than the deliberate pause recorded in the registry. Use
`--include-disabled` to override. Deactivation never touches stored state.

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
from core.observability import (  # noqa: E402
    format_age,
    heartbeat_age,
    is_stale,
    open_tenant_log,
    read_heartbeat,
    write_heartbeat,
)
from core.registry import ModelConfig, active_models, disabled_models, get_model  # noqa: E402
from core.scheduling import PersistentIntervalGate  # noqa: E402
from core.settings import (  # noqa: E402
    CANDLE_CACHE_DIR,
    FUNNEL_RETENTION_DAYS,
    LOG_BACKUP_COUNT,
    LOG_MAX_BYTES,
    LOG_RETENTION_DAYS,
    MACRO_SNAPSHOT_RETENTION_DAYS,
    CANDLE_CACHE_RETENTION_DAYS,
    BACKTEST_LOG_RETENTION_DAYS,
    CHART_RETENTION_DAYS,
    HEARTBEAT_ALERT_AFTER,
    MAINTENANCE_INTERVAL_MINUTES,
    OPS_TELEGRAM_ROUTE,
    RUNTIME_BACKUP_INTERVAL_MINUTES,
    RUNTIME_BACKUP_RETENTION,
    RUNTIME_CACHE_RETENTION_DAYS,
)

RUNTIME_DIR = os.path.join(REPO_ROOT, "runtime")
LOGS_DIR = os.path.join(RUNTIME_DIR, "logs")
STATE_DIR = os.path.join(RUNTIME_DIR, "state")

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


def _record_outcome(model: ModelConfig, status: str, detail: str = "",
                    duration_seconds: float | None = None) -> None:
    """Stamp the heartbeat and escalate if this tenant is repeatedly failing.

    The streak lives IN the heartbeat rather than in supervisor memory, for two
    reasons: it survives a supervisor restart (restarting during an outage must
    not re-arm an alert somebody already received), and `fleet_status.py` gets
    it for free.

    One alert per outage, not one per cycle. `alerted` latches when the message
    goes out and clears on the first ok cycle, which also emits a short recovery
    notice -- an operator who was told something broke is owed the news that it
    came back.
    """
    previous = read_heartbeat(STATE_DIR, model.key) or {}
    was_alerted = bool(previous.get("alerted"))

    if status == "ok":
        streak, alerted = 0, False
        if was_alerted:
            _notify_ops(f"✅ *{model.key}* recovered — cycle completed normally"
                        f" after {previous.get('failure_streak', '?')} failed cycle(s).")
            _log(f"[{model.key}] recovered after {previous.get('failure_streak', '?')} failed cycle(s)")
    else:
        streak = int(previous.get("failure_streak", 0)) + 1
        alerted = was_alerted
        if HEARTBEAT_ALERT_AFTER > 0 and not was_alerted and streak >= HEARTBEAT_ALERT_AFTER:
            _notify_ops(
                f"🚨 *{model.key}* has failed {streak} consecutive cycle(s).\n"
                f"Last status: `{status}`\n"
                f"Detail: {detail or 'none recorded'}\n"
                f"Positions are NOT being managed while this tenant is down."
            )
            alerted = True
            _log(f"[{model.key}] ESCALATED: {streak} consecutive failures -- ops alert sent")

    write_heartbeat(STATE_DIR, model.key, status=status, detail=detail,
                    duration_seconds=duration_seconds,
                    extra={"failure_streak": streak, "alerted": alerted})


def _notify_ops(message: str) -> None:
    """Send one operational alert. Never raises, never blocks a cycle.

    Imported lazily and wrapped: the escalation path exists to report that
    something is already broken, and it must not be able to become the thing
    that breaks the supervisor.
    """
    try:
        from core.utils.telegram_notifier import send_telegram_message

        result = send_telegram_message(message, route=OPS_TELEGRAM_ROUTE)
        if not result["ok"]:
            _log("[ops] escalation alert could NOT be delivered "
                 f"(route {OPS_TELEGRAM_ROUTE!r}) -- see the message above")
    except Exception as exc:  # noqa: BLE001
        _log(f"[ops] escalation alert failed: {type(exc).__name__}: {exc}")


def run_model_cycle(model: ModelConfig, args) -> str:
    """Run one cycle of one model. Never raises -- returns a short outcome
    string for logging. Every failure mode here is contained to this model.

    The child's stdout/stderr go to `runtime/logs/<key>.log` rather than being
    inherited: N tenants sharing one stream produce interleaved, unattributable
    output. The log is rotated here, immediately BEFORE the child opens it --
    renaming a file a running process holds fails on Windows, and this is the
    one moment no child has the handle.

    A heartbeat is stamped on every outcome, success or not, so the fleet view
    can distinguish "ran and found nothing" from "has not run since Tuesday".
    Repeated failures escalate to one Telegram alert -- see `_record_outcome`.
    """
    if not model.exists:
        _record_outcome(model, "skipped", "no live_runner on disk")
        return f"skipped (no live_runner at {model.live_runner})"

    cmd = _child_args(model, args)
    started = time.monotonic()
    handle = open_tenant_log(LOGS_DIR, model.key, max_bytes=LOG_MAX_BYTES, backups=LOG_BACKUP_COUNT)

    try:
        if handle is not None:
            handle.write(f"\n{'=' * 70}\n[cycle] {dt.datetime.now():%Y-%m-%d %H:%M:%S} "
                         f"{model.key} -> {' '.join(cmd[1:])}\n{'=' * 70}\n")
            handle.flush()
        try:
            result = subprocess.run(
                cmd,
                cwd=model.model_dir,  # each model's own directory is its sys.path root
                timeout=model.phase_timeout_seconds,
                # Falls back to inherited output if the log could not be opened:
                # losing log capture is an inconvenience, skipping a trading
                # cycle is not.
                stdout=handle, stderr=subprocess.STDOUT if handle is not None else None,
            )
            elapsed = time.monotonic() - started
            if result.returncode != 0:
                outcome = f"exit code {result.returncode} after {elapsed:.0f}s (other models unaffected)"
                _record_outcome(model, "error", outcome, elapsed)
                return outcome
            _record_outcome(model, "ok", duration_seconds=elapsed)
            return f"ok in {elapsed:.0f}s"
        except subprocess.TimeoutExpired:
            # subprocess.run has already killed the child before raising.
            outcome = (f"TIMEOUT after {model.phase_timeout_seconds}s -- child killed; "
                       f"retries next cycle (other models unaffected)")
            _record_outcome(model, "timeout", outcome, time.monotonic() - started)
            return outcome
        except Exception as exc:  # noqa: BLE001 -- a supervisor must never die of a child
            outcome = f"failed to launch: {exc} (other models unaffected)"
            _record_outcome(model, "error", str(exc))
            return outcome
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


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
        # Warn if this tenant has drifted past its schedule. Checked here,
        # after the cycle, so the warning names the model that is actually
        # behind rather than being a periodic sweep over everything.
        if is_stale(STATE_DIR, model.key, model.cycle_interval_minutes):
            _log(f"[{model.key}] WARNING: stale -- last successful heartbeat "
                 f"{format_age(heartbeat_age(STATE_DIR, model.key))} ago, "
                 f"interval is {model.cycle_interval_minutes} min")

        sleep_for = max(0.0, interval - (time.monotonic() - cycle_started))
        if sleep_for:
            _log(f"[{model.key}] next cycle in {sleep_for / 60:.1f} min")
        # Wait on the stop event rather than time.sleep, so Ctrl+C is responsive
        # instead of blocking for a full interval.
        _stop.wait(sleep_for)


def _take_runtime_snapshot() -> None:
    """Archive the live trading state, and verify what was written.

    This is the DISASTER-RECOVERY half of housekeeping, and it is the half that
    was missing: `runtime/` is gitignored, so rolling code back with git
    restores no trades, journals or report markers. `scripts/backup_manager.py`
    could always archive them; nothing ever ran it, so every restore point was
    whatever a human had remembered to take.

    A `runtime` archive is ~115 KB and takes under a second, which is why this
    can afford to be frequent AND to verify -- an archive that was never read
    back is a guess, and re-parsing the trade stores out of the zip is the
    failure a CRC alone cannot see.

    Imported lazily so a supervisor start never depends on the backup tool
    importing cleanly.
    """
    from scripts.backup_manager import create_backup, verify_snapshot

    path = create_backup("runtime", retention=RUNTIME_BACKUP_RETENTION)
    if verify_snapshot(path):
        _log(f"[backup] runtime snapshot verified: {os.path.relpath(path, REPO_ROOT)}")
    else:
        _log(f"[backup] WARNING: snapshot {os.path.relpath(path, REPO_ROOT)} FAILED verification")


def _maintenance_loop() -> None:
    """Retention sweeps and automated runtime snapshots.

    Lives in the supervisor rather than in a model because neither is any one
    model's concern -- the yfinance cache and the charts directory are shared,
    and a snapshot covers every tenant at once. Both are gated on persisted
    markers so restarting the supervisor does not re-run them every time, and
    each is wrapped separately so a housekeeping failure can never take down
    the process that keeps the models running -- nor can a failing sweep
    suppress the backup, which is the more important of the two.
    """
    gate_path = os.path.join(RUNTIME_DIR, "state", "gates_supervisor.json")
    sweep_gate = PersistentIntervalGate(
        MAINTENANCE_INTERVAL_MINUTES, state_path=gate_path, name="runtime_sweep",
    )
    backup_gate = PersistentIntervalGate(
        RUNTIME_BACKUP_INTERVAL_MINUTES, state_path=gate_path, name="runtime_backup",
    )
    while not _stop.is_set():
        try:
            if sweep_gate.due():
                _log("[maintenance] running retention sweep")
                sweep_runtime(RUNTIME_DIR, RUNTIME_CACHE_RETENTION_DAYS, CHART_RETENTION_DAYS,
                              candle_dir=CANDLE_CACHE_DIR,
                              candle_days=CANDLE_CACHE_RETENTION_DAYS,
                              log_days=LOG_RETENTION_DAYS,
                              funnel_days=FUNNEL_RETENTION_DAYS,
                              backtest_days=BACKTEST_LOG_RETENTION_DAYS,
                              snapshot_days=MACRO_SNAPSHOT_RETENTION_DAYS)
        except Exception as exc:  # noqa: BLE001
            _log(f"[maintenance] sweep failed (models unaffected): {exc}")

        try:
            if RUNTIME_BACKUP_INTERVAL_MINUTES > 0 and backup_gate.due():
                _log("[backup] taking scheduled runtime snapshot")
                _take_runtime_snapshot()
        except Exception as exc:  # noqa: BLE001
            _log(f"[backup] snapshot failed (models unaffected): {exc}")

        _stop.wait(300)  # re-check every 5 min; the gates decide if anything runs


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run every registered TRADEIN model independently",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--models", help="comma-separated subset of model keys (default: all ENABLED models)")
    ap.add_argument("--include-disabled", action="store_true",
                    help="also run models marked enabled=False in core/registry.py "
                         "(explicit override of a deliberate pause)")
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
        models = active_models()

    # Deactivated models are filtered out even when named explicitly. Naming a
    # model on the command line says "run this one", not "override the pause" --
    # the pause is the more deliberate statement of the two, and silently
    # honouring the CLI would make `--models biton` a way to restart a model
    # someone stopped on purpose. `--include-disabled` is the explicit override.
    if not args.include_disabled:
        paused = [m for m in models if not m.enabled]
        models = tuple(m for m in models if m.enabled)
        for m in paused:
            _log(f"[supervisor] {m.key} is DEACTIVATED in core/registry.py -- not scheduling it "
                 f"(its stored state is untouched; set enabled=True to resume)")

    # Always name every paused model, not just ones the caller asked for: the
    # failure mode of a flag like this is forgetting it is set.
    for m in disabled_models():
        if m not in models:
            _log(f"[supervisor] note: {m.key} ({m.label}) is currently disabled")

    if not models:
        print("[supervisor] no enabled models to run -- nothing to do.", file=sys.stderr)
        return 1

    missing = [m.key for m in models if not m.exists]
    if missing:
        _log(f"[supervisor] WARNING: registered but not on disk, will be skipped: {', '.join(missing)}")

    # Stage is shown because it changes REPORTING, not execution: an incubating
    # model runs on exactly the same footing as a production one, and only the
    # executive report treats them differently. Making that visible here stops
    # "why is my client strategy missing from the report" being a mystery.
    _log(f"[supervisor] {len(models)} model(s): "
         + ", ".join(f"{m.key} [{m.stage}] every {m.cycle_interval_minutes}min "
                     f"(timeout {m.phase_timeout_seconds}s, route {m.telegram_route}"
                     f"{', dedicated' if m.dedicated_route else ''})" for m in models))

    if RUNTIME_BACKUP_INTERVAL_MINUTES > 0:
        _log(f"[supervisor] runtime snapshots every {RUNTIME_BACKUP_INTERVAL_MINUTES / 60:g}h "
             f"(keeping {RUNTIME_BACKUP_RETENTION}) -> backups/runtime_state/")
    else:
        _log("[supervisor] WARNING: automatic runtime snapshots are DISABLED "
             "(RUNTIME_BACKUP_INTERVAL_MINUTES=0); runtime/ is gitignored, so nothing "
             "is backing up live trade state")

    if args.dry_run:
        for m in models:
            print(f"  {m.key:8} cwd={m.model_dir}")
            print(f"           cmd={' '.join(_child_args(m, args))}")
        _log(f"[supervisor] per-tenant logs -> "
             f"{os.path.join(os.path.relpath(LOGS_DIR, REPO_ROOT), '<key>.log')} "
             f"(rotated at {LOG_MAX_BYTES // (1024 * 1024)}MB, {LOG_BACKUP_COUNT} kept)")
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
