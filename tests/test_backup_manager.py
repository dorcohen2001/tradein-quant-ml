"""Backup archive creation, verification, and retention.

The verifier is what these tests care about most. A backup that is never read
back is a guess, and the failure mode that matters is not a missing file --
it is an archive that LOOKS valid but whose trade store was already corrupt
when captured, which would only be discovered during a restore.
"""
from __future__ import annotations

import json
import os
import zipfile

import pytest

from scripts import backup_manager as bm


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A miniature repo, so tests never write into the real backups/ tree."""
    repo = tmp_path / "repo"
    (repo / "core").mkdir(parents=True)
    (repo / "runtime" / "stores").mkdir(parents=True)
    (repo / "runtime" / "cache" / "yfinance").mkdir(parents=True)
    (repo / "runtime" / "charts").mkdir(parents=True)
    (repo / ".git").mkdir()

    (repo / "core" / "registry.py").write_text("# source", encoding="utf-8")
    (repo / ".env").write_text("TELEGRAM_BOT_TOKEN=secret", encoding="utf-8")
    (repo / "runtime" / "stores" / "trades_livne.json").write_text(
        json.dumps([{"id": "a1", "current_status": "ACTIVE"}]), encoding="utf-8")
    (repo / "runtime" / "cache" / "yfinance" / "x.pkl").write_bytes(b"\x80cache")
    (repo / "runtime" / "charts" / "c.png").write_bytes(b"\x89PNG")
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")

    monkeypatch.setattr(bm, "REPO_ROOT", str(repo))
    monkeypatch.setattr(bm, "BACKUP_ROOT", str(repo / "backups"))
    monkeypatch.setattr(bm, "MODE_DIRS", {
        "full": str(repo / "backups" / "full_snapshots"),
        "runtime": str(repo / "backups" / "runtime_state"),
        "code": str(repo / "backups" / "code_only"),
    })
    return repo


def names(path):
    with zipfile.ZipFile(path) as zf:
        return set(zf.namelist())


# --------------------------------------------------------------------------
# Full snapshots
# --------------------------------------------------------------------------
def test_full_snapshot_captures_source_and_live_state(sandbox):
    archive = bm.create_backup("full")
    entries = names(archive)
    assert "core/registry.py" in entries
    assert "runtime/stores/trades_livne.json" in entries


def test_full_snapshot_excludes_the_bulky_recreatable_dirs(sandbox):
    """.git, the yfinance cache and rendered charts are the difference between
    a ~1 MB archive and a ~120 MB one, and all are reconstructible."""
    entries = names(bm.create_backup("full"))
    assert not any(e.startswith(".git/") for e in entries)
    assert not any(e.startswith("runtime/cache/") for e in entries)
    assert not any(e.startswith("runtime/charts/") for e in entries)


def test_full_snapshot_includes_secrets_by_default(sandbox):
    """A restore point that cannot restore credentials is not a restore point."""
    assert ".env" in names(bm.create_backup("full"))


def test_exclude_secrets_produces_a_portable_archive(sandbox):
    archive = bm.create_backup("full", include_secrets=False)
    assert ".env" not in names(archive)
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read(bm.MANIFEST_NAME))
    assert manifest["includes_secrets"] is False


def test_runtime_mode_captures_only_live_state(sandbox):
    entries = names(bm.create_backup("runtime"))
    assert "runtime/stores/trades_livne.json" in entries
    assert "core/registry.py" not in entries


def test_code_mode_excludes_runtime_entirely(sandbox):
    entries = names(bm.create_backup("code"))
    assert "core/registry.py" in entries
    assert not any(e.startswith("runtime/") for e in entries)


def test_manifest_records_git_context_and_counts(sandbox):
    archive = bm.create_backup("full")
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read(bm.MANIFEST_NAME))
    assert manifest["mode"] == "full"
    assert manifest["file_count"] == len(names(archive)) - 1  # manifest is not a backed-up file
    assert "runtime/stores/trades_livne.json" in manifest["json_state_files"]


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------
def test_verify_passes_on_a_fresh_archive(sandbox):
    assert bm.verify_snapshot(bm.create_backup("full")) is True


def test_verify_fails_on_a_missing_archive(sandbox):
    assert bm.verify_snapshot(str(sandbox / "nope.zip")) is False


def test_verify_fails_on_a_truncated_archive(sandbox):
    archive = bm.create_backup("full")
    with open(archive, "r+b") as fh:
        fh.truncate(os.path.getsize(archive) // 2)
    assert bm.verify_snapshot(archive) is False


def test_verify_catches_a_store_that_was_already_corrupt_when_captured(sandbox):
    """The failure a CRC check cannot see: the bytes survived perfectly, but
    they were not valid JSON to begin with. Only discovered at restore time
    unless the verifier re-parses."""
    (sandbox / "runtime" / "stores" / "trades_livne.json").write_text("{ truncated", encoding="utf-8")
    archive = bm.create_backup("full")
    assert bm.verify_snapshot(archive) is False


def test_verify_detects_a_file_removed_from_the_archive(sandbox):
    archive = bm.create_backup("full")
    with zipfile.ZipFile(archive) as zf:
        keep = [(n, zf.read(n)) for n in zf.namelist() if n != "core/registry.py"]
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in keep:
            zf.writestr(name, data)
    assert bm.verify_snapshot(archive) is False


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------
def test_retention_keeps_only_the_newest_n(sandbox):
    for _ in range(4):
        bm.create_backup("runtime", retention=2)
    kept = os.listdir(bm.MODE_DIRS["runtime"])
    assert len([k for k in kept if k.endswith(".zip")]) == 2


def test_retention_is_per_mode_so_modes_cannot_evict_each_other(sandbox):
    bm.create_backup("full", retention=1)
    for _ in range(3):
        bm.create_backup("runtime", retention=1)
    assert len([f for f in os.listdir(bm.MODE_DIRS["full"]) if f.endswith(".zip")]) == 1
    assert len([f for f in os.listdir(bm.MODE_DIRS["runtime"]) if f.endswith(".zip")]) == 1


def test_retention_zero_disables_pruning(sandbox):
    for _ in range(3):
        bm.create_backup("runtime", retention=0)
    assert len([f for f in os.listdir(bm.MODE_DIRS["runtime"]) if f.endswith(".zip")]) == 3


def test_no_partial_archive_is_left_behind(sandbox):
    bm.create_backup("full")
    assert not any(f.endswith(".part") for f in os.listdir(bm.MODE_DIRS["full"]))


def test_empty_selection_refuses_rather_than_writing_an_empty_archive(sandbox, monkeypatch):
    """An empty 'backup' that verifies clean is worse than a loud failure."""
    monkeypatch.setattr(bm, "RUNTIME_STATE_GLOBS", ("runtime/nothing/*.json",))
    with pytest.raises(RuntimeError):
        bm.create_backup("runtime")
