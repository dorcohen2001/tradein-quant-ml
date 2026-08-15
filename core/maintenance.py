"""Retention sweeps for derived runtime artifacts.

Two directories grew without any bound at all:

    runtime/cache/yfinance/   897 files / 47 MB at the time this was written.
                              `yf_cache` writes entries and expires them
                              logically (each pickle carries its own
                              `expires_at`) but never deletes the file, so the
                              directory only ever grew.
    runtime/charts/           125 PNGs / 64 MB. One render per alerted ticker
                              per day, each ~500 KB at 1680x1080 scale=2, kept
                              forever after being delivered to Telegram.

Only the funnel logs had retention (50 per model). This module gives the other
two the same treatment, on age rather than count -- these are caches keyed by
content, so "how old" is meaningful in a way that "how many" is not.

WHY AGE-BASED PRUNING IS SAFE HERE
----------------------------------
Nothing in this module deletes anything a running system still needs:

  * A yfinance cache entry is unusable once it is older than
    `YF_STALE_FALLBACK_SECONDS` (72h) -- past that even the emergency
    stale-fallback path refuses to serve it. The default 7-day floor is more
    than double that, so a pruned entry was already dead weight; the worst
    case is one extra API call on the next miss.
  * A chart PNG is delivered to Telegram at render time and is never read
    back by any code path. It is kept only for local inspection.

Both are re-creatable from scratch, which is the definition this repo already
uses for what may be deleted (see `data/` and the `.gitignore` rationale).

Every failure is non-fatal and per-file: a chart open in a viewer, a cache
entry being written by the other model right now, a permissions quirk -- each
is skipped, and the sweep continues. Retention is housekeeping and must never
be able to take down a live trading loop.
"""
from __future__ import annotations

import os
import sys
import time


def prune_by_age(directory: str, max_age_days: float, suffix: str | None = None,
                 dry_run: bool = False) -> tuple[int, int]:
    """Delete files in `directory` last modified more than `max_age_days` ago.

    Returns `(files_removed, bytes_reclaimed)`. Non-recursive by design -- both
    target directories are flat, and recursing would make an accidental
    misconfiguration far more destructive.

    `max_age_days <= 0` disables the sweep entirely and returns (0, 0), so a
    misread config can never mean "delete everything".
    """
    if max_age_days <= 0 or not os.path.isdir(directory):
        return (0, 0)

    cutoff = time.time() - (max_age_days * 86400)
    removed = 0
    reclaimed = 0

    try:
        entries = os.listdir(directory)
    except OSError as exc:
        print(f"[maintenance] cannot list {directory}: {exc}", file=sys.stderr)
        return (0, 0)

    for name in entries:
        if suffix and not name.endswith(suffix):
            continue
        path = os.path.join(directory, name)
        try:
            if not os.path.isfile(path):
                continue
            stat = os.stat(path)
            if stat.st_mtime >= cutoff:
                continue
            if not dry_run:
                os.remove(path)
            removed += 1
            reclaimed += stat.st_size
        except OSError:
            # Locked, already gone, or permission-denied. Best-effort by design.
            continue

    return (removed, reclaimed)


def sweep_runtime(runtime_dir: str, cache_days: float, chart_days: float,
                  dry_run: bool = False, candle_dir: str | None = None,
                  candle_days: float | None = None) -> dict[str, tuple[int, int]]:
    """Run every retention sweep. Returns `{label: (removed, bytes)}`.

    `candle_dir` is the `data/` tree written by `core.backtesting.CandleCache`.
    It is passed in rather than derived because it sits OUTSIDE `runtime/` (it
    is separately gitignored as re-downloadable derived data) and because the
    backtest CLIs accept a `--data-dir` override.
    """
    results = {
        "yfinance_cache": prune_by_age(
            os.path.join(runtime_dir, "cache", "yfinance"), cache_days, suffix=".pkl", dry_run=dry_run,
        ),
        "charts": prune_by_age(
            os.path.join(runtime_dir, "charts"), chart_days, suffix=".png", dry_run=dry_run,
        ),
    }

    # Candle cache. Every tenant runner writes here on every cycle, across every
    # timeframe it trades, and nothing pruned it before -- so it grew without
    # bound in direct proportion to how many tenants are running.
    #
    # Nested one directory per venue/symbol, so `prune_by_age` (deliberately
    # non-recursive) is applied per leaf rather than being made to recurse: a
    # recursive delete pointed at the wrong root is far more destructive than
    # this is verbose.
    if candle_dir and os.path.isdir(candle_dir):
        age = cache_days if candle_days is None else candle_days
        removed = reclaimed = 0
        for root, _dirs, _files in os.walk(candle_dir):
            for suffix in (".parquet", ".csv"):
                count, size = prune_by_age(root, age, suffix=suffix, dry_run=dry_run)
                removed += count
                reclaimed += size
        results["candle_cache"] = (removed, reclaimed)
    for label, (count, size) in results.items():
        if count:
            verb = "would remove" if dry_run else "removed"
            print(f"[maintenance] {label}: {verb} {count} file(s), "
                  f"{size / (1024 * 1024):.1f} MB", file=sys.stderr)
    return results
