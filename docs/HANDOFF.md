# TRADEIN — session handoff

**Written 2026-08-19.** Point a new Claude Code session at this file. It is the
context that is *not* recoverable from the code: what changed and why, what is
running, what is broken, and the mistakes worth not repeating.

Read `README.md` for how the system works. Read this for what state it is in.

---

## 1. What this system is

A **paper-trading signal platform**. Nothing here moves money: it scans,
computes a setup, sends a Telegram alert, and logs a simulated position.
Multi-tenant — each "model" is an independent strategy with its own store,
journal, Telegram route and OS process.

Branch `refactor/directory-architecture`, HEAD `f99b55b`. **407 tests pass**
(`pytest tests/`, ~55s, no network).

---

## 2. Current state (verify before trusting — this file ages)

### Models

| key | enabled | stage | cadence | notes |
|---|---|---|---|---|
| `livne` | **yes** | production | 2 min | The original MTFA intraday model. Do not casually modify. |
| `biton` | **no** (paused 2026-08-15) | production | 15 min | 13 non-terminal positions frozen. Pausing does not manage them. |
| `horizon` | **yes** | incubating | 2 min | New this session. Livne's MTFA, recalibrated. |

### Running right now

The **supervisor** has been up since 2026-08-17 22:48 (`python run_supervisor.py`),
spawning one `--once` child per model per interval. Both tenants healthy:
`failure_streak: 0`, cycle durations ~83s (horizon) and ~93s (livne) against a
2-minute interval and a 480s timeout.

### Live results since horizon was activated (~2 days)

| | signals | closed | win rate | avg | total | PF |
|---|---|---|---|---|---|---|
| livne | 43 | 40 | 72.5% | -0.01% | -0.23% | 0.97 |
| horizon | 22 | 21 | 66.7% | -0.05% | -0.98% | 0.73 |

Horizon's `STAGNATION_EXIT` fired **3 times** live. **This is not a verdict** —
21 closed trades is noise, and horizon has never had a baseline backtest. Do not
draw conclusions from it; do not tune on it.

### Telegram

Both models route to the **same chat, same bot** (the shared `TELEGRAM_CHAT_ID`
in the repo-root `.env`). Neither declares `dedicated_route`, and no per-route
env vars exist. Every alert is prefixed `[Model name]`, which is the only thing
distinguishing them in the group.

---

## 3. Architecture

```
core/
  runtime/tenant.py    THE execution loop. TenantRunner owns order + safety
                       (lock, cycle sequence, containment, CLI).
                       TenantStrategy is what a model implements.
  locking.py           FileLock + TenantLock — one instance per model
  paper_trading/
    refresh.py         the one "mark positions to market" implementation
    outbox.py          alerts that survive a failed send
    catch_up.py        bar-by-bar replay of missed price action
    store.py           atomic JSON store
  data/
    market.py          shared candle accessor (cross-tenant dedup)
    rate_governor.py   cross-process token bucket (BACKTEST PATH ONLY — see 5.2)
    health.py          provider failure counters
  backtesting/         feed, cache, intervals, providers
  testing/             conformance harness + FakeMarket  (see section 6)
  registry.py          SINGLE SOURCE OF TRUTH for which models exist
models/<key>/          an ADAPTER (~250 lines) + that model's strategy
run_supervisor.py      schedules every enabled model independently
```

**The cycle order is load-bearing:** catch-up → refresh → flush alerts →
summaries → scan. Each stage is wrapped independently so one failure cannot
cost another.

**A model is no longer a copy of the loop.** Before this session livne and biton
shared 390 of 432 runner lines. Now each is an adapter implementing
`TenantStrategy`. `apply_bar` stays per-model on purpose — livne/horizon bank a
partial at TP1, biton closes outright; a shared state machine would have to know
about both.

---

## 4. What changed this session (8 commits)

| commit | what |
|---|---|
| `08e59db` | Audit phases 1–3: tenant lock, elapsed-time refresh window, automated backups, `last_checked_at` backfill, alert outbox, dead-man escalation, hot-path caching, retention, provider logging |
| `ae53f39` | Backups survive a running fleet (they aborted on a locked tenant `.lock` file) |
| `1a46f02` | **Stock screener bug**: `unrecognised interval '1wk'` skipped *every* stock, every cycle |
| `c6c3c5b` | Phase 4a: shared `TenantRunner`; generator template + biton migrated |
| `98f762c` | Phase 4b: livne migrated |
| `4d76bb8` | IPO/short-history fix + fleet-wide conformance harness |
| `b07317a` | Horizon onboarded (paused) |
| `f99b55b` | Horizon activated |

Tags: `backup-pre-tenantrunner-20260817_172820` (rollback point before the big
refactor), `phase4-tenantrunner-complete-20260817_181139`.

### The audit that started it

Full report (31 findings in 4 tiers):
https://claude.ai/code/artifact/1f867048-ec63-4a89-9bd3-635c2320ec14

Not all findings were implemented — see section 5.

### Bugs found and fixed along the way

- **`1wk`** — `core.data.market.interval_minutes` kept its own interval parser
  and rejected yfinance's weekly token. `ta.mtfa` requests the weekly frame
  first, so **every stock symbol was skipped, every cycle**, inside a
  catch-block. Caught red-handed in the user's own funnel export.
- **`period_to_bars`** — floored `1440 // 10080` to 0 and treated a weekly bar
  as daily: `period="2y"` asked for **520** weekly bars (ten years) instead of
  105. Not harmless — the zone map is built from that frame's swing extremes.
- **IPO tickers** — a 3× window pad asked for six years of history, predating
  every post-2020 listing; the impossible head gap was then re-requested **every
  cycle forever**, logged as "possibly delisted".
- **Backups aborted against a running fleet** — the archiver tried to read a
  tenant `.lock` file, which Windows makes unreadable while held. The automated
  6-hourly snapshot would have failed in exactly the situation it exists for.

---

## 5. Open items, most important first

### 5.1 ACTIVE BUG — shared `market_snapshots.json` (found 2026-08-19)

`MACRO_SNAPSHOT_STORE` is a **path literal**, not derived from the model key:

```
models/livne/config.py:373    RUNTIME_DIR/stores/market_snapshots.json
models/horizon/config.py:442  RUNTIME_DIR/stores/market_snapshots.json   <-- same file
```

Both tenants run `get_macro_context('crypto')` on the same 2-minute cadence and
both write it. On Windows `os.replace` fails with `WinError 5` when the target
is concurrently held, so one loses the race and **the entire crypto scan for
that cycle is lost**:

```
[screener] crypto scan failed (PermissionError: [WinError 5] Access is denied:
  ...tmp41bbofwv.tmp -> ...market_snapshots.json); other asset classes unaffected.
```

Observed: **23 of 1384** horizon cycles and **12 of 1384** livne cycles — ~1.7%
signal loss, permanent, and worse with each new tenant.

This is exactly the collision the registry's per-key path derivation exists to
prevent; this one file was never migrated. It was invisible until a *second*
MTFA tenant existed.

**Fix:** derive it per key like every other runtime artifact
(`market_snapshots_{MODEL_KEY}.json`), or put it behind a shared lock. The
per-key file is simpler and matches every other artifact. Losing horizon's
existing snapshot history is harmless — it is a rolling 30-day correlation cache.

### 5.2 Audit items never implemented

- **I2** — the cross-process rate governor covers the **backtest path only**.
  Live crypto and intraday-stock fetches bypass it entirely. Two tenants now
  fire simultaneously with 8 workers each against one uncoordinated IP budget.
- **I5** — the registry is read once at supervisor start; onboarding requires a
  fleet-wide restart.
- **I8** — the `data/` candle cache grows without bound (merge refreshes mtime,
  so age-pruning can never reclaim an active symbol) and merges unlocked.
- **I9** — Steve reports backtest CSVs with no freshness or provenance check.
- **R1** — the data-layer migration was parity-gated at `1d` **only**. 15m/5m/1h
  still run the legacy path and were never verified. The `1wk` bug came from
  exactly this gap; the same class of bug may be waiting at intraday intervals.
- **R4** — `ta/mtfa.py` (716 lines), the trade generator and the risk manager
  have **no tests**. Conformance checks the lifecycle, not the signal.
- Trackers still share ~150 lines of store boilerplate.

### 5.3 Horizon-specific

- **No baseline backtest was ever run.** It was activated on operator decision.
  `docs/HORIZON_CALIBRATION.md` records what is unestablished.
- The **session filter and emergency exit are not simulated** by the backtest,
  so equity backtest results are optimistic relative to live.
- The **1.45× volume gate** has never been measured against trade count.
  Horizon produced 22 signals in 2 days vs livne's 43 — roughly half, as
  designed, but unvalidated.

---

## 6. How to verify a core change WITHOUT touching models

This was a whole workstream, driven by the operator's objection to per-model
inspection. **Do not `cd` into model directories and run ad-hoc commands** — it
does not scale past a few tenants, and it is how the `1wk` bug survived.

```bash
pytest tests/                        # 407 tests, no network, ~55s
pytest tests/test_model_contract.py  # every registered model, as a black box
```

`tests/test_model_contract.py` contains **no model names**. It enumerates the
registry and runs `core/testing/conformance.py` in a subprocess per model,
checking platform invariants: status vocabulary, terminality, bar idempotence,
chronology, P&L sign, path namespacing, route ownership, blackout survival,
supervisor CLI. Adding the hundredth tenant adds zero test code.

The battery is proven able to FAIL — four tests build deliberately broken
tenants. `CONTRACT_VERSION` lets a fleet migrate in waves.

Data-layer changes use **scripted fakes**, never live tickers or cassettes:
`core/testing/fake_market.py`, with `tests/test_short_history.py` as the worked
example (reproduces an IPO listing date in milliseconds).

Full rationale: `docs/TESTING_STRATEGY.md`. The rule it states:
**a core change must never require an edit under `models/`.**

---

## 7. Operating commands

```bash
python run_supervisor.py                   # run every enabled model
python run_supervisor.py --dry-run         # show the schedule, launch nothing
python run_supervisor.py --models livne    # subset

python scripts/backup_manager.py --mode full     # verified snapshot
python scripts/backup_manager.py --list
python scripts/prune_runtime.py --dry-run

python agents/steve/daily_tradein_analyzer.py --scheduled  # unattended report
python -m core.utils.telegram_notifier --route horizon     # where does it land
```

```bash
cd models/horizon && python -m backtest_suite.baseline --start-date 2026-05-01 --end-date 2026-08-01 --exec-interval 5m
```

**Never run the supervisor and a standalone `live_runner.py` for the same
model.** The second refuses to start (tenant lock) and exits 1; three
consecutive failures trigger a Telegram dead-man alert.

Runtime snapshots run automatically every 6h (keeping 30). 17 exist.

---

## 8. Gotchas — things that will bite a new session

1. **`runtime/` is gitignored.** A `git checkout` restores code and **not one
   trade**. Roll state back with a `--mode runtime` archive.
2. **Models cannot share an interpreter.** They are separate `sys.path` roots
   defining clashing `config`/`utils`/`strategy`. Any cross-model tooling must
   use subprocesses. A constraint, not a preference.
3. **Verify what is running before touching anything.** Twice this session a
   store changed mid-work because a runner was live. Check running processes and
   the heartbeats in `runtime/state/`.
4. **The interval vocabulary has one authority**
   (`core/backtesting/intervals.py`). Binance says `1w`, yfinance says `1wk`.
   Anything that parses an interval string itself will eventually disagree with
   a venue — that was the `1wk` bug.
5. **Same-bar ambiguity resolves to the STOP**, consistently, in live, catch-up
   and backtest. Never "fix" this: it is the one assumption that cannot flatter
   results.
6. **`closed_with_pnl` is not the same set as `closed_statuses`.** A terminal
   status missing from the former is silently dropped from win rate, profit
   factor and drawdown. This has already corrupted metrics once.
7. **Windows file locking is real.** `os.replace` onto a file another process
   holds fails with WinError 5. That is section 5.1, and it is why the backup
   tool skips unreadable files rather than aborting.
8. **A cycle takes ~90s against a 2-minute interval.** Cycle duration is not
   cadence; that gap is why the refresh window is sized from `last_checked_at`
   rather than from the nominal interval.
9. **Alerts are queued before they are sent** (`outbox`). A transition is
   persisted with a `pending_alert` and cleared only on confirmed delivery. If
   you see `pending_alert` on a record, an alert is owed, not lost.

---

## 9. Suggested next actions

1. **Fix 5.1** — active signal loss, small fix, worse with every tenant added.
2. **Run horizon's baseline backtest** and the same window through livne
   (`--compare-with livne` prints the command). Until then horizon is unproven.
3. **Wire the rate governor into the live path (I2)** — two tenants now share
   an uncoordinated budget.
4. **Run the parity gate at 15m/5m/1h (R1)** before trusting intraday data.
5. **Tests for the strategy layer (R4)** — the signal logic is the largest
   untested surface in the repo.

---

## 10. Other reference documents

| file | what |
|---|---|
| `README.md` | how the system works, layout, adding a model |
| `GUIDE.md` | operator runbook, rollback quick reference |
| `docs/TESTING_STRATEGY.md` | the 100-model test architecture |
| `docs/HORIZON_CALIBRATION.md` | horizon's changes, rationale, what is unproven |
| `docs/CLIENT_ONBOARDING_GUIDE.md` | executable onboarding protocol |
| `docs/STRATEGY_SPEC_TEMPLATE.md` | spec sheet to send a client |
