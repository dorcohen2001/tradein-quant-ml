# TRADEIN

A **paper-trading** signal system. No module here places real brokerage or
exchange orders. "Live" means: scan → compute a setup → send a Telegram alert
→ log a simulated position. Nothing in this repository moves money.

Two independent models run **concurrently in production** against the same
watchlists. They are different strategies and are expected to disagree.

| | Livne | Biton |
|---|---|---|
| Pipeline | Multi-timeframe (MTFA): weekly/monthly HTF structure + 12H/8H/4H/1H ITF trigger | Single-timeframe daily (MA confluence + Fib golden zone) |
| Lifecycle | PENDING → ACTIVE → PARTIAL_TP1 → WIN_TP2 / LOSS_SL_BE / EMERGENCY_EXIT | PENDING → ACTIVE → WIN_TP1 / WIN_TP2 / LOSS_SL |
| Min R:R | 1:3 | 1:2 |
| Code | `models/livne/` | `models/biton/` |

## Layout

```
core/         shared, model-agnostic: yfinance disk cache, Telegram transport,
              market-session gating, news/sentiment, chart rendering, and the
              intraday backtesting engine (core/backtesting: candle fetch +
              cache, point-in-time resampling, intrabar fills, metrics).
              Contains NO strategy logic and NO trading thresholds.
models/       one directory per model; each is its own sys.path root and runs
              as its own OS process. They never import each other.
runtime/      ALL mutable state: trade stores, journals, funnel exports,
              caches, rendered charts, logs. Nothing here is source.
data/         backtest candle cache (git-ignored, re-downloadable). Shared by
              both models so a symbol is only ever pulled from Binance/Yahoo
              once.
agents/steve/ daily analyzer -> Markdown report -> email (covers BOTH models)
mail/         Gmail SMTP sender + archived reports
docs/         design notes
```

Dependency direction is one-way and enforced by convention:

```
models/livne ─┐
              ├─► core        (core never imports models)
models/biton ─┘
```

The only coupling between the two models is that Livne's runner launches
Biton's as a **subprocess** each cycle, and both write into `runtime/` under
distinct, per-model filenames.

## Running

Start the live loop (this also drives Biton every cycle):

```bash
cd models/livne && python live_runner.py --asset-class both
```

Dry-run scan, no alerts and no paper trades logged:

```bash
cd models/livne && python main.py --asset-class both
```

Daily report for both models:

```bash
python agents/steve/daily_tradein_analyzer.py --no-llm
```

Backtest a model. Both suites simulate on **intraday** candles: signals are
generated bar by bar on the analysis timeframe, and stops/targets are checked
against each bar's high and low, not its close.

```bash
cd models/livne && python -m backtest_suite.run_backtest --start-date 2026-07-01 --end-date 2026-08-01
```

```bash
cd models/biton && python -m backtest_suite.run_backtest --start-date 2026-07-01 --end-date 2026-08-01 --interval 1h
```

Livne defaults to its live 15m trigger timeframe; Biton defaults to 1h
(`--interval 1d` reproduces its previous daily behaviour). Useful flags on
both: `--exec-interval 5m` manages open positions on finer bars for precise
exit timestamps, `--tickers BTCUSDT,ETHUSDT` overrides the watchlist,
`--offline` replays purely from the cached candles in `data/`, and `--verbose`
prints the full trade-by-trade log. Each run also writes that log to
`runtime/backtest/trades_log_<model>.csv`, which the Steve analyzer reads.

Candles are cached in `data/` on first download, so the second run over the
same window is roughly twice as fast and hits no API at all. Note that free
intraday stock history is capped by yfinance (~60 days at 15m/5m, ~730 days at
1h); crypto has no such limit.

Chart-alert smoke test (renders only; add `--send` to actually post):

```bash
python core/charting/test_chart_alert.py --model livne
```

## Setup

```bash
pip install -r requirements.txt
```

Then create a repo-root `.env` (shared by both models):

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
NEWSAPI_KEY=          # optional; falls back to Google News RSS
```

`mail/.env` separately holds `SENDER_EMAIL`, `SENDER_APP_PASSWORD`,
`RECIPIENT_EMAIL` for the report emailer.

## How `core` is importable

Each model directory is its own sys.path root (that is what keeps the two
models' `config`/`utils`/`generator` namespaces separate). The repository root
is therefore not on `sys.path` by default, so every module that imports
`core.*` first does:

```python
import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*
from core.utils.telegram_notifier import send_telegram_message
```

No `pip install -e .`, no `.pth` file, and no PYTHONPATH environment variable
that a scheduled task could silently lose.
