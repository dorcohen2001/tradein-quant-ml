"""Bridge from a model's trade setup to the `core.charting` renderer.

Builds the renderer's TradeSetup directly from a model's structured setup
object -- no round-trip through the formatted Telegram text and no regex
parsing. OHLCV comes from the same source the signal was computed on
(Binance for crypto, yfinance for stocks), so chart levels always match the
alert. Designed to never break the alert flow: any failure is logged and
reported as None so the caller can fall back to a text-only message.

Layering note: this module lives in `core`, so it must not import any
model's `config` or `utils.data_fetch` -- the two models have genuinely
different data layers, and `core` is not allowed to depend on either. The
market-data fetchers are therefore INJECTED by the caller (`fetch_crypto`,
`fetch_stock`), which is why the signature takes them explicitly rather than
importing them. It reads only duck-typed attributes off `setup`, so both
models' TradeSetup shapes work unchanged.

The old `sys.path.append(GRAPHS_DIR)` shim is gone: the renderer is now the
proper package `core.charting`, imported normally.

`core.charting.setup_parser`'s text parser remains in use only for the
manual `core/charting/generate_chart.py` CLI.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

_CRYPTO_QUOTES = ("USDT", "USDC", "BUSD", "PERP")


def generate_chart_for_setup(
    setup, *, fetch_crypto, fetch_stock, output_dir, spec=None, model_key: str | None = None,
) -> str | None:
    """Render a chart PNG for a model's trade setup.

    `fetch_crypto(symbol, interval=..., limit=...)` and
    `fetch_stock(ticker, period=..., interval=...)` are the calling model's
    own data-fetch functions (both models expose exactly these signatures).
    `output_dir` is created if missing.

    `spec` is a `core.charting.spec.ChartSpec` choosing the timeframe, history
    window and indicators; omitted, it is the daily chart this function always
    produced, so an un-migrated caller is unaffected.

    `model_key` prefixes the output filename. Charts share one directory, and
    the name used to be `{TICKER}_{TF}_{DATE}.png` -- so two models alerting on
    the same ticker on the same day wrote the SAME path. That was harmless
    while models ran sequentially, but under the supervisor they run
    concurrently and one process could overwrite the file another was still
    uploading.

    Returns the absolute path of the saved image, or None if anything failed
    (missing data, unknown ticker, render error, ...).
    """
    try:
        from core.charting.chart_renderer import render_chart
        from core.charting.setup_parser import TradeSetup as ChartSetup
        from core.charting.spec import ChartSpec

        spec = spec or ChartSpec.daily()
        for issue in spec.warnings():
            print(f"[chart] WARNING: {issue}", file=sys.stderr)

        if setup.asset_class == "crypto":
            df = fetch_crypto(setup.ticker, interval=spec.interval, limit=spec.crypto_limit)
            base = setup.ticker
            for q in _CRYPTO_QUOTES:
                if base.endswith(q):
                    base = base[: -len(q)]
                    break
            yf_symbol = f"{base}-USD"  # "-USD" suffix disables weekend rangebreaks
        else:
            df = fetch_stock(setup.ticker, period=spec.stock_period, interval=spec.interval)
            yf_symbol = setup.ticker

        if df.empty:
            raise RuntimeError(
                f"no market data for {setup.ticker} at {spec.interval} "
                f"(stock_period={spec.stock_period}, crypto_limit={spec.crypto_limit})"
            )

        chart_setup = ChartSetup(
            asset=setup.ticker,
            yf_symbol=yf_symbol,
            interval=spec.interval,
            tf_label=spec.tf_label,
            resample=None,
            direction=setup.direction,
            emas=list(spec.emas),
            show_rsi=spec.show_rsi,
            show_macd=spec.show_macd,
            fib_zone=setup.fib_zone,
            supports=list(setup.supports),
            resistances=list(setup.resistances),
            entry_zone=(setup.entry_low, setup.entry_high),
            targets=[("TP1", setup.target1), ("TP2", setup.target2)],
            stop_loss=setup.stop,
        )

        os.makedirs(output_dir, exist_ok=True)
        prefix = f"{model_key}_" if model_key else ""
        out_name = f"{prefix}{setup.ticker}_{spec.tf_label}_{dt.date.today():%Y-%m-%d}.png"
        out_path = os.path.join(output_dir, out_name)
        render_chart(chart_setup, df, out_path)
        return out_path
    except Exception as exc:  # noqa: BLE001 — alert flow must survive any chart failure
        print(f"[chart] WARNING: chart generation failed ({exc}); "
              f"falling back to text-only alert.", file=sys.stderr)
        return None
