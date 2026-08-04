"""CLI entry point: turn a structured trade-setup text into a chart PNG.

Usage:
    python generate_chart.py example_setup.txt
    python generate_chart.py --text "DAILY TRADE SETUP — LTCUSDT ..."
    type setup.txt | python generate_chart.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from core.charting.data_fetcher import fetch_ohlcv
from core.charting.chart_renderer import render_chart
from core.charting.setup_parser import parse_setup


def main() -> None:
    ap = argparse.ArgumentParser(description="Trade-setup chart generator")
    ap.add_argument("file", nargs="?", help="path to a text file with the setup")
    ap.add_argument("--text", help="pass the setup text inline instead of a file")
    args = ap.parse_args()

    if args.text:
        text = args.text
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        ap.error("provide a setup file, --text, or pipe text via stdin")

    setup = parse_setup(text)
    print(f"Parsed setup: {setup.asset} ({setup.yf_symbol}) | {setup.tf_label}"
          f" | direction={setup.direction or 'n/a'}")
    print(f"  EMAs={setup.emas} SMAs={setup.smas} RSI={setup.show_rsi} "
          f"MACD={setup.show_macd}")
    print(f"  fib_zone={setup.fib_zone} entry={setup.entry_zone} "
          f"targets={setup.targets} SL={setup.stop_loss}")
    print(f"  supports={setup.supports} resistances={setup.resistances}")

    df = fetch_ohlcv(setup)
    print(f"Fetched {len(df)} {setup.tf_label} bars "
          f"({df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d})")

    out_name = f"{setup.asset}_{setup.tf_label}_{dt.date.today():%Y-%m-%d}.png"
    out_path = Path(__file__).resolve().parent / out_name
    render_chart(setup, df, str(out_path))
    print(f"Chart saved: {out_path}")


if __name__ == "__main__":
    main()
