# Session Summary & System Architecture Status

**Project root:** `C:\Users\Dor\Desktop\TRADEIN\TRADEIN` (a sibling `backuptrade1/` folder one level up, at `C:\Users\Dor\Desktop\TRADEIN\backuptrade1`, holds a full pre-session snapshot of this codebase — restore point if anything here needs to be rolled back).

**Paradigm:** This is a **paper-trading** system end to end. No module in this codebase places real brokerage/exchange orders. "Live trading" means: scan → compute a setup → send a Telegram alert → log a simulated position to `paper_trading/trades.json` + an Excel journal. Confirm this explicitly with the user before ever treating any output as a real trade instruction.

---

## 1. Core Objectives & Problem Statement

The session started from a working MTFA (Multi-Timeframe Analysis) trading-signal pipeline that had one critical symptom: **`python -m backtest_suite.run_backtest` reliably produced 0 signals**, across both stocks and crypto, over every window tested. The work this session was an iterative diagnose → fix → re-measure loop:

1. Build a **funnel diagnostic** (a duplicate, instrumented orchestration of the real signal-decision pipeline) to see exactly how many candidates survive each gate.
2. Use that funnel to find the real bottlenecks (not guesses) — first the Fibonacci self-referential bug, then Stage 3 (HTF zone match), then Stage 7 (reversal-candle confirmation).
3. Fix/relax gates in a principled way (never silently deleting risk controls — SL/TP validity and minimum R:R were preserved as **hard, non-negotiable gates** throughout).
4. Re-run the funnel + a full backtest after every change to measure the actual effect, not assume one.
5. Once frequency was fixed for the backtest, build the same visibility into the **live** scanner (`screener/funnel_diagnostics.py` + Excel export), so future tuning doesn't require ad hoc diagnostic scripts again.

Net result: backtest went from **0 signals in every configuration** to **10 signals** in a representative 3-month window (2026-05-01 → 2026-08-01, stocks + crypto), with full visibility into why any given ticker passes or fails, live or backtest.

---

## 2. Dynamic Timeframe & Pipeline Architecture

### Multi-Timeframe Zoom-In (live mode)

```
Stage A (HTF) — ta.mtfa.htf_pass()
  Weekly bars: structure (support/resistance, Order Blocks), RSI divergence,
  and a zone match (price within HTF_ZONE_PROXIMITY_PCT of a level) +
  mandatory weekly volume confluence.
  Monthly bars: bias/context ONLY — no longer a hard veto (see §3).
        │
        ▼
Stage B (ITF) — ta.mtfa.analyze_mtfa()
  12H/8H/4H: majority (2-of-3) PRIMARY_MA_PERIOD (200) trend-alignment vote.
  4H + 1H:   ta.mtfa.detect_braking_event() — the "zoom-in" — scans the last
             BRAKING_WINDOW_BARS (5) bars on EACH of these two timeframes for
             a braking/reversal event (see §3 for the 4 sub-conditions).
  4H:        ITF volume spike check + Fibonacci golden-zone containment OR
             reaction check.
        │
        ▼
Correlation gate — macro.macro_analyzer (SPY/QQQ/VIX for stocks, USDT.D
  trend for crypto) restricts the direction MTFA found.
        │
        ▼
Risk validation — risk.risk_manager: structural SL (ATR-capped), TP1/TP2
  (must be strictly positive), minimum R:R (TP1 ≥ 1:2, TP2 ≥ 1:3).
```

### Backtest Proxy vs. Live Execution — the key architectural distinction

**The backtest has no historical intraday (4H/1H) data pipeline** — `backtest_suite/historical_data.py` only fetches **daily** OHLCV (`fetch_stock_history`/`fetch_crypto_klines`, both `interval="1d"`). Building a true historical 4H/1H fetcher (feasible for crypto via Binance's full historical klines API, effectively infeasible for stocks beyond yfinance's ~730-day 60m cap) was explicitly scoped OUT this session as a much larger, separate project.

So `backtest_suite/run_backtest.py`'s `ta_mtfa_backtest()` is a **parallel, hand-written orchestration** that mirrors `ta.mtfa.analyze_mtfa()`'s logic but runs entirely on **daily bars**:
- HTF Stage A: identical — resamples the daily-bar history to weekly/monthly and calls the **same** `ta.mtfa.htf_pass()` function (fully shared, zero duplication there).
- ITF Stage B: approximated as a **single daily-bar "trigger" pass** — `detect_braking_event()` is still called (the exact same shared function Stage 7 uses live), just fed the daily bars instead of a 4H/1H frame. So the *detector itself* is identical between live and backtest; only the *timeframe granularity* it's fed differs.

This boundary is documented in both `backtest_suite/simulator.py`'s and `run_backtest.py`'s module docstrings — **do not be surprised that backtest and live results diverge**; the backtest is a same-detector/coarser-timeframe **proxy**, not a bit-for-bit replay of the live path.

---

## 3. Parameter Refactoring & Funnel Optimization

All values below are **current, as of the end of this session**, read directly from `config.py`.

### Stage 3 — HTF Zone & Volume Confluence (`ta/mtfa.py::htf_pass`, `_direction_zone`, `directional_volume_confluence`)

| Change | Before | After |
|---|---|---|
| `HTF_ZONE_PROXIMITY_PCT` | 2% | **4%** |
| `HTF_VOLUME_CONFLUENCE_MULT` (weekly volume vs its 20-week avg) | 1.3x → 1.10x → | **1.05x** |
| Weekly trend alignment / RSI divergence | Hard veto (`if not (divergence_support or trend_support): return None`) | **Removed.** Zone match is now the *only* hard requirement; trend/divergence feed `screener._score_candidate`'s `ta_confluence` component instead (30% zone + 25% divergence + 25% MA alignment + 20% monthly bias) |
| Monthly HTF trend | Hard veto (nulled `long_signal`/`short_signal` on conflict) | **Removed**, same reasoning — monthly is context/bias (`MTFASetup.monthly_trend`), never a reject |

### Stage 7 — Braking / Reversal Event (`ta/mtfa.py::detect_braking_event`, `ta/candles.py`)

Replaced the old single-bar, same-day `classify_reversal_candle()`-only check with `detect_braking_event(df, direction, window_bars=BRAKING_WINDOW_BARS)`, which scans the **last 5 bars** (most recent first) for **ANY** of four independent signatures:

1. **Pin-bar rejection** (`ta.candles.is_pin_bar_rejection`) — wick in the trade's favor ≥ `BRAKING_PIN_BAR_WICK_RATIO` (**50%**) of the candle's total range. Looser than the pre-existing `is_hammer`/`is_shooting_star` (no body-size or opposite-wick constraint).
2. **Classic patterns** (`ta.candles.classify_reversal_candle`, unchanged/reused) — engulfing, hammer, shooting star, doji. Doji now counts **unconditionally** (previously gated on RSI divergence support).
3. **Volume climax / absorption** (`ta.candles.is_volume_climax`) — volume ≥ `BRAKING_VOLUME_CLIMAX_MULT` (**1.5x**) its 20-bar average AND body ≤ `BRAKING_SMALL_BODY_RATIO` (**30%**) of range. Direction-agnostic (exhaustion is exhaustion regardless of candle color).
4. **Market Structure Shift / MSS** (`ta.candles.is_structure_shift`) — current bar's High breaks the prior `BRAKING_STRUCTURE_LOOKBACK` (**10**) bars' High (LONG) / Low breaks the prior 10 bars' Low (SHORT).

Applied **live** on both the 4H and 1H ITF frames (`ta/mtfa.py::_fetch_itf` now also fetches a genuine 1H frame for both crypto and stocks — previously ITF only carried 12H/8H/4H). Applied in **backtest** on the daily-bar window (see §2 boundary above). Also **reused** in `backtest_suite/simulator.py::_trend_reversal_signal` for the dynamic exit (called with the *opposite* of the held direction — a bearish event while LONG is what closes it), alongside the pre-existing MACD histogram cross check.

`ITF_VOLUME_SPIKE_MULT` (the separate ITF trigger-candle volume check, distinct from the climax sub-condition above) was also lowered: 1.5x → **1.10x** ("adaptive volume scaling" for a Daily/Weekly swing execution timeframe).

### Scoring & Risk Bounds

- **No minimum confluence-score gate was ever removed, because none existed.** Verified by exhaustive grep early in the tuning work — `Candidate.score` (0–100) is computed purely for ranking/sorting (`TOP_N_ASSETS`, `candidate_pool`), never used as a pass/fail filter anywhere in the codebase.
- **Risk guards were explicitly preserved as hard gates** throughout every relaxation round:
  - `compute_stop` / `compute_targets` (`risk/risk_manager.py`): stop must land on the correct side of entry; **both TP1 and TP2 must be strictly positive** (`if target1 <= 0 or target2 <= 0: return {"target1": None, "target2": None}`).
  - **ATR distance cap** (`STOP_MAX_ATR_MULT = 5.0`): bounds any structural stop candidate to at most 5×ATR from entry — this was a **real bug fix** this session (see §4), not a new restriction; it prevents a stale/extreme structural level (e.g. a 52-week high 2–3× above current price) from blowing the risk distance out and dragging TP1/TP2 negative for SHORT setups.
  - `validate_min_rr`: TP1 R:R ≥ `TP1_MIN_RR` (**2.0**), TP2 R:R ≥ `MIN_RISK_REWARD` (**3.0**). These floors were *raised* this session (from 1.5/2.0 originally), not loosened.

### Liquidity filters (established earlier, unchanged this round, included for completeness)

`MIN_STOCK_DOLLAR_VOLUME_SMA20 = $7.5M`, `MIN_CRYPTO_DOLLAR_VOLUME_SMA20 = $35M` (both 20-day SMA of `Close × Volume`, not a single-bar reading). No static minimum stock price. `MIN_STOCK_MARKET_CAP = $1B`. `PRIMARY_MA_PERIOD = 200` / `SECONDARY_MA_PERIOD = 150` (EMA20/EMA50 fully deprecated and removed from `ta/ta_engine.py`).

---

## 4. Backtest & Funnel Diagnostic Results

Funnel methodology: a standalone diagnostic (`screener/funnel_diagnostics.py` live; an equivalent throwaway script was used for backtest-window measurement) re-runs each ticker through the same gates with a counter at every stage, so survivor counts are exact, not estimated.

### Stage-by-stage survival, full watchlist (109 stocks + 34 crypto), 2026-05-01 → 2026-08-01

| Stage | Very first measurement | After Stage 3 widen (4%/no-veto/1.05x) | After Stage 7 `detect_braking_event` |
|---|---:|---:|---:|
| 0. Ticker-days evaluated | 10,029 | 10,029 | 10,009 |
| 2. Liquidity (SMA20 $ vol) | 7,639 (76.2%) | 7,639 (76.2%) | 7,619 (76.1%) |
| **3. HTF zone + volume confluence** | 114 (1.49%) | **259 (3.39%)** | 255 (3.35%) |
| 6. 200MA alignment | 26 (37.7%) | 48 (34.3%) | 46 (33.6%) |
| **7. Braking/reversal event** | 3 (11.5%) | 5 (10.4%) | **43 (93.5%)** |
| 8. ITF volume spike | 2 (66.7%) | 2 (40.0%) | 27 (62.8%) |
| 9. Fib contained-or-reacted | 2 (100%) | 2 (100%) | 17 (63.0%) |
| **12. Min R:R → FULL SIGNAL** | **0** | **0** | **10** |

Two real bugs were found and fixed *during* this funnel work (not new restrictions — correctness fixes):
- **Self-referential Fib bug**: the swing used for the golden-zone Fib check included the current (reversal) bar, making "price inside the 0.5–0.786 retracement" nearly impossible exactly when a reversal candle fires (a fresh extreme *is* the 0.0/1.0 point). Fixed via `ta/mtfa.py::swing_excluding_recent` (excludes the last 3 bars from the swing calculation).
- **SHORT stop asymmetry**: `compute_stop`'s SHORT branch unconditionally included `swing_high` as a candidate (LONG's `swing_low` was fallback-only) — `max()` picked the stale swing high almost every time, blowing out the risk distance and driving TP1/TP2 negative. Reproduced live on TAOUSDT (swing high $539 vs. price $194). Fixed by making both branches symmetric (fallback-only) plus the ATR distance cap above.

### Full `run_backtest.py` report, same window, after all fixes (10 signals, 8 resolved / 2 still OPEN)

```
Total Signals Generated : 10  (8 resolved, 2 still OPEN)
Win Rate (resolved)     : 25.0%  (2 profitable / 6 unprofitable)
Profit Factor           : 0.42
Strategy Return (sum)   : -14.49%  (resolved trades only, equal-weighted, non-compounded)

Exit Reason Breakdown:
  SL                5 trades   0/5 profitable   avg PnL  -4.34%   avg hold 6d
  TREND_REVERSAL    3 trades   2/3 profitable   avg PnL  +2.40%   avg hold 8d
```

**Honest read, stated to the user and worth repeating to a fresh session:** frequency is fixed, but this window's *quality* is currently poor (25% win rate, profit factor < 1). This is one 3-month, mostly-stocks sample (crypto still produced 0 signals in this window) — not statistically conclusive, but a real signal that the Stage 7 relaxation traded selectivity for frequency. **Next natural step, not yet done:** either test over a longer window for a real sample size, or tighten one specific piece of `detect_braking_event` (e.g. require the volume-climax/MSS sub-conditions to align with broader trend context rather than firing in isolation).

Also fixed this session (unrelated to frequency, found via code review, not the funnel): `TradeOutcome.stop` was silently being overwritten by the breakeven price once a trade passed TP1, mislabeling the CSV export — fixed by adding a separate `TradeOutcome.initial_stop` field and splitting the CSV into `initial_stop_loss`/`stop_loss_at_exit` columns. And a same-bar stop/TP1 ambiguity bug (`if hit_stop and not hit_tp1` let an ambiguous bar get recorded as a TP1 win instead of conservatively favoring the stop) in both `paper_trading/tracker.py` and `backtest_suite/simulator.py`.

**Backtest trade duration**: the old fixed `max_days=10` force-close is **completely removed**. Trades now run bar-by-bar until SL, a target, or a `detect_braking_event`-driven dynamic exit fires — however long that takes. A trade still unresolved when the historical data window runs out is reported `OPEN` / `END_OF_DATA` (an honest "don't know yet," excluded from win-rate/profit-factor math), never force-resolved into a fabricated win or loss.

---

## 5. Live Trading & Funnel Diagnostics Logging

### `screener/funnel_diagnostics.py` (new this session)

A **read-only tracer**, fully independent of the real trading-decision path (`strategy.strategy_engine.evaluate_setup` / `ta.mtfa.analyze_mtfa` are never called or modified by it). It re-implements the same short-circuiting gate sequence by calling the **same shared, already-tested primitives** (`htf_pass`, `detect_braking_event`, `volume_spike`, `fib_reaction`, `compute_stop`/`compute_targets`/`validate_min_rr`) in its own orchestration — the same pattern `run_backtest.py`'s `ta_mtfa_backtest` already used for the backtest proxy, now productionized for live use.

Gate sequence traced per ticker (mirrors the real evaluation order): Liquidity → Stage A (HTF zone + weekly volume confluence) → Stage B braking event → Stage B ITF volume spike → Stage B 200MA alignment → Stage B Fib confluence → Macro correlation regime → Risk validation. Each ticker's record carries a `PASS`/`FAIL`/`NOT_REACHED` status per stage, plus `final_decision` (`QUALIFIED`/`REJECTED`) and an exact `rejection_reason` string naming the first gate that actually failed.

### Excel export (`funnel_logs/live_funnel_YYYYMMDD_HHMMSS.xlsx`)

Two sheets, built with `openpyxl` (matching the existing `paper_trading/journal.py` style):
- **`Funnel Summary`** — one block per asset class: stage name, tickers that *entered* the stage, tickers that *passed*, pass %, drop count, plus a final `FINAL: QUALIFIED` row.
- **`Ticker Diagnostic Log`** — one row per scanned ticker: ticker, asset class, price, direction (once known), dollar volume, RR1/RR2, a PASS/FAIL/NOT_REACHED column per gate, final decision, and the rejection reason. Qualified rows are highlighted green.

`funnel_logs/` is created automatically (`os.makedirs(..., exist_ok=True)`) and is gitignored (like `paper_trading/trades.json` and `macro/market_snapshots.json` — all runtime-generated, not committed).

### Non-blocking execution

Wired into `live_runner.py::run()` (called once per asset class after that class's real scan, using the already-fetched `MacroContext` — no duplicate macro fetch). Both the trace step and the export step are wrapped in their own `try/except`, printing `"(scan/alerts unaffected)"` on any failure — a network hiccup mid-trace or a previous report still open/locked in Excel (`PermissionError`, caught explicitly) degrades to a log line, never an interrupted scan/alert cycle. `run(..., export_funnel: bool = True)` defaults on; pass `False` to disable.

**Verified live** via the exact command `python live_runner.py --asset-class both --once`: `funnel_logs/` was created, the Excel file was written with both sheets correctly structured, and its survivor counts matched the real scanner's own `"N candidates passed"` output exactly (109 stocks → 94 passed liquidity → 0 passed Stage A; 34 crypto → 5 passed liquidity → 0 passed Stage A, in that particular live snapshot).

---

## 6. How to Run & Current Operations Guide

### Backtest (historical replay, no side effects on live state)

```bash
python -m backtest_suite.run_backtest --start-date 2026-05-01 --end-date 2026-08-01 --asset-class both --verbose
```
- `--asset-class stocks|crypto|both`, `--max-tickers N` (limit watchlist size for faster smoke runs), `--disable-dynamic-exit` (A/B-test with only SL/TP, no `detect_braking_event`-driven early exit).
- **Writes to `backtest_suite/trades_log.csv`** (overwrites it with this run's results) and prints the full report to stdout. This file is git-tracked — be deliberate about when to actually run this for real vs. use a scratch `path=` with `export_trades_csv()` for exploratory runs, since it will clobber prior recorded history.

### Live scanner — single cycle (safe to run repeatedly for testing)

```bash
python live_runner.py --asset-class both --once
```
Runs exactly one scan+refresh cycle then exits. Sends real Telegram alerts and logs real paper trades **if** anything qualifies that cycle. Also always exports a funnel diagnostic Excel report to `funnel_logs/` (default `export_funnel=True`).

### Live scanner — continuous (the actual "run the bot" command)

```bash
python live_runner.py --asset-class both
```
Loops forever, one full cycle every `CYCLE_INTERVAL_MINUTES` (currently **15**), until Ctrl+C. Each cycle: (1) refreshes existing open paper positions against live prices first (stop/TP1-partial-breakeven/TP2/emergency-exit checks, Telegram status updates), (2) scans for new setups (skips stocks entirely when the US market is closed; crypto scans 24/7), (3) exports the funnel diagnostic Excel report.

### Paper trading — what actually happens, no ambiguity

- **No real money, no real brokerage/exchange orders, ever.** This is 100% simulated.
- New qualifying setups → logged to `paper_trading/trades.json` (gitignored) + `paper_trading/paper_trades_journal.xlsx`, and a Telegram alert is sent (chart + formatted setup) if `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are configured in `.env`.
- Position lifecycle: `PENDING` (entry zone not yet touched, expires after 5 days if never filled) → `ACTIVE` → `PARTIAL_TP1` (50% closed, stop moved to breakeven) → `WIN_TP2` / `LOSS_SL_BE` / `EMERGENCY_EXIT`, or straight `ACTIVE` → `LOSS_SL` if the stop hits before TP1.
- `main.py` also exists as a `dry_run=True` CLI demo entry point (no Telegram, no trade logging) — useful for quick manual sanity checks without side effects.

### Known environment quirks worth knowing immediately

- Windows console (`cp1252`) will throw `UnicodeEncodeError` on emoji if you `print()` a Telegram-formatted message directly in a raw Python shell — `live_runner.py` itself already handles this (`sys.stdout.reconfigure(encoding="utf-8", ...)` at import time); it only bites ad hoc debugging scripts.
- `backtest_suite/trades_log.csv` can end up OS-locked (`PermissionError`) if it's open in Excel — check with a quick write-probe before assuming a backtest run failed to export.
- yfinance prints `"No earnings dates found, symbol may be delisted"` for ETF tickers (SPY/QQQ/ARKK/IWM/DIA/XLF/XLE/SMH) in the watchlist — harmless noise (ETFs don't have earnings), not an error, and now appears twice per cycle (once from the real scan's earnings-blackout check, once from the funnel diagnostic's independent cheap-filter check).

### Architecture reference

A living, previously-published architecture-spec Claude Artifact covers the full pipeline in diagram/formula form (indicator formulas, Stage A/B decision trees, risk formulas with a worked example, position-management state diagrams). It was kept up to date across this session's changes — ask the user for the link if continuing architecture-documentation work, or rebuild it from this summary plus a fresh source read if the link isn't available.
