"""CLI entry point: runs the end-to-end daily market scan and prints the
top Telegram-ready trade setup(s) to the terminal.

Usage:
    python main.py --asset-class both --top-n 2
"""
from __future__ import annotations

import argparse
import sys

from config import TOP_N_ASSETS
from generator.trade_generator import build_trade_setup, format_telegram_message
from macro.macro_analyzer import get_macro_context
from news.news_analyzer import analyze_sentiment
from screener.market_screener import scan_crypto, scan_stocks


def run(asset_class: str, top_n: int, candidate_pool: int) -> list[str]:
    reports: list[str] = []
    rejections: list[str] = []

    classes = ["stocks", "crypto"] if asset_class == "both" else [asset_class]

    for cls in classes:
        print(f"[macro] Analyzing {cls} market regime...", file=sys.stderr)
        macro = get_macro_context(cls)
        print(f"[macro] {macro.summary}", file=sys.stderr)

        print(f"[screener] Scanning {cls} watchlist...", file=sys.stderr)
        candidates = scan_stocks() if cls == "stocks" else scan_crypto()
        print(f"[screener] {len(candidates)} candidates passed liquidity/TA filters", file=sys.stderr)

        accepted = 0
        for candidate in candidates[:candidate_pool]:
            if accepted >= top_n:
                break
            print(f"[news] Fetching sentiment for {candidate.ticker}...", file=sys.stderr)
            news = analyze_sentiment(candidate.ticker, cls)

            setup, reason = build_trade_setup(candidate, macro, news)
            if setup is None:
                rejections.append(reason)
                continue

            reports.append(format_telegram_message(setup))
            accepted += 1

        if accepted == 0:
            print(f"[result] No qualifying {cls} setups today.", file=sys.stderr)

    if rejections:
        print("\n[filtered out]", file=sys.stderr)
        for r in rejections:
            print(f"  - {r}", file=sys.stderr)

    return reports


def main():
    parser = argparse.ArgumentParser(description="Daily Financial & Crypto Market Analysis Pipeline")
    parser.add_argument("--asset-class", choices=["stocks", "crypto", "both"], default="both",
                         help="Which market(s) to scan (default: both)")
    parser.add_argument("--top-n", type=int, default=TOP_N_ASSETS,
                         help="Max number of setups to output per asset class (default: %(default)s)")
    parser.add_argument("--candidate-pool", type=int, default=6,
                         help="How many top-scored candidates to evaluate before giving up (default: %(default)s)")
    args = parser.parse_args()

    reports = run(args.asset_class, args.top_n, args.candidate_pool)

    if not reports:
        print("No high-conviction setups found today across the scanned watchlist.")
        return

    for report in reports:
        print(report)
        print("\n" + "=" * 60 + "\n")


if __name__ == "__main__":
    main()
