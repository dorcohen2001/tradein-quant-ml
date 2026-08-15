# TRADEIN — Operations & AI Prompt Runbook

Copy-paste reference for running the system, plus prompt templates for fresh
Claude Code sessions.

**Every command below has been run against this repository and reflects its
actual CLI flags.** Where something you might expect does not exist, that is
stated plainly rather than invented.

---

## Table of Contents

**[Section 1 — Terminal Cheat Sheet](#section-1--terminal-cheat-sheet)**
- [1.0 Environment](#10-environment)
- [1.1 Supervisor & live execution](#11-supervisor--live-execution)
- [1.2 Model scaffolding & management](#12-model-scaffolding--management)
- [1.3 Backup & state management](#13-backup--state-management)
- [1.4 Analysis & reports (Steve)](#14-analysis--reports-steve)
- [1.5 Testing & integrity](#15-testing--integrity)
- [1.6 Extras: backtest, charts, Telegram](#16-extras-backtest-charts-telegram)

**[Section 2 — AI Prompt Templates](#section-2--ai-prompt-templates)**
- [A — Onboard a new client strategy](#template-a--onboard-a-new-client-strategy)
- [B — Modify an existing model's strategy](#template-b--modify-an-existing-models-strategy)
- [C — Enable / disable a model (safe toggle)](#template-c--enable--disable-a-model-safe-toggle)
- [D — Graduate incubating → production](#template-d--graduate-incubating--production)
- [E — Pre-refactor snapshot](#template-e--pre-refactor-snapshot)

---

# SECTION 1 — TERMINAL CHEAT SHEET

## 1.0 Environment

**There is no virtualenv in this project.** It runs on the system Python
(3.14.5). Do not look for `venv\Scripts\activate` — it does not exist. If you
later create one, activate it before every command in this guide.

All commands are run **from the repository root** unless stated otherwise:

```bash
cd C:\Users\Dor\Desktop\TRADEIN
```

Confirm the interpreter and that dependencies are installed:

```bash
python --version && python -c "import pandas, yfinance, plotly; print('deps OK')"
```

Install / refresh dependencies (versions are pinned):

```bash
pip install -r requirements.txt
```

If reports or alerts print with `?` or `�` where an em-dash or emoji should be,
that is the CMD console codepage, not corrupted data. Switch the window to
UTF-8 first:

```bash
chcp 65001
```

See which models exist, their stage, and whether they are enabled:

```bash
python -c "import sys; sys.path.insert(0,'.'); from core.registry import all_models; [print(f'{m.key:22} stage={m.stage:11} enabled={m.enabled} route={m.telegram_route}') for m in all_models()]"
```

---

## 1.1 Supervisor & live execution

The supervisor is the normal way to run everything. It gives each model its own
thread, interval, timeout and CLI — no model can delay or crash another.

```bash
# Run every ENABLED model forever, each on its own interval. Ctrl+C to stop.
python run_supervisor.py
```

```bash
# Show the schedule and the exact child commands. Launches nothing. Start here.
python run_supervisor.py --dry-run
```

```bash
# Run exactly one cycle per model, then exit. Good for a scheduled task.
python run_supervisor.py --once
```

```bash
# Run only specific models (comma-separated keys).
python run_supervisor.py --models livne
```

```bash
# Also run models marked enabled=False. Explicit override of a deliberate pause.
python run_supervisor.py --models biton --include-disabled
```

```bash
# Refresh open positions only — no new-signal scan.
python run_supervisor.py --once --skip-scan
```

```bash
# Force an asset class / size across every model (otherwise each uses its own config).
python run_supervisor.py --once --asset-class crypto --top-n 1
```

### Running one model directly

```bash
# One cycle of a single model, bypassing the supervisor.
cd models\livne && python live_runner.py --once
```

```bash
# Dry scan: prints setups, sends no alerts and logs no paper trades.
cd models\livne && python main.py --asset-class crypto --top-n 1
```

```bash
# Run a model that is disabled in the registry (one-off).
cd models\biton && python live_runner.py --once --force
```

> A model marked `enabled=False` refuses to run directly without `--force`, so a
> stale Task Scheduler entry cannot quietly restart something you paused.

---

## 1.2 Model scaffolding & management

```bash
# Scaffold an isolated client strategy and register it as incubating.
python scripts\create_client_model.py --key alpha --name "Alpha Capital" --interval 5 --timeframe 15m --asset-class crypto
```

Flags: `--key` `--name` `--interval` (minutes) `--timeframe` (`5m`/`15m`/`1h`/`4h`/`1d`)
`--asset-class` (`crypto`/`stocks`/`both`) `--description` `--force` `--remove`.

```bash
# Roll back a scaffold: deregisters it and deletes models/client_<key>/.
python scripts\create_client_model.py --remove alpha
```

> `--remove` deliberately **keeps** runtime history (trade store, journal,
> charts, funnel exports). Removing a scaffold is not a statement about the data
> it produced. Delete those by hand if you really want them gone.

```bash
# Overwrite an existing scaffold (keeps the registry entry intact).
python scripts\create_client_model.py --key alpha --name "Alpha Capital" --force
```

Writes to exactly two places: `models/client_<key>/`, and one entry between the
`CLIENT MODELS` markers in `core/registry.py`. Nothing else is touched.

---

## 1.3 Backup & state management

`runtime/` is gitignored — git does **not** back up your trade stores. This is
the tool that does.

```bash
# Full snapshot: code + configs + live runtime state. The restore point.
python scripts\backup_manager.py --mode full
```

```bash
# Live trading state only (stores + journals). Cheap; safe to run often.
python scripts\backup_manager.py --mode runtime
```

```bash
# Source only, no state.
python scripts\backup_manager.py --mode code
```

```bash
# Archive with no .env — safe to copy off this machine.
python scripts\backup_manager.py --mode full --exclude-secrets
```

```bash
# Integrity-check an archive: CRC + manifest count + re-parses every trade store.
python scripts\backup_manager.py --verify-snapshot backups\full_snapshots\TRADEIN_FULL_BACKUP_20260815_095153.zip
```

```bash
# List every archive with size and timestamp.
python scripts\backup_manager.py --list
```

```bash
# Keep only the newest N of this mode (default 10; 0 disables pruning).
python scripts\backup_manager.py --mode runtime --retention 30
```

> A `full` archive **contains `.env`** so it can genuinely restore credentials.
> Keep those local. Every write is verified by reading it back.

### Runtime artifact retention

```bash
# Show what would be pruned from the yfinance cache and charts. Deletes nothing.
python scripts\prune_runtime.py --dry-run
```

```bash
# Prune for real (defaults: cache >7 days, charts >14 days).
python scripts\prune_runtime.py
```

```bash
# Custom ages; 0 disables that sweep.
python scripts\prune_runtime.py --cache-days 3 --chart-days 7
```

### Restore

```bash
# Inspect an archive without extracting.
python -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); print('\n'.join(z.namelist()[:40]))" backups\full_snapshots\TRADEIN_FULL_BACKUP_20260815_095153.zip
```

```bash
# Pull ONE file out of an archive to a safe scratch location, then compare before overwriting.
python -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extract('runtime/stores/trades_livne.json', r'C:\Users\Dor\Desktop\restore_tmp')" backups\full_snapshots\TRADEIN_FULL_BACKUP_20260815_095153.zip
```

> **Stop the supervisor before restoring anything into `runtime/`.** A live
> runner writes those files every cycle and will overwrite what you restore.

---

## 1.4 Analysis & reports (Steve)

```bash
# Standard daily analysis over PRODUCTION models. Archives + emails the report.
python agents\steve\daily_tradein_analyzer.py
```

```bash
# Deterministic metrics report, no LLM narrative handoff, no email. Safest for a look.
python agents\steve\daily_tradein_analyzer.py --no-llm --no-email
```

```bash
# Include incubating models (client trials) in the report.
python agents\steve\daily_tradein_analyzer.py --include-incubating --no-llm --no-email
```

```bash
# Lower the sample gate that triggers the narrative handoff (default 20 closed trades).
python agents\steve\daily_tradein_analyzer.py --min-closed 5
```

```bash
# Phase 2: deliver an externally written narrative, then archive + email.
python agents\steve\daily_tradein_analyzer.py --narrative agents\steve\narrative_2026-08-15.md
```

Exit codes: `0` success · `1` a stage failed · `3` metrics emitted, awaiting a
narrative (phase 2).

### Date-specific reports — how this actually works

**There is no `--date` flag.** Steve always analyses the *current* state of the
trade stores and writes `mail/reports/report_<today>.md`. It cannot reconstruct
a historical report, because the stores hold current status, not daily
snapshots.

What you can do:

```bash
# Read an archived report from a past date.
type mail\reports\report_2026-08-15.md
```

```bash
# List every archived report.
dir mail\reports
```

```bash
# Recover a report from a backup archive taken on that date.
python -c "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); print(z.read('mail/reports/report_2026-08-15.md').decode('utf-8'))" backups\full_snapshots\TRADEIN_FULL_BACKUP_20260815_095153.zip
```

> If you need true point-in-time reporting, schedule
> `--mode runtime` backups daily — that is the only thing that captures the
> store as it stood on a given day.

---

## 1.5 Testing & integrity

```bash
# Full suite. Run this after any change.
pytest tests/
```

```bash
# Quiet one-line-per-file summary.
pytest tests/ -q
```

```bash
# Client sandbox isolation: paths, routing, crash containment, report gating.
pytest tests/test_client_isolation.py -v
```

```bash
# Registry: path derivation pinned against the pre-refactor literals.
pytest tests/test_registry.py -q
```

```bash
# Fast smoke: stop at the first failure, skip the slow subprocess tests.
pytest tests/ -x -q --deselect tests/test_catch_up.py
```

```bash
# Filter by substring of the test NAME (not the file). Example: 15 isolation tests.
pytest tests/ -q -k "collide or route"
```

```bash
# Show the slowest 10 tests.
pytest tests/ -q --durations=10
```

### Integrity checks

```bash
# Trade stores are valid JSON, with counts. (Raises if a store is corrupt.)
python -c "import json,io; [print(k, len(json.load(io.open('runtime/stores/trades_'+k+'.json', encoding='utf-8'))), 'trades') for k in ('livne','biton')]"
```

```bash
# Byte-level check that live state has not moved.
python -c "import hashlib; [print(k, hashlib.sha256(open(f'runtime/stores/trades_{k}.json','rb').read()).hexdigest()[:16]) for k in ('livne','biton')]"
```

```bash
# Confirm no source change leaked into the live model trees.
git diff --stat HEAD -- models/livne models/biton
```

```bash
# Syntax-check everything without running it.
python -m compileall -q core models scripts agents run_supervisor.py
```

---

## 1.6 Extras: backtest, charts, Telegram

```bash
# Intraday backtest for a model. --start-date and --end-date are REQUIRED.
cd models\livne && python -m backtest_suite.run_backtest --start-date 2026-07-01 --end-date 2026-08-01
```

```bash
# Backtest a specific ticker set with a finer execution timeframe, printing every trade.
cd models\livne && python -m backtest_suite.run_backtest --start-date 2026-07-01 --end-date 2026-08-01 --tickers BTCUSDT,ETHUSDT --exec-interval 5m --verbose
```

```bash
# Replay purely from cached candles — no network at all.
cd models\livne && python -m backtest_suite.run_backtest --start-date 2026-07-01 --end-date 2026-08-01 --offline
```

```bash
# Check where a Telegram route resolves, and send a test message to it.
python -m core.utils.telegram_notifier --route livne
```

```bash
# Chart rendering smoke test (renders only; add --send to actually post).
python core\charting\test_chart_alert.py --model livne
```

---

# SECTION 2 — AI PROMPT TEMPLATES

Copy one of these into a **fresh Claude Code session**, fill the `[BRACKETS]`,
and delete any optional line you do not need.

Each template already carries the safety rules. Keep them — they are what stops
a session from editing a live model.

---

## Template A — Onboard a new client strategy

```
Onboard a new client strategy. Read docs/CLIENT_ONBOARDING_GUIDE.md first and follow its protocol.

HARD RULES:
- Do NOT modify models/livne/, models/biton/, or anything under runtime/.
- Register the client ONLY via scripts/create_client_model.py (never hand-edit core/registry.py).
- Do not ask me for the bot token; I will put it in .env myself.

IDENTITY
  key:          [alpha]
  display name: [Alpha Capital]
  description:  [one line for the report roster]

MARKET & EXECUTION
  asset class:  [crypto | stocks | both]
  watchlist:    [BTCUSDT, ETHUSDT, SOLUSDT]
  timeframe:    [15m]
  interval:     [5] minutes between cycles

STRATEGY RULES
  indicators:   [EMA 20/50, RSI 14, ATR 14]
  entry LONG:   [describe precisely]
  entry SHORT:  [describe precisely, or "long only"]
  stop loss:    [1.5 x ATR | fixed 2% | structural swing]
  take profit:  [TP1 2.5 x ATR, TP2 4 x ATR]
  min R:R:      [1.5]
  expiry:       [12] hours before an unfilled signal goes stale

TELEGRAM
  [dedicated bot | reuse existing bot], destination is a [channel | group]
  I will set TELEGRAM_ROUTE_CLIENT_[KEY]_TOKEN and _CHAT_ID in .env myself.

WHEN DONE:
1. Run the scaffolder, then implement the rules above in models/client_[alpha]/strategy/.
2. Verify: dry run the model, `python run_supervisor.py --dry-run`, `pytest tests/`.
3. Confirm trades_livne.json and trades_biton.json are unchanged.
4. Report what you created and what I still need to do.
```

> If you do not have firm rules yet, replace the STRATEGY RULES block with
> `strategy rules: not decided yet — scaffold with the placeholder rule`.

---

## Template B — Modify an existing model's strategy

```
Modify the strategy logic for model [client_alpha].

SCOPE — you may ONLY edit:
  models/[client_alpha]/strategy/    (screener.py, ta_engine.py)
  models/[client_alpha]/config.py    (tunable parameters only)

DO NOT TOUCH:
- core/ (any shared logic), run_supervisor.py, agents/, scripts/
- any other model directory
- runtime/ — no trade store, journal or log may be modified
- the trade record schema or any status name (existing open positions depend on both)

CHANGE REQUESTED:
  [describe the logic change precisely — e.g. "add a 200 EMA trend filter:
   only take LONGs when Close > EMA200 on the analysis timeframe"]

CONSTRAINTS:
- Use only bars up to the last row. No lookahead.
- Keep the (setup, reason) return contract of evaluate().
- Keep rejection reasons informative — they are the only debugging surface.

VERIFY BEFORE REPORTING DONE:
1. cd models/[client_alpha] && python live_runner.py --once --dry-run
2. pytest tests/
3. git diff --stat  — confirm only the two allowed paths changed
4. Confirm every trades_*.json is byte-identical to before
```

---

## Template C — Enable / disable a model (safe toggle)

```
[Disable | Enable] the model [biton] in core/registry.py.

CHANGE: set enabled=[False | True] on that model's ModelConfig entry. Nothing else.

ABSOLUTE CONSTRAINTS:
- Do NOT delete or edit its registry entry beyond that one boolean.
- Do NOT touch runtime/stores/trades_[biton].json, its journal, funnel exports or logs.
- Do NOT modify any other model.

VERIFY:
1. python run_supervisor.py --dry-run          -> model is [absent | present] as expected
2. python agents/steve/daily_tradein_analyzer.py --no-llm --no-email
3. pytest tests/
4. sha256 of runtime/stores/trades_*.json unchanged before vs after

REPORT:
- How many positions are non-terminal (PENDING/ACTIVE/PARTIAL_TP1) in that model.
- If disabling: state plainly that those positions will NOT be monitored while it
  is off, and that on re-enable they are graded against the price at that moment.
```

---

## Template D — Graduate incubating → production

```
Promote model [client_alpha] from stage="incubating" to stage="production" in core/registry.py.

FIRST — check the graduation checklist in docs/CLIENT_ONBOARDING_GUIDE.md §6 and
report whether each item is met. Do NOT promote if the sample is too small; tell
me instead. Report:
  - closed trade count, win rate, expectancy, max drawdown
  - how long it has been running
  - whether live results are consistent with its backtest

CHANGE (only if I confirm): stage="incubating" -> stage="production". One line.
Keep its key, paths, history and dedicated telegram_route exactly as they are.

VERIFY NO REGRESSION:
1. Every derived path is unchanged (trade store, journal, funnel prefix, chart prefix)
2. telegram_route still resolves ONLY to the client's own chat, dedicated_route still True
3. python agents/steve/daily_tradein_analyzer.py --no-llm --no-email  -> now includes it
4. python run_supervisor.py --dry-run  -> shows [production]
5. pytest tests/  -> all pass
6. trades_livne.json / trades_biton.json byte-identical
```

---

## Template E — Pre-refactor snapshot

```
Take a full safety snapshot before major work. Do this BEFORE touching any code.

1. Confirm the live runner is stopped. If any python process is running the
   supervisor or a live_runner, stop and tell me — do not proceed.

2. python scripts/backup_manager.py --mode full
   Report the archive path, file count and size.

3. Verify it:
   python scripts/backup_manager.py --verify-snapshot <path from step 2>
   Confirm CRC OK and that every runtime JSON re-parsed successfully.
   Confirm runtime/stores/trades_*.json are inside the archive with their trade counts.

4. Commit anything uncommitted (report what, do not skip untracked files):
   git status --porcelain
   git add -A && git commit

5. Tag it:
   git tag -a backup-pre-[SHORT_DESCRIPTION]-$(date +%Y%m%d_%H%M%S) -m "Snapshot before [WHAT YOU ARE ABOUT TO DO]"

6. Report: archive path, verification result, commit hash, tag name, and the
   exact command to roll back to this point.
```

---

## Appendix — Rollback quick reference

```bash
# What restore points exist?
git tag -l -n1 && python scripts\backup_manager.py --list
```

```bash
# Roll code back to a tag, as a branch (never detached HEAD).
git switch -c rollback/from-tag backup-baseline-pre-refactor-20260815_095246
```

```bash
# Recover a single file from git without touching anything else.
git checkout backup-baseline-pre-refactor-20260815_095246 -- core/registry.py
```

> Rolling code back does **not** roll back `runtime/`. If the change crossed a
> trade-schema or lifecycle change, restore the matching `--mode runtime`
> archive too, or the old code will read records it does not understand.
