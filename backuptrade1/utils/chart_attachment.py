"""Bridge to the sibling ./Graphs chart renderer.

Builds the renderer's TradeSetup directly from the structured
`generator.trade_generator.TradeSetup` — no round-trip through the formatted
Telegram text and no regex parsing. OHLCV comes from the same source the
signal was computed on (Binance for crypto, yfinance for stocks), so chart
levels always match the alert. Designed to never break the alert flow: any
failure is logged and reported as None so the caller can fall back to a
text-only Telegram message.

The Graphs project's own text parser (`setup_parser.py`) remains in use only
for the manual `generate_chart.py` CLI.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

from config import GRAPHS_DIR

_CRYPTO_QUOTES = ("USDT", "USDC", "BUSD", "PERP")


def _ensure_graphs_importable() -> None:
    if not os.path.isdir(GRAPHS_DIR):
        raise FileNotFoundError(f"Graphs project not found at {GRAPHS_DIR}")
    # appended (not prepended) so TRADEIN's own modules always win on name clashes
    if GRAPHS_DIR not in sys.path:
        sys.path.append(GRAPHS_DIR)


def generate_chart_for_setup(setup) -> str | None:
    """Render a chart PNG for a `generator.trade_generator.TradeSetup`.

    Returns the absolute path of the saved image inside GRAPHS_DIR, or None
    if anything failed (missing data, unknown ticker, render error, ...).
    """
    try:
        _ensure_graphs_importable()
        from chart_renderer import render_chart
        from setup_parser import TradeSetup as ChartSetup

        if setup.asset_class == "crypto":
            from utils.data_fetch import get_binance_klines
            df = get_binance_klines(setup.ticker, interval="1d", limit=600)
            base = setup.ticker
            for q in _CRYPTO_QUOTES:
                if base.endswith(q):
                    base = base[: -len(q)]
                    break
            yf_symbol = f"{base}-USD"  # "-USD" suffix disables weekend rangebreaks
        else:
            from utils.data_fetch import get_stock_history
            df = get_stock_history(setup.ticker, period="2y", interval="1d")
            yf_symbol = setup.ticker

        if df.empty:
            raise RuntimeError(f"no market data for {setup.ticker}")

        chart_setup = ChartSetup(
            asset=setup.ticker,
            yf_symbol=yf_symbol,
            interval="1d",
            tf_label="1D",
            resample=None,
            direction=setup.direction,
            emas=[50, 200],
            show_rsi=True,
            show_macd=True,
            fib_zone=setup.fib_zone,
            supports=list(setup.supports),
            resistances=list(setup.resistances),
            entry_zone=(setup.entry_low, setup.entry_high),
            targets=[("TP1", setup.target1), ("TP2", setup.target2)],
            stop_loss=setup.stop,
        )

        out_name = f"{setup.ticker}_1D_{dt.date.today():%Y-%m-%d}.png"
        out_path = os.path.join(GRAPHS_DIR, out_name)
        render_chart(chart_setup, df, out_path)
        return out_path
    except Exception as exc:  # noqa: BLE001 — alert flow must survive any chart failure
        print(f"[chart] WARNING: chart generation failed ({exc}); "
              f"falling back to text-only alert.", file=sys.stderr)
        return None
