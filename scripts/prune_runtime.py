"""Manually run the runtime retention sweep.

The supervisor runs this automatically once a day (see `_maintenance_loop`),
but the sweep is useful on its own -- most obviously right now, on a
`runtime/` that has never been pruned, and afterwards for anyone who runs the
models directly rather than under the supervisor.

    python scripts/prune_runtime.py --dry-run     # show what would go
    python scripts/prune_runtime.py               # use the configured defaults
    python scripts/prune_runtime.py --cache-days 3 --chart-days 7

`--dry-run` first is the recommended habit: it reports exact counts and sizes
without touching anything.
"""
from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.maintenance import sweep_runtime  # noqa: E402
from core.settings import CHART_RETENTION_DAYS, RUNTIME_CACHE_RETENTION_DAYS  # noqa: E402


def _dir_size(path: str) -> tuple[int, float]:
    if not os.path.isdir(path):
        return (0, 0.0)
    total = 0
    count = 0
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if os.path.isfile(full):
            count += 1
            total += os.path.getsize(full)
    return (count, total / (1024 * 1024))


def main() -> int:
    ap = argparse.ArgumentParser(description="Prune derived runtime artifacts by age")
    ap.add_argument("--cache-days", type=float, default=RUNTIME_CACHE_RETENTION_DAYS,
                    help="max age for runtime/cache/yfinance (default: %(default)s; 0 disables)")
    ap.add_argument("--chart-days", type=float, default=CHART_RETENTION_DAYS,
                    help="max age for runtime/charts (default: %(default)s; 0 disables)")
    ap.add_argument("--dry-run", action="store_true", help="report what would be removed, delete nothing")
    args = ap.parse_args()

    runtime_dir = os.path.join(REPO_ROOT, "runtime")
    targets = {
        "yfinance cache": os.path.join(runtime_dir, "cache", "yfinance"),
        "charts": os.path.join(runtime_dir, "charts"),
    }

    print("before:")
    for label, path in targets.items():
        count, mb = _dir_size(path)
        print(f"  {label:15} {count:5} files  {mb:8.1f} MB")

    results = sweep_runtime(runtime_dir, args.cache_days, args.chart_days, dry_run=args.dry_run)

    if not args.dry_run:
        print("after:")
        for label, path in targets.items():
            count, mb = _dir_size(path)
            print(f"  {label:15} {count:5} files  {mb:8.1f} MB")

    total_files = sum(c for c, _ in results.values())
    total_mb = sum(b for _, b in results.values()) / (1024 * 1024)
    verb = "would reclaim" if args.dry_run else "reclaimed"
    print(f"{verb}: {total_files} file(s), {total_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
