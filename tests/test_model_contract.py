"""Every registered model, verified against the platform contract.

This file does not know that Livne or Biton exist. It enumerates the registry,
runs the conformance battery against each model IN ITS OWN SUBPROCESS, and
reports which invariants failed. Onboarding the hundredth tenant adds no code
here, and a core change is validated across the whole fleet without anyone
opening a model directory.

Two directions are tested, and both matter:

  * every registered tenant satisfies the contract;
  * the battery actually FAILS a tenant that violates it. A conformance suite
    that cannot fail is worse than none, because it manufactures confidence.

The subprocess is not stylistic. Models are separate sys.path roots defining
clashing top-level module names, so two can never be imported into one
interpreter -- and the boundary also turns a tenant that crashes on import into
a failed REPORT rather than a failed test session.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.registry import available_models  # noqa: E402
from core.testing.conformance import CONTRACT_VERSION, run_conformance  # noqa: E402

MODELS = available_models()


def _run_battery(model) -> dict:
    """Run the conformance module inside `model`'s own directory."""
    env = dict(os.environ)
    # `-m` resolves the module before the model's `_bootstrap` can run, so the
    # repo root has to be on the path up front.
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_CHAT_ID", None)

    proc = subprocess.run(
        [sys.executable, "-m", "core.testing.conformance", "--json"],
        cwd=model.model_dir, env=env, capture_output=True, text=True, timeout=300,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"{model.key}: conformance run produced no report\n"
                    f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")


@pytest.mark.parametrize("model", MODELS, ids=[m.key for m in MODELS])
def test_registered_model_satisfies_the_contract(model):
    """The whole point: a core change is verified against every tenant without
    touching, reading or hand-running any of them."""
    report = _run_battery(model)
    failures = [c for c in report["checks"] if not c["ok"]]
    assert report["passed"], (
        f"{model.key} violates the tenant contract:\n"
        + "\n".join(f"  - {c['check']}: {c['detail']}" for c in failures)
    )


@pytest.mark.parametrize("model", MODELS, ids=[m.key for m in MODELS])
def test_every_model_is_checked_against_the_current_contract(model):
    """A fleet migrates in waves; nothing may quietly stay on an old contract."""
    assert _run_battery(model)["contract_version"] == CONTRACT_VERSION


def test_the_registry_is_the_only_source_of_the_fleet():
    """If this file ever needs a hard-coded model name, the design has failed."""
    assert MODELS, "no models discovered -- the harness would silently test nothing"


# ==========================================================================
# The battery must be able to FAIL
# ==========================================================================
def _broken_runner(**overrides):
    """A synthetic tenant that violates the contract on purpose."""
    import datetime as dt

    from core.paper_trading.store import pnl_pct
    from core.registry import ModelConfig
    from core.runtime import TenantContext, TenantRunner, TenantStrategy

    class Broken(TenantStrategy):
        open_statuses = frozenset({"PENDING", "ACTIVE"})
        resurrect = overrides.get("resurrect", False)
        undeclared = overrides.get("undeclared", False)
        inverted_pnl = overrides.get("inverted_pnl", False)

        def load_trades(self):
            return []

        def save_trades(self, trades):
            pass

        def apply_bar(self, trade, high, low, ts):
            status = trade["current_status"]
            if status not in self.open_statuses and not self.resurrect:
                return False
            direction = trade["direction"]
            entry_low, entry_high = trade["entry_zone"]
            entry = (entry_low + entry_high) / 2
            stop = trade["stop_loss"]
            hit = low <= stop if direction == "LONG" else high >= stop
            if status == "PENDING":
                if low <= entry_high and high >= entry_low:
                    trade["current_status"] = "ACTIVE"
                    trade["entry_date"] = ts.isoformat(timespec="seconds")
                    return True
                return False
            if not hit:
                return False
            trade["current_status"] = "TOTALLY_MADE_UP" if self.undeclared else "LOSS_SL"
            trade["exit_date"] = ts.isoformat(timespec="seconds")
            trade["exit_price"] = stop
            trade["pnl_pct"] = (abs(pnl_pct(direction, entry, stop))
                                if self.inverted_pnl else pnl_pct(direction, entry, stop))
            return True

        def current_price(self, ticker, asset_class):
            return 100.0

    model = ModelConfig(
        key="client_broken", label="Broken tenant", cycle_interval_minutes=5,
        closed_statuses=("LOSS_SL", "EXPIRED"), update_statuses=(),
        closed_with_pnl=("LOSS_SL",), telegram_route="client_broken",
    )
    ctx = TenantContext(model=model, state_dir=os.path.join(REPO_ROOT, "runtime", "state"))
    return lambda: TenantRunner(ctx, Broken())


def test_a_conformant_tenant_passes():
    """The control. Without it, the failure tests below prove nothing."""
    report = run_conformance(_broken_runner())
    assert report["passed"], [c for c in report["checks"] if not c["ok"]]


def test_a_resurrecting_lifecycle_is_caught():
    """A terminal trade that transitions again would let a replayed bar reopen
    a settled position -- and the catch-up replay overlaps by design."""
    report = run_conformance(_broken_runner(resurrect=True))
    failed = {c["check"] for c in report["checks"] if not c["ok"]}
    assert not report["passed"]
    assert "terminal_is_final" in failed


def test_an_undeclared_status_is_caught():
    """A status outside the registry vocabulary silently drops the trade from
    win rate, profit factor and drawdown downstream -- the exact bug the
    registry docstring records as having already happened once."""
    report = run_conformance(_broken_runner(undeclared=True))
    failed = {c["check"] for c in report["checks"] if not c["ok"]}
    assert not report["passed"]
    assert "statuses_are_declared" in failed


def test_inverted_accounting_is_caught():
    """A LONG stopped out below its entry reporting a profit."""
    report = run_conformance(_broken_runner(inverted_pnl=True))
    failed = {c["check"] for c in report["checks"] if not c["ok"]}
    assert not report["passed"]
    assert "loss_is_negative" in failed


def test_a_tenant_that_cannot_even_load_is_reported_not_raised(tmp_path):
    """A broken model must produce a failing REPORT. If it took the test
    session down with it, one bad tenant would hide the other ninety-nine."""
    (tmp_path / "live_runner.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [sys.executable, "-m", "core.testing.conformance", "--json"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120,
    )
    report = json.loads(proc.stdout)

    assert report["passed"] is False
    assert any(c["check"] == "loads" and not c["ok"] for c in report["checks"])
    assert proc.returncode == 1
