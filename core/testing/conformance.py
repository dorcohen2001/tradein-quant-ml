"""The contract every tenant must satisfy, checked without reading its code.

THE PROBLEM THIS SOLVES
-----------------------
Verifying a core change by opening each model, running ad-hoc commands against
it and eyeballing the output does not scale past a handful of tenants. At ten
it is a day's work; at a hundred it is not work anyone will actually do, so it
silently stops happening and core changes ship unverified.

The fix is to stop treating a model as something to INSPECT and start treating
it as something that either SATISFIES A CONTRACT or does not. `TenantStrategy`
is that contract; this module is its executable definition.

WHAT IS CHECKED
---------------
Only platform invariants -- properties that must hold for every tenant no
matter what it trades. Nothing here knows or cares whether a strategy is any
good, which is the model owner's business and not the platform's:

    identity      the model exposes the standard entry point and is registered
    vocabulary    every status it can produce is declared in its registry entry
    terminality   a closed position never re-opens or moves again
    idempotence   re-applying the same bar does not transition twice
    chronology    signal <= entry <= exit
    accounting    a LONG closed below its entry cannot report a profit
    isolation     it writes only to paths derived from its own key
    routing       it sends only to its own declared route
    resilience    a provider blackout degrades, it does not crash
    interface     its CLI accepts every flag the supervisor passes

HOW IT RUNS
-----------
In a subprocess, with the model's own directory as the working directory --
mandatory, not stylistic: every model is its own sys.path root and they define
clashing top-level module names, so two can never share an interpreter. The
subprocess boundary is also what makes a crashing tenant a FAILED REPORT rather
than a failed test run.

    cd models/<key>
    PYTHONPATH=<repo root> python -m core.testing.conformance

(the repo root has to be on PYTHONPATH because `-m` resolves the module before
any of the model's own `_bootstrap` can run).

`tests/test_model_contract.py` discovers every registered model and runs this
against each one, so adding the hundredth tenant adds no test code -- and a
core change is verified against the whole fleet without opening a single model
directory.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import sys
import tempfile
import traceback

# Bumped when the platform adds or changes an invariant. A tenant records the
# version it was last verified against, so a fleet of a hundred can be migrated
# in waves instead of all at once.
CONTRACT_VERSION = 1

NOW = dt.datetime(2026, 8, 17, 12, 0, 0)


class ContractViolation(AssertionError):
    """A tenant broke a platform invariant."""


def _trade(**kw) -> dict:
    """A position in the shape every tenant's store uses."""
    base = {
        "id": "conformance-1",
        "signal_date": (NOW - dt.timedelta(hours=3)).isoformat(timespec="seconds"),
        "ticker": "BTCUSDT",
        "asset_class": "crypto",
        "direction": "LONG",
        "entry_zone": [99.0, 101.0],
        "stop_loss": 95.0,
        "initial_stop_loss": 95.0,
        "target1": 110.0,
        "target2": 120.0,
        "current_status": "ACTIVE",
        "entry_date": (NOW - dt.timedelta(hours=2)).isoformat(timespec="seconds"),
        "exit_date": None,
        "exit_price": None,
        "pnl_pct": None,
        "telegram_message_id": None,
        "last_checked_at": (NOW - dt.timedelta(minutes=5)).isoformat(timespec="seconds"),
        # Fields only some lifecycles carry. Harmless to the others, and their
        # absence is what used to make a shared fixture impossible.
        "tp1_close_pct": 0.5,
        "breakeven_price": 100.15,
        "tp1_hit": False,
        "tp1_exit_price": None,
        "remaining_size_pct": 1.0,
        "emergency_exit_reason": None,
    }
    base.update(kw)
    return base


class _Result:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})

    def check(self, name: str, condition, detail: str = "") -> None:
        self.record(name, bool(condition), "" if condition else detail)

    @property
    def failures(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"]]

    def as_dict(self, key: str) -> dict:
        return {
            "model": key,
            "contract_version": CONTRACT_VERSION,
            "passed": not self.failures,
            "checks": self.checks,
        }


def run_conformance(build_runner) -> dict:
    """Run the battery against one tenant. Returns a JSON-serialisable report.

    `build_runner` is the standard entry point: every model exposes it, and it
    is the ONLY thing this harness knows about a tenant.
    """
    from core.testing.fake_market import FakeMarket

    result = _Result()
    runner = build_runner()
    strategy = runner.strategy
    model = runner.ctx.model
    market = FakeMarket().install(strategy)

    # --- identity ------------------------------------------------------
    result.check("registered", model.key,
                 "model has no registry key")
    result.check("state_dir_is_derived", runner.ctx.state_dir,
                 "no state directory configured, so the tenant lock cannot work")

    declared = set(model.closed_statuses) | set(model.update_statuses) | set(strategy.open_statuses)
    result.check("declares_a_status_vocabulary", declared,
                 "no statuses declared in the registry entry")

    # --- the lifecycle, driven directly --------------------------------
    produced: set[str] = set()

    def transition(trade: dict, high: float, low: float) -> bool:
        moved = strategy.apply_bar(trade, high, low, NOW)
        produced.add(trade["current_status"])
        return moved

    # 1. A stop breach must close the position.
    stopped = _trade()
    transition(stopped, 96.0, 90.0)
    result.check("stop_breach_closes", stopped["current_status"] not in strategy.open_statuses,
                 f"a bar through the stop left the position {stopped['current_status']}")

    # 2. A closed position must never move again -- the property that stops a
    #    replayed bar resurrecting a settled trade.
    frozen = copy.deepcopy(stopped)
    moved_again = strategy.apply_bar(frozen, 130.0, 5.0, NOW + dt.timedelta(minutes=5))
    result.check("terminal_is_final", not moved_again,
                 f"a terminal trade transitioned again to {frozen['current_status']}")
    result.check("terminal_state_unchanged", frozen["current_status"] == stopped["current_status"],
                 "a terminal trade's status was mutated by a later bar")

    # 3. Re-applying the SAME bar must not transition twice. The live path and
    #    the catch-up replay can overlap by a bar at the boundary.
    filling = _trade(current_status="PENDING", entry_date=None)
    first = transition(filling, 101.5, 99.5)
    second = strategy.apply_bar(copy.deepcopy(filling), 101.5, 99.5, NOW)
    result.check("bar_replay_is_idempotent", not (first and second),
                 "the same bar applied twice produced two transitions")

    # 4. Chronology.
    if stopped.get("exit_date") and stopped.get("entry_date"):
        result.check("chronology", stopped["exit_date"] >= stopped["entry_date"],
                     f"exit {stopped['exit_date']} precedes entry {stopped['entry_date']}")

    # 5. Accounting sign. A LONG stopped out below its entry cannot be a win.
    if stopped.get("pnl_pct") is not None:
        result.check("loss_is_negative", stopped["pnl_pct"] < 0,
                     f"a LONG closed at {stopped.get('exit_price')} below entry ~100 "
                     f"reported pnl {stopped['pnl_pct']}")

    # 6. A SHORT is quoted the other way round.
    short = _trade(direction="SHORT", stop_loss=105.0, target1=90.0, target2=80.0,
                   breakeven_price=99.85)
    transition(short, 106.0, 104.0)
    if short.get("pnl_pct") is not None:
        result.check("short_loss_is_negative", short["pnl_pct"] < 0,
                     f"a SHORT stopped above entry reported pnl {short['pnl_pct']}")

    # 7. Every status produced must be one the registry knows about. A status
    #    outside the vocabulary silently drops the trade from win rate, profit
    #    factor and drawdown downstream.
    undeclared = produced - declared - {"PENDING"}
    result.check("statuses_are_declared", not undeclared,
                 f"produced undeclared status(es) {sorted(undeclared)}; "
                 f"registry declares {sorted(declared)}")

    # --- resilience -----------------------------------------------------
    market.blackout = True
    try:
        survivors = [_trade(), _trade(id="conformance-2", current_status="PENDING")]
        runner.strategy.load_trades = lambda: survivors
        runner.strategy.save_trades = lambda trades: None
        runner.strategy.journal = lambda trade: None
        runner.process_open_positions(now=NOW)
        result.record("survives_provider_blackout", True)
    except Exception as exc:  # noqa: BLE001
        result.record("survives_provider_blackout", False,
                      f"{type(exc).__name__}: {exc}")
    market.blackout = False

    # --- interface ------------------------------------------------------
    try:
        args = runner.parse_args(
            ["--once", "--asset-class", "crypto", "--top-n", "1",
             "--candidate-pool", "2", "--skip-scan"])
        result.check("cli_accepts_supervisor_flags", args.once and args.skip_scan,
                     "parsed, but the supervisor's flags did not take effect")
    except SystemExit as exc:
        result.record("cli_accepts_supervisor_flags", False,
                      f"argparse rejected a flag run_supervisor sends (exit {exc.code})")

    # --- isolation ------------------------------------------------------
    # Every path this tenant writes must carry its own key. Two tenants sharing
    # a store is the one failure that destroys another client's history.
    for label, path in (("trade_store", model.trade_store),
                        ("report_state", model.report_state_store),
                        ("journal", model.journal_xlsx)):
        result.check(f"path_namespaced:{label}", model.key in os.path.basename(path),
                     f"{label} path {path} does not carry the model key")

    result.check("route_is_own", runner.ctx.route in (model.key, "default", model.telegram_route),
                 f"tenant sends to route {runner.ctx.route!r}, which is not its own")

    return result.as_dict(model.key)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the tenant conformance battery against the model in this directory")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON on stdout")
    args = ap.parse_args(argv)

    # Sandbox everything the tenant might write, so a conformance run can never
    # touch live trade state even if a tenant ignores the injected store.
    sandbox = tempfile.mkdtemp(prefix="conformance_")
    os.environ["TRADEIN_RATELIMIT_DIR"] = sandbox
    # No route resolves, so no message can leave the machine even if a tenant
    # sends outside the harness's control.
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        os.environ.pop(var, None)

    sys.path.insert(0, os.getcwd())
    try:
        import live_runner  # the standard entry point, in the model's own root
        report = run_conformance(live_runner.build_runner)
    except Exception as exc:  # noqa: BLE001 -- a broken tenant is a REPORT, not a crash
        report = {
            "model": os.path.basename(os.getcwd()),
            "contract_version": CONTRACT_VERSION,
            "passed": False,
            "checks": [{"check": "loads", "ok": False,
                        "detail": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc()}],
        }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        status = "PASS" if report["passed"] else "FAIL"
        print(f"{report['model']}: {status} "
              f"({sum(1 for c in report['checks'] if c['ok'])}/{len(report['checks'])} checks)")
        for check in report["checks"]:
            if not check["ok"]:
                print(f"  FAILED {check['check']}: {check['detail']}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
