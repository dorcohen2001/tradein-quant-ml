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
agents/steve/ daily analyzer -> Markdown report -> email (covers ALL models)
mail/         Gmail SMTP sender + archived reports
scripts/      operational CLIs: backup_manager.py, prune_runtime.py
tests/        pytest suite; imports only core/ + scripts/, never a model
backups/      timestamped archives (git-ignored; see Backups below)
docs/         design notes
run_supervisor.py   schedules every registered model independently
```

Dependency direction is one-way and enforced by convention:

```
models/livne ─┐
              ├─► core        (core never imports models)
models/biton ─┘
```

The models are fully independent: neither imports nor launches the other.
`run_supervisor.py` schedules every model registered in `core/registry.py`,
each in its own thread, at its own interval, with its own timeout and CLI
flags. They share only the `runtime/` tree, under distinct per-model filenames.

`core/registry.py` is the single source of truth for which models exist. It
holds one frozen `ModelConfig` per model — key, label, cadence, the three
status vocabularies, Telegram route — and **derives every runtime path from
the key**, so adding a model touches no other file. See "Adding a model" below.

## Running

Start everything (both models, each on its own schedule):

```bash
python run_supervisor.py
```

Useful variants — `--models livne` runs a subset, `--once` runs one cycle each
and exits, `--dry-run` prints the schedule and exact commands without
launching anything. Flags you *don't* pass are not forwarded, so each model
falls back to its own config rather than inheriting another's.

A single model can still be run directly, exactly as before:

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

### Sending models to different Telegram groups

Every model posts to the shared `TELEGRAM_CHAT_ID` above until you say
otherwise. To split one out, set a per-route variable — the route name is the
model key:

```
TELEGRAM_CHAT_ID_LIVNE=-1001234567890
TELEGRAM_BOT_TOKEN_LIVNE=...        # optional; inherits the shared bot if unset
```

Or configure several at once with a JSON blob, which takes precedence:

```
TELEGRAM_ROUTES={"livne":{"chat_id":"-100111"},"biton":{"chat_id":"-100222"}}
```

Test where a route actually lands:

```bash
python -m core.utils.telegram_notifier --route livne
```

## Backups

`runtime/` is gitignored, so a `git checkout` neither backs up nor restores
live trade state. `scripts/backup_manager.py` covers that gap:

```bash
python scripts/backup_manager.py --mode full
```

`--mode runtime` archives only the live stores and journals (cheap enough to
schedule); `--mode code` is source-only. Archives land in `backups/`, keep the
newest 10 per mode, and every write is verified by reading it back —
`--verify-snapshot <path>` re-checks CRCs *and* re-parses every archived trade
store, which is the failure a CRC alone cannot see. A `full` archive contains
`.env` so it can genuinely restore; pass `--exclude-secrets` for a copy safe to
move off the machine.

Derived artifacts are pruned by age (`scripts/prune_runtime.py`, also run daily
by the supervisor); `--dry-run` first is the recommended habit.

## Adding a model

Add one `ModelConfig` to `_MODELS` in `core/registry.py` and create
`models/<key>/`. Everything else follows from the key — trade store, report
state, journal, backtest CSV, funnel prefix, chart prefix, Telegram route,
supervisor scheduling, and Steve's report section. No other file needs editing.

```python
ModelConfig(
    key="ravid",
    label="Ravid model",
    description="Mean-reversion on 1h bars.",     # shown in Steve's brief
    cycle_interval_minutes=5,
    closed_statuses=("WIN_TP1", "LOSS_SL", "EXPIRED"),
    update_statuses=(),
    closed_with_pnl=("WIN_TP1", "LOSS_SL"),       # EXPIRED excluded: never entered
    telegram_route="ravid",
)
```

The model directory needs `_bootstrap.py` (copy either existing one verbatim),
a `config.py` deriving its paths from `get_model("<key>")`, and a
`live_runner.py` exposing `--once`. Registered-but-missing directories are
skipped with a warning rather than crashing the supervisor.

## Tests

```bash
pytest tests/
```

Tests import only from `core`, `scripts` and the repo root — never from a model
package, since the models define clashing top-level module names (`config`,
`utils`, `paper_trading`) and cannot both be imported into one process.

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
