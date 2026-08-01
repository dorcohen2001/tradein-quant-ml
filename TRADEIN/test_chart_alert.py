"""End-to-end test of the chart-attached Telegram alert flow.

Builds a sample TradeSetup (no scanner/news APIs needed), formats the exact
message live_runner would send, renders the chart via ../graphs, and — with
--send — pushes photo + text to the configured Telegram chat using the same
dispatch logic as live_runner.

Usage:
    python test_chart_alert.py          # dry-run: format + render chart only
    python test_chart_alert.py --send   # also send to Telegram
"""
from __future__ import annotations

import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from generator.trade_generator import TradeSetup, format_telegram_message
from utils.chart_attachment import generate_chart_for_setup
from utils.telegram_notifier import send_telegram_message, send_telegram_photo

SAMPLE_SETUP = TradeSetup(
    ticker="BTCUSDT",
    asset_class="crypto",
    direction="SHORT",
    entry_low=64189.35,
    entry_high=64639.58,
    stop=65023.50,
    target1=59130.91,
    target2=57800.19,
    rr1=7.35,
    rr2=9.24,
    macro_summary=(
        "BTC Downtrend ($64,315.99), ETH Mixed ($1,904.89), USDT.D 8.08% "
        "Falling/Stable (risk-on) -> Regime: Neutral"
    ),
    thesis=(
        "Price hugging EMA50 (-0.99%); Inside Fibonacci golden zone "
        "(6.409e+04-6.604e+04); Support near 5.913e+04; Resistance near "
        "6.47e+04; Trend: DOWNTREND, RSI 50.3, MACD bearish"
    ),
    news_label="Negative",
    news_reason="negative sentiment aligns with SHORT direction",
    top_headlines=[
        "Bitcoin bounces to $64,300 but the real move waits on the Fed - CoinDesk",
        "Bitcoin rises toward $64,000 amid Korea's record chip crash - CoinDesk",
        "Live updates: Bitcoin rises above $64,000 ahead of Fed rate decision - CoinDesk",
    ],
    score=66.5,
    fib_zone=(64090.0, 66040.0),
    supports=[59130.91],
    resistances=[64700.0],
)


def main() -> None:
    ap = argparse.ArgumentParser(description="Chart-attached alert end-to-end test")
    ap.add_argument("--send", action="store_true", help="actually send to Telegram")
    args = ap.parse_args()

    message = format_telegram_message(SAMPLE_SETUP)
    print(f"[test] Formatted message: {len(message)} chars "
          f"(caption limit is 1024 -> {'fits' if len(message) <= 1024 else 'photo + follow-up text'})")

    chart_path = generate_chart_for_setup(SAMPLE_SETUP)
    if chart_path:
        print(f"[test] Chart rendered: {chart_path}")
    else:
        print("[test] Chart generation FAILED (alert would fall back to text-only).")

    if not args.send:
        print("[test] Dry-run complete (pass --send to push to Telegram).")
        return

    # mirror live_runner's dispatch logic exactly
    if chart_path:
        tg_result = send_telegram_photo(chart_path, caption=message)
        if not tg_result["ok"]:
            print("[test] Photo alert failed — falling back to text-only.")
            tg_result = send_telegram_message(message)
    else:
        tg_result = send_telegram_message(message)

    print(f"[test] Telegram send: {'OK' if tg_result['ok'] else 'FAILED'} "
          f"(message_id={tg_result['message_id']})")


if __name__ == "__main__":
    main()
