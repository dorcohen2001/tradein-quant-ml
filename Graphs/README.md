# Trade Setup Chart Generator

Parses structured market-analysis text (daily trade setups) and renders a
dark-mode candlestick chart PNG with every indicator and trade level the text
mentions.

## Usage

```bash
pip install -r requirements.txt

# from a file
python generate_chart.py example_setup.txt

# inline
python generate_chart.py --text "DAILY TRADE SETUP — LTCUSDT (SHORT) ..."

# via stdin
type setup.txt | python generate_chart.py
```

Output is saved next to the scripts as `{ASSET}_{TIMEFRAME}_{DATE}.png`
(e.g. `LTCUSDT_1D_2026-07-30.png`).

## What gets detected

| Element | Trigger in the text | Rendering |
|---|---|---|
| Asset | `LTCUSDT`, `BTCUSD`, ... or `Ticker: AAPL` | fetched via yfinance (`LTC-USD`) |
| Timeframe | `DAILY`, `WEEKLY`, `4H`, `1H`, `15M` | candle interval (4H/2H resampled from 1h) |
| Chart type | candlestick by default; `line chart` switches | main price panel |
| EMA / SMA | `EMA 50`, `EMA 200`, `SMA 20` | overlay lines |
| RSI | any `RSI` mention | subplot with 30/70 bands |
| MACD | any `MACD` mention | subplot with histogram + lines |
| Fib zone | `Golden Zone 44.43 - 46.02` | gold shaded band |
| Support / Resistance | `support at 40.10 and 37.80` | dotted horizontal lines |
| Entry zone | `Entry Zone: 44.40 - 45.60` | blue shaded band |
| Targets | `TP1: 42.92`, `Take Profit`, `Target` | green dashed lines |
| Stop loss | `SL: 46.59`, `Stop Loss` | red dashed line |
| Volume | always | histogram at the bottom of the price panel |

## Files

- `generate_chart.py` — CLI entry point
- `setup_parser.py` — text → `TradeSetup` (regex/keyword driven)
- `data_fetcher.py` — OHLCV download via yfinance (+ resampling)
- `chart_renderer.py` — Plotly figure construction + Kaleido PNG export
- `example_setup.txt` — sample LTCUSDT short setup used for testing
