"""CLI entry point: runs the daily market scan in DRY-RUN mode and prints the
top Telegram-ready trade setup(s) to the terminal. No Telegram alerts are sent
and no paper trades are logged — use live_runner.py for the live flow.

This is a thin wrapper around live_runner.run(dry_run=True) so the scan
pipeline exists in exactly one place.

Usage:
    python main.py --asset-class both --top-n 2
"""
from __future__ import annotations

import argparse

from config import TOP_N_ASSETS
from live_runner import run


def main():
    parser = argparse.ArgumentParser(description="Daily Financial & Crypto Market Analysis Pipeline (dry run)")
    parser.add_argument("--asset-class", choices=["stocks", "crypto", "both"], default="both",
                         help="Which market(s) to scan (default: both)")
    parser.add_argument("--top-n", type=int, default=TOP_N_ASSETS,
                         help="Max number of setups to output per asset class (default: %(default)s)")
    parser.add_argument("--candidate-pool", type=int, default=6,
                         help="How many top-scored candidates to evaluate before giving up (default: %(default)s)")
    args = parser.parse_args()

    reports = run(args.asset_class, args.top_n, args.candidate_pool, dry_run=True)

    if not reports:
        print("No high-conviction setups found today across the scanned watchlist.")
        return

    for report in reports:
        print(report)
        print("\n" + "=" * 60 + "\n")


if __name__ == "__main__":
    main()
