"""Every writable runtime artifact must be private to its tenant.

Written after a live incident: Livne and Horizon both resolved
MACRO_SNAPSHOT_STORE to `runtime/stores/market_snapshots.json` -- a path
literal that survived the per-model migration because, with only one MTFA
tenant, nothing ever collided. Once Horizon went live both processes did a
read-modify-write of that one file every two minutes and raced on os.replace,
which on Windows raises PermissionError (WinError 5). The write was fatal, so
the loser lost its whole asset-class scan: 23 of 1608 Horizon cycles and 12 of
1728 Livne cycles, and it would worsen with every tenant added.

Two independent guards, because either alone would have missed it:
  * paths are distinct per tenant (removes the race), and
  * a failed write is survivable (any lock -- AV, the 6-hourly backup zipping
    runtime/ -- must not be able to kill a scan).
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.registry import all_models  # noqa: E402

# Config attributes naming a file this model WRITES. A shared read-only cache
# is fine and deliberate (see core/utils/yf_cache.py: content-addressed by
# SHA1, atomic, non-fatal, and shared on purpose so one model's fetch warms
# the other's). Only mutable per-model series belong here.
WRITABLE_STORE_ATTRS = (
    "MACRO_SNAPSHOT_STORE",
    "GATE_STATE_STORE",
    "PAPER_TRADING_STORE",
    "REPORT_STATE_STORE",
)


def _load_model_config(key: str):
    """Import a tenant's config.py under its own sys.path, like the runner does."""
    model_dir = os.path.join(REPO_ROOT, "models", key)
    if not os.path.isdir(model_dir):
        pytest.skip(f"model {key} has no directory")
    added = [p for p in (model_dir, os.path.join(model_dir, "macro")) if p not in sys.path]
    sys.path[:0] = added
    for stale in ("config", "macro_analyzer"):
        sys.modules.pop(stale, None)
    try:
        return importlib.import_module("config")
    finally:
        for p in added:
            sys.path.remove(p)
        sys.modules.pop("config", None)


@pytest.mark.parametrize("attr", WRITABLE_STORE_ATTRS)
def test_writable_stores_are_never_shared_between_tenants(attr):
    owners: dict[str, list[str]] = {}
    for model in all_models():
        cfg = _load_model_config(model.key)
        path = getattr(cfg, attr, None)
        if path is None:
            continue
        owners.setdefault(os.path.normcase(os.path.abspath(path)), []).append(model.key)

    shared = {path: keys for path, keys in owners.items() if len(keys) > 1}
    assert not shared, (
        f"{attr} is shared by multiple tenants -- concurrent read-modify-write "
        f"will race on os.replace: "
        + "; ".join(f"{os.path.basename(p)} <- {', '.join(k)}" for p, k in shared.items())
    )


@pytest.mark.parametrize("attr", WRITABLE_STORE_ATTRS)
def test_writable_store_filenames_carry_the_model_key(attr):
    """A new tenant must inherit isolation, not have to remember to add it."""
    for model in all_models():
        cfg = _load_model_config(model.key)
        path = getattr(cfg, attr, None)
        if path is None:
            continue
        assert model.key in os.path.basename(path), (
            f"{model.key}'s {attr} is {os.path.basename(path)!r}, which does not "
            f"contain the model key -- a future tenant would collide with it"
        )


def test_a_locked_snapshot_file_does_not_kill_the_scan(monkeypatch, tmp_path):
    """os.replace failing must degrade trend precision, never raise."""
    sys.path[:0] = [os.path.join(REPO_ROOT, "models", "livne"),
                    os.path.join(REPO_ROOT, "models", "livne", "macro")]
    for stale in ("config", "macro_analyzer"):
        sys.modules.pop(stale, None)
    try:
        macro_analyzer = importlib.import_module("macro_analyzer")
        target = str(tmp_path / "market_snapshots_livne.json")
        monkeypatch.setattr(macro_analyzer, "MACRO_SNAPSHOT_STORE", target)

        def denied(_src, _dst):
            raise PermissionError(5, "Access is denied")

        monkeypatch.setattr(os, "replace", denied)

        macro_analyzer._save_snapshots([{"ts": "2026-08-19T00:00:00+00:00"}])  # must not raise

        assert not list(tmp_path.glob("*.tmp")), "a failed write stranded its temp file"
    finally:
        for stale in ("config", "macro_analyzer"):
            sys.modules.pop(stale, None)
        del sys.path[:2]


def test_interrupts_still_propagate_through_the_snapshot_writer(monkeypatch, tmp_path):
    """Swallowing OSError must not have swallowed Ctrl+C along with it."""
    sys.path[:0] = [os.path.join(REPO_ROOT, "models", "livne"),
                    os.path.join(REPO_ROOT, "models", "livne", "macro")]
    for stale in ("config", "macro_analyzer"):
        sys.modules.pop(stale, None)
    try:
        macro_analyzer = importlib.import_module("macro_analyzer")
        monkeypatch.setattr(macro_analyzer, "MACRO_SNAPSHOT_STORE",
                            str(tmp_path / "market_snapshots_livne.json"))

        def interrupted(_src, _dst):
            raise KeyboardInterrupt

        monkeypatch.setattr(os, "replace", interrupted)

        with pytest.raises(KeyboardInterrupt):
            macro_analyzer._save_snapshots([{"ts": "2026-08-19T00:00:00+00:00"}])

        assert not list(tmp_path.glob("*.tmp"))
    finally:
        for stale in ("config", "macro_analyzer"):
            sys.modules.pop(stale, None)
        del sys.path[:2]
