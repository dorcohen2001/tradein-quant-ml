"""End-to-end test of the chart-attached Telegram alert flow, for either model.

Builds a sample TradeSetup (no scanner/news/market APIs needed to construct
it), formats the exact message that model's live_runner would send, renders
the chart through `core.utils.chart_attachment`, and -- with --send -- pushes
photo + text to the configured Telegram chat using the same dispatch logic as
live_runner.

    python core/charting/test_chart_alert.py                  # Livne, dry run
    python core/charting/test_chart_alert.py --model biton    # Biton, dry run
    python core/charting/test_chart_alert.py --send           # also send to Telegram

Why a --model flag: the two models have genuinely different TradeSetup
schemas. Livne's carries the multi-tier TP1/breakeven fields, the structured
divergence/liquidity/correlation notes, and a PositionSizeEstimate; Biton's
is the older flat shape. The previous version of this script lived in both
codebases as a byte-identical copy hard-coded to Biton's schema, so under
Livne it raised `TypeError: TradeSetup.__init__() missing 6 required
positional arguments` the moment it was imported -- it had been dead on
arrival there. Building the sample per model is what keeps it honest.

This is a test harness, not library code, which is why it is allowed to reach
into `models/` -- nothing in `core` proper may do that.
"""
from __future__ import annotations

import argparse
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.utils.chart_attachment import generate_chart_for_setup
from core.utils.telegram_notifier import send_telegram_message, send_telegram_photo

_SAMPLE_HEADLINES = [
    "Bitcoin bounces to $64,300 but the real move waits on the Fed - CoinDesk",
    "Bitcoin rises toward $64,000 amid Korea's record chip crash - CoinDesk",
    "Live updates: Bitcoin rises above $64,000 ahead of Fed rate decision - CoinDesk",
]
_SAMPLE_MACRO = (
    "BTC Downtrend ($64,315.99), ETH Mixed ($1,904.89), USDT.D 8.08% "
    "Falling/Stable (risk-on) -> Regime: Neutral"
)


def _load_model(model: str):
    """Put the chosen model's directory first on sys.path and import its
    TradeSetup, message formatter, and data fetchers. Only one model is ever
    loaded per process -- the two define clashing top-level module names
    (`config`, `generator`, `utils`) and must never be imported together."""
    model_dir = os.path.join(REPO_ROOT, "models", model)
    if not os.path.isdir(model_dir):
        raise SystemExit(f"no such model directory: {model_dir}")
    sys.path.insert(0, model_dir)

    from generator.trade_generator import TradeSetup, format_telegram_message
    from utils.data_fetch import get_binance_klines, get_stock_history

    return TradeSetup, format_telegram_message, get_binance_klines, get_stock_history


def _build_sample(model: str, TradeSetup):
    """A representative SHORT crypto setup in whichever schema `model` uses."""
    common = dict(
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
        macro_summary=_SAMPLE_MACRO,
        news_label="Negative",
        news_reason="negative sentiment aligns with SHORT direction",
        top_headlines=_SAMPLE_HEADLINES,
        score=66.5,
        fib_zone=(64090.0, 66040.0),
        supports=[59130.91],
        resistances=[64700.0],
    )

    if model == "biton":
        return TradeSetup(
            thesis=(
                "Price hugging EMA50 (-0.99%); Inside Fibonacci golden zone "
                "(6.409e+04-6.604e+04); Support near 5.913e+04; Resistance near "
                "6.47e+04; Trend: DOWNTREND, RSI 50.3, MACD bearish"
            ),
            **common,
        )

    # Livne: multi-tier TP lifecycle + structured MTFA notes + sizing.
    from risk.risk_manager import position_size_estimate

    entry_price = (common["entry_low"] + common["entry_high"]) / 2
    return TradeSetup(
        thesis=(
            "HTF (DOWNTREND) resistance zone 6.47e+04-6.51e+04; Bearish (distribution) "
            "RSI divergence confirmed on weekly; low-volume weekly retest at zone (0.82x avg); "
            "4H pin_bar_rejection braking/reversal event on 1.24x volume, 3/3 ITF timeframes "
            "200MA-aligned, reacted off 0.5/0.618 Fib level"
        ),
        tp1_close_pct=0.5,
        breakeven_price=entry_price * (1 - 0.0015),
        divergence_note=(
            "Distribution (bearish) RSI divergence confirmed on weekly: price high "
            "63980.0 -> 64720.0, RSI high 71.4 -> 66.2"
        ),
        liquidity_note="Orderbook liquidity concentration agrees with the SHORT thesis",
        correlation_note="Regime: Neutral (direction confirmed within ['LONG', 'SHORT'])",
        position_size=position_size_estimate(entry_price, common["stop"]),
        ob_zone=(64850.0, 65120.0),
        **common,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Chart-attached alert end-to-end test")
    ap.add_argument("--model", choices=["livne", "biton"], default="livne",
                    help="which model's TradeSetup schema / data layer to exercise (default: %(default)s)")
    ap.add_argument("--send", action="store_true", help="actually send to Telegram")
    args = ap.parse_args()

    TradeSetup, format_telegram_message, fetch_crypto, fetch_stock = _load_model(args.model)
    from config import CHART_OUTPUT_DIR

    setup = _build_sample(args.model, TradeSetup)
    message = format_telegram_message(setup)
    print(f"[test] model={args.model}: formatted message is {len(message)} chars "
          f"(caption limit 1024 -> {'fits' if len(message) <= 1024 else 'photo + follow-up text'})")

    chart_path = generate_chart_for_setup(
        setup, fetch_crypto=fetch_crypto, fetch_stock=fetch_stock, output_dir=CHART_OUTPUT_DIR,
    )
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
