"""One-off migration: stamp `last_checked_at` on open positions that lack it.

    python scripts/backfill_last_checked.py              # dry run (default)
    python scripts/backfill_last_checked.py --apply
    python scripts/backfill_last_checked.py --apply --models livne

WHY THIS IS NEEDED
------------------
`last_checked_at` is written by the live loop on every evaluated trade, and it
is what `core.paper_trading.catch_up` uses to decide how much price action a
restart missed. Every position written BEFORE that field existed does not carry
it, and `last_checked_at()` then falls through to `last_status_change` ->
`entry_date` -> `signal_date`.

For a position opened days ago those fallbacks are days old, so the first run
after this feature ships sees a multi-day gap on every open trade, clamps it to
`max_lookback_hours` (72h) and replays roughly 860 five-minute bars per
position. Two things go wrong with that:

  * a burst of historical fetches on the first cycle, against the same
    providers the live path needs right then;
  * far worse, bars the live loop ALREADY evaluated get re-graded. A level the
    live pass genuinely did not breach is fine -- but a wick the old fixed
    2-minute refresh window missed will now close the position, at a historical
    timestamp, and arrive in the catch-up summary as a resolution that
    "happened" days ago.

Stamping `last_checked_at = now` says the honest thing: these positions are
current as of this moment, and only price action from here on is unexamined.
The trades themselves are not touched -- no status, price, or P&L is altered.

DELIBERATELY DOES NOT OVERWRITE
-------------------------------
A record that already HAS `last_checked_at` is left exactly as it is. That
value describes a real gap, and overwriting it with `now` would erase evidence
of an outage the catch-up is supposed to find. This migration only fills in
what is missing, so running it twice is a no-op.

Closed positions are skipped: nothing ever re-examines them, and writing a
"last checked" time onto a trade that closed in July would be a lie in the
store.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.locking import TenantBusy, TenantLock  # noqa: E402
from core.paper_trading.catch_up import OPEN_STATUSES  # noqa: E402
from core.paper_trading.store import load_trades, save_trades  # noqa: E402
from core.registry import all_models, get_model  # noqa: E402

STATE_DIR = os.path.join(REPO_ROOT, "runtime", "state")

FIELD = "last_checked_at"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def backfill_store(path: str, now: dt.datetime, apply: bool) -> dict:
    """Fill `last_checked_at` on open records in one store.

    Returns counts. Never migrates on load -- this reads the raw records,
    because a per-model schema migration is the owning model's business and
    running Livne's over Biton's store is exactly the cross-contamination the
    registry exists to prevent.
    """
    trades = load_trades(path)
    stamp = now.isoformat(timespec="seconds")

    counts = {"total": len(trades), "open": 0, "already": 0, "filled": 0}
    for trade in trades:
        if trade.get("current_status") not in OPEN_STATUSES:
            continue
        counts["open"] += 1
        if trade.get(FIELD):
            counts["already"] += 1
            continue
        counts["filled"] += 1
        if apply:
            trade[FIELD] = stamp

    if apply and counts["filled"]:
        save_trades(path, trades)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill last_checked_at on open paper-trading positions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry run that changes nothing)")
    ap.add_argument("--models", help="comma-separated model keys (default: every registered model)")
    args = ap.parse_args()

    if args.models:
        try:
            models = [get_model(k.strip()) for k in args.models.split(",") if k.strip()]
        except KeyError as exc:
            _log(f"[backfill] {exc}")
            return 1
    else:
        # EVERY registered model, including disabled ones: Biton is paused with
        # 13 non-terminal positions, and those are precisely the records that
        # would trigger a 72h replay whenever it is re-enabled.
        models = list(all_models())

    now = dt.datetime.now()
    mode = "APPLY" if args.apply else "DRY RUN"
    _log(f"[backfill] {mode} -- stamping {FIELD} = {now.isoformat(timespec='seconds')}")

    total_filled = 0
    for model in models:
        if not os.path.exists(model.trade_store):
            _log(f"  {model.key:14} no store yet -- skipped")
            continue

        # Take the tenant lock: this rewrites the same store the runner writes,
        # and doing that while a cycle is mid-flight is the lost update this
        # lock exists to prevent.
        try:
            with TenantLock(STATE_DIR, model.key):
                counts = backfill_store(model.trade_store, now, apply=args.apply)
        except TenantBusy as exc:
            _log(f"  {model.key:14} SKIPPED -- {exc}")
            continue
        except RuntimeError as exc:  # a corrupt store must be loud, not skipped
            _log(f"  {model.key:14} FAILED -- {exc}")
            return 1

        total_filled += counts["filled"]
        verb = "stamped" if args.apply else "would stamp"
        _log(f"  {model.key:14} {counts['total']:4d} trades, {counts['open']:3d} open, "
             f"{counts['already']:3d} already stamped, {verb} {counts['filled']:3d}")

    if not args.apply:
        _log(f"[backfill] dry run complete -- {total_filled} record(s) would be stamped. "
             f"Re-run with --apply to write.")
    else:
        _log(f"[backfill] done -- {total_filled} record(s) stamped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
