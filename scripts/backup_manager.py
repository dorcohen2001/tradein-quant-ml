"""Timestamped backup utility for the whole repository.

Why this exists: before this script the only restore points were ad-hoc folder
copies on the Desktop (inconsistently named, one of them byte-identical to a
git tag) plus a couple of git tags. Neither covered the thing that actually
cannot be reconstructed -- `runtime/`, which is gitignored and holds the live
paper-trading stores and Excel journals. A code rollback via git does NOT roll
back that state, and there was no copy of it anywhere.

Three archive kinds, one per subdirectory of `backups/`:

    full     code + configs + live runtime state. The restore point.
    runtime  ONLY the live trading state. Cheap enough to run on a schedule.
    code     source only, no state. For "what did the code look like before
             that refactor" without dragging 100 MB of state along.

What `full` deliberately EXCLUDES, and why -- these are the difference between
a ~10 MB archive and a ~120 MB one, and none of them are recoverable-only-here:

    .git/                 the repo's own history; backing it up inside itself
                          is circular, and `git bundle` is the right tool if
                          you ever want that.
    runtime/cache/        yfinance response cache (47 MB). Pure derived data,
                          re-fetched automatically on the next miss.
    runtime/charts/       rendered alert PNGs (64 MB). Re-rendered on demand,
                          and already delivered to Telegram.
    data/                 backtest candle cache. Re-downloadable from
                          Binance/Yahoo; explicitly gitignored for this reason.
    __pycache__/, *.pyc   build artifacts.
    backups/              never archive the archives.

SECRETS: a `full` archive intentionally CONTAINS the repo-root `.env`, because
a restore point that cannot restore credentials is not a restore point. The
archives live under `backups/`, which is gitignored, so they never reach a
remote -- but they are unencrypted on local disk. Pass `--exclude-secrets` if
you want an archive that is safe to copy off this machine, and the run will
tell you which mode it used either way.

Usage:
    python scripts/backup_manager.py --mode full
    python scripts/backup_manager.py --mode runtime --retention 30
    python scripts/backup_manager.py --mode code --exclude-secrets
    python scripts/backup_manager.py --list
    python scripts/backup_manager.py --verify-snapshot backups/full_snapshots/....zip

Exit codes: 0 = success, 1 = failure (a failed verify, an empty archive, ...).
Pure standard library on purpose -- a backup tool that needs `pip install`
first is a backup tool that fails on the day you actually need it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import subprocess
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_ROOT = os.path.join(REPO_ROOT, "backups")

MODE_DIRS = {
    "full": os.path.join(BACKUP_ROOT, "full_snapshots"),
    "runtime": os.path.join(BACKUP_ROOT, "runtime_state"),
    "code": os.path.join(BACKUP_ROOT, "code_only"),
}
MODE_PREFIXES = {
    "full": "TRADEIN_FULL_BACKUP_",
    "runtime": "runtime_",
    "code": "TRADEIN_CODE_",
}

MANIFEST_NAME = "BACKUP_MANIFEST.json"

# Directory names pruned during the walk, matched on the path RELATIVE to the
# repo root so that e.g. "data" only excludes <repo>/data and never a
# "models/<x>/data" that might exist later.
_EXCLUDED_RELDIRS = {
    "full": {".git", "backups", "data", "runtime/cache", "runtime/charts"},
    "code": {".git", "backups", "data", "runtime"},
    "runtime": set(),
}
# Directory basenames pruned anywhere in the tree.
_EXCLUDED_DIRNAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv", "node_modules"}
_EXCLUDED_FILE_GLOBS = ("*.pyc", "*.pyo", "*.tmp", "*~")
_SECRET_GLOBS = ("*.env", ".env")

# Live trading state -- the part of the tree that genuinely cannot be
# regenerated from anywhere else. `runtime` mode archives exactly this, and
# `verify` re-parses every .json among them to prove the archive is not just
# structurally intact but semantically readable.
RUNTIME_STATE_GLOBS = (
    "runtime/stores/*.json",
    "runtime/journals/*.xlsx",
    "runtime/state/*.json",
)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _rel(path: str) -> str:
    """Repo-relative path with forward slashes (zip entries are POSIX-style)."""
    return os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")


def _is_secret(rel_path: str) -> bool:
    base = os.path.basename(rel_path)
    return any(fnmatch.fnmatch(base, pat) for pat in _SECRET_GLOBS)


def _excluded_file(rel_path: str) -> bool:
    base = os.path.basename(rel_path)
    return any(fnmatch.fnmatch(base, pat) for pat in _EXCLUDED_FILE_GLOBS)


def _iter_tree(mode: str, include_secrets: bool) -> list[str]:
    """Absolute paths of every file the archive should contain, for a walk-based
    mode ("full"/"code")."""
    excluded_reldirs = _EXCLUDED_RELDIRS[mode]
    collected: list[str] = []

    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        rel_dir = _rel(dirpath)
        if rel_dir == ".":
            rel_dir = ""
        # Prune in place so os.walk never descends into them at all.
        dirnames[:] = [
            d for d in dirnames
            if d not in _EXCLUDED_DIRNAMES
            and ((f"{rel_dir}/{d}" if rel_dir else d) not in excluded_reldirs)
        ]
        for name in sorted(filenames):
            abs_path = os.path.join(dirpath, name)
            rel_path = _rel(abs_path)
            if _excluded_file(rel_path):
                continue
            if not include_secrets and _is_secret(rel_path):
                continue
            collected.append(abs_path)
    return sorted(collected)


def _iter_runtime_state() -> list[str]:
    """Absolute paths matching RUNTIME_STATE_GLOBS. A glob that matches nothing
    (e.g. `runtime/state/` has never been created) is simply skipped rather
    than being an error -- the set of state directories has grown over time and
    is expected to keep growing."""
    import glob as _glob

    collected: list[str] = []
    for pattern in RUNTIME_STATE_GLOBS:
        collected.extend(_glob.glob(os.path.join(REPO_ROOT, pattern)))
    return sorted(p for p in collected if os.path.isfile(p))


def _git_context() -> dict:
    """Commit/branch/dirty-state at archive time, so a snapshot can always be
    tied back to a point in history. Never fatal: a backup must still work if
    git is missing or the repo is in a weird state."""
    def _run(args: list[str]) -> str | None:
        try:
            out = subprocess.run(
                ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=15,
            )
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    status = _run(["status", "--porcelain"])
    return {
        "commit": _run(["rev-parse", "HEAD"]),
        "branch": _run(["rev-parse", "--abbrev-ref", "HEAD"]),
        "dirty": bool(status) if status is not None else None,
    }


def create_backup(mode: str, include_secrets: bool = True, retention: int = 10) -> str:
    """Write one archive and prune old ones. Returns the archive path."""
    out_dir = MODE_DIRS[mode]
    os.makedirs(out_dir, exist_ok=True)

    files = _iter_runtime_state() if mode == "runtime" else _iter_tree(mode, include_secrets)
    if not files:
        raise RuntimeError(f"mode={mode} matched no files -- refusing to write an empty archive")

    archive_path = os.path.join(out_dir, f"{MODE_PREFIXES[mode]}{_timestamp()}.zip")
    rel_files = [_rel(p) for p in files]
    secrets_included = [r for r in rel_files if _is_secret(r)]

    manifest = {
        "mode": mode,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "repo_root": REPO_ROOT,
        "file_count": len(files),
        "uncompressed_bytes": sum(os.path.getsize(p) for p in files),
        "includes_secrets": bool(secrets_included),
        "secret_files": secrets_included,
        # Every archived file that must still parse as JSON on restore. `verify`
        # actually re-parses these out of the zip -- a zip CRC proves the bytes
        # survived, it does not prove the trade store was valid when captured.
        "json_state_files": [r for r in rel_files if r.startswith("runtime/") and r.endswith(".json")],
        "git": _git_context(),
        "tool_version": 1,
    }

    # Write to a .part file and rename on success, so an interrupted run can
    # never leave a truncated archive sitting in the snapshots directory
    # looking like a valid restore point.
    partial_path = f"{archive_path}.part"
    try:
        with zipfile.ZipFile(partial_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for abs_path, rel_path in zip(files, rel_files):
                zf.write(abs_path, rel_path)
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
        os.replace(partial_path, archive_path)
    except BaseException:
        if os.path.exists(partial_path):
            os.remove(partial_path)
        raise

    size_mb = os.path.getsize(archive_path) / (1024 * 1024)
    _log(f"[backup] {mode}: {len(files)} files -> {_rel(archive_path)} ({size_mb:.1f} MB)")
    if secrets_included:
        _log(f"[backup] NOTE: archive contains {len(secrets_included)} secret file(s) "
             f"({', '.join(secrets_included)}). Keep it local; use --exclude-secrets for a "
             f"copy that is safe to move off this machine.")
    _prune(mode, retention)
    return archive_path


def _prune(mode: str, retention: int) -> list[str]:
    """Keep the newest `retention` archives for `mode`; delete the rest.

    Pruning is per-mode-directory and matches on this mode's filename prefix,
    so a runtime snapshot can never evict a full snapshot (the same
    per-prefix rule the funnel-log retention already uses)."""
    if retention <= 0:
        return []
    out_dir = MODE_DIRS[mode]
    prefix = MODE_PREFIXES[mode]
    if not os.path.isdir(out_dir):
        return []

    archives = [
        os.path.join(out_dir, n) for n in os.listdir(out_dir)
        if n.startswith(prefix) and n.endswith(".zip")
    ]
    archives.sort(key=os.path.getmtime, reverse=True)

    removed: list[str] = []
    for path in archives[retention:]:
        try:
            os.remove(path)
            removed.append(path)
            _log(f"[backup] retention({retention}): pruned {os.path.basename(path)}")
        except OSError as exc:
            _log(f"[backup] could not prune {path}: {exc}")
    return removed


def verify_snapshot(path: str) -> bool:
    """Integrity-check an archive. Three levels, because each catches a
    different failure:

      1. zip structure + per-entry CRC (`testzip`)  -> bit rot / truncation
      2. manifest file-count vs. actual entries     -> a partial write
      3. re-parse every archived runtime JSON       -> a store that was already
                                                       corrupt when captured
    """
    if not os.path.exists(path):
        _log(f"[verify] FAIL: no such archive: {path}")
        return False

    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                _log(f"[verify] FAIL: corrupt entry in archive: {bad}")
                return False

            names = set(zf.namelist())
            size_mb = os.path.getsize(path) / (1024 * 1024)
            _log(f"[verify] {os.path.basename(path)}: {len(names)} entries, {size_mb:.1f} MB, CRC OK")

            if MANIFEST_NAME not in names:
                _log("[verify] WARN: no manifest (archive predates this tool?) -- CRC check only.")
                return True

            manifest = json.loads(zf.read(MANIFEST_NAME))
            expected = manifest.get("file_count")
            actual = len(names) - 1  # the manifest itself is not a backed-up file
            if expected is not None and expected != actual:
                _log(f"[verify] FAIL: manifest says {expected} files, archive holds {actual}")
                return False

            _log(f"[verify] mode={manifest.get('mode')} created={manifest.get('created_at')} "
                 f"commit={(manifest.get('git') or {}).get('commit', '?')[:8]} "
                 f"dirty={(manifest.get('git') or {}).get('dirty')}")

            for rel_path in manifest.get("json_state_files", []):
                if rel_path not in names:
                    _log(f"[verify] FAIL: manifest lists {rel_path} but it is missing from the archive")
                    return False
                try:
                    json.loads(zf.read(rel_path))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    _log(f"[verify] FAIL: archived {rel_path} is not valid JSON: {exc}")
                    return False
            if manifest.get("json_state_files"):
                _log(f"[verify] {len(manifest['json_state_files'])} runtime JSON file(s) re-parsed OK")

            if manifest.get("includes_secrets"):
                _log("[verify] NOTE: this archive contains secret files -- keep it local.")
    except (zipfile.BadZipFile, OSError) as exc:
        _log(f"[verify] FAIL: {exc}")
        return False

    _log("[verify] PASS")
    return True


def list_snapshots() -> None:
    for mode, directory in MODE_DIRS.items():
        _log(f"\n=== {mode} ({_rel(directory)}) ===")
        if not os.path.isdir(directory):
            _log("  (none)")
            continue
        entries = sorted(
            (n for n in os.listdir(directory) if n.endswith(".zip")),
            key=lambda n: os.path.getmtime(os.path.join(directory, n)),
            reverse=True,
        )
        if not entries:
            _log("  (none)")
            continue
        for name in entries:
            full = os.path.join(directory, name)
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(full))
            _log(f"  {name}  {os.path.getsize(full) / (1024 * 1024):7.1f} MB  {mtime:%Y-%m-%d %H:%M:%S}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Timestamped backup manager for the TRADEIN repository",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--mode", choices=sorted(MODE_DIRS), help="which archive kind to create")
    ap.add_argument("--retention", type=int, default=10, metavar="N",
                    help="keep only the newest N archives of this mode (default: %(default)s; 0 disables pruning)")
    ap.add_argument("--exclude-secrets", action="store_true",
                    help="omit .env files, producing an archive safe to copy off this machine")
    ap.add_argument("--verify-snapshot", metavar="PATH", help="integrity-check an existing archive and exit")
    ap.add_argument("--list", action="store_true", help="list all existing archives and exit")
    args = ap.parse_args()

    if args.verify_snapshot:
        return 0 if verify_snapshot(args.verify_snapshot) else 1
    if args.list:
        list_snapshots()
        return 0
    if not args.mode:
        ap.error("one of --mode, --verify-snapshot or --list is required")

    try:
        path = create_backup(args.mode, include_secrets=not args.exclude_secrets, retention=args.retention)
    except Exception as exc:
        _log(f"[backup] FAILED: {exc}")
        return 1

    # Always verify what was just written. A backup that was never read back is
    # a guess, and the whole point of this file is to stop guessing.
    return 0 if verify_snapshot(path) else 1


if __name__ == "__main__":
    sys.exit(main())
