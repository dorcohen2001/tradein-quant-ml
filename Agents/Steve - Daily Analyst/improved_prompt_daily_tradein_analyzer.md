# Task: Build an Automated Daily Paper-Trading Analyzer, Report Generator & Email Dispatcher

## Role & Goal
You are acting as a senior Python automation engineer. Build a **modular, production-ready daily workflow** on my Windows machine that: (1) analyzes my paper-trading system's performance, (2) generates a bold, strategic narrative report, (3) archives the report by date, and (4) emails it via my existing Gmail SMTP script.

## Environment (verified paths — do not "discover" alternatives)
- OS: Windows 10/11, Python 3.x (verify the installed version with `python --version` before assuming syntax/features).
- Primary system directory: `C:\Users\Dor\Desktop\TRADEIN`
- Trading system code: `C:\Users\Dor\Desktop\TRADEIN\TRADEIN` (screener, TA, macro, news, generator, live runner)
- Paper trading data: `C:\Users\Dor\Desktop\TRADEIN\TRADEIN\paper_trading`
  - `trades.json` — JSON list of paper trades (statuses: PENDING / ACTIVE / WIN_TP1 / WIN_TP2 / LOSS_SL / EXPIRED, with `pnl_pct`, `signal_date`, `exit_date`, etc.)
  - `paper_trades_journal.xlsx` — human-readable mirror of the same data; do NOT use it as a data source, read `trades.json`.
- Backtest data: `C:\Users\Dor\Desktop\TRADEIN\TRADEIN\backtest_suite\trades_log.csv` — simulated historical signals produced by the exact same production signal logic (`run_backtest.py`). Use it as a second, statistically larger sample alongside the live paper trades.
- Existing email tooling: `C:\Users\Dor\Desktop\TRADEIN\MailToMail` — contains a working `send_email.py` and a configured `.env` (`SENDER_EMAIL`, `SENDER_APP_PASSWORD`, `RECIPIENT_EMAIL`). Reuse it — do not rewrite the email logic or duplicate credentials.
- Report archive target: `C:\Users\Dor\Desktop\TRADEIN\MailToMail\Scripts` — create it if missing.

## Step 0 — Discovery Before Building (required)
Before writing the orchestrator:
1. Inspect `trades.json` and `trades_log.csv`: confirm column/field schemas, timestamp formats, and which statuses count as closed trades.
2. Read `send_email.py` to confirm its interface (function signature, env var names, attachment support).
3. Summarize what you found and state your analysis plan **before** implementing. If the data structure is ambiguous, ask me rather than guessing.

## Step 1 — Performance Analysis Module
Compute from real data (no fabricated numbers):
- Win/loss breakdown: win rate, average win vs. average loss, profit factor, expectancy per trade.
- Losing-trade forensics: recurring loss patterns (by symbol, asset class, direction, holding period), consecutive-loss streaks.
- Risk metrics: max drawdown (depth and duration), volatility of returns, Sharpe/Sortino **only if** enough closed trades exist to make them meaningful.
- Compute the same aggregates for the backtest CSV separately and label the two samples clearly (live vs. simulated).
- If a metric can't be computed from the available data, say so explicitly in the report instead of inventing it.

## Step 2 — Strategic Report Generation via local LLM handoff (no API key)
No external LLM API is called and no API key is stored. The narrative is produced by a local LLM operator (Claude Code on this machine) via a two-phase handoff:
- **Phase 1:** when at least `MIN_CLOSED_TRADES` (default 20) live trades are closed, the script prints the report instructions + a compact metrics JSON (~1–2 KB: live aggregates, backtest aggregates, last N closed trades), writes them to `llm_request.json`, and exits with code 3 ("narrative requested") without archiving/emailing. NEVER include `trades.json`, the XLSX, or raw headline lists — the payload must not grow with history size.
- **Phase 2:** the operator generates the narrative Markdown and delivers it with `--narrative <file.md>`; the script appends a deterministic data appendix, then archives and emails.
- **Minimum-sample gate:** below the threshold (or with `--no-llm`), skip the handoff and archive/email a short deterministic status note ("N closed trades; insufficient sample for strategy revision") plus the backtest aggregates. This is the normal unattended path early on — do not treat it as a failure.

**Report content requirements:**
- Bold, high-level strategic thinking — not minor parameter tweaks. Propose: new technical indicators, alternative risk-management frameworks, fundamental/macro filters, revised entry/exit logic, position-sizing changes, and hypotheses worth testing — each with rationale tied to the actual weaknesses found in Step 1.
- **STRICTLY NO CODE in the report** — describe what to add/modify/remove in plain prose only.
- Format: Markdown. Structure: Executive Summary → Performance Snapshot (live + backtest) → Weaknesses & Loss Patterns → Strategic Proposals (ranked by expected impact) → Concrete Next Actions.

## Step 3 — Archiving & Email
- Save each report as `report_YYYY-MM-DD.md` in `C:\Users\Dor\Desktop\TRADEIN\MailToMail\Scripts`. If a report for today already exists, **overwrite it** (the job is idempotent; no `_v2` suffixes).
- Send the report via the existing `send_email.py` to `RECIPIENT_EMAIL`. Subject line: `TRADEIN Daily Report — YYYY-MM-DD`. Attach the `.md` and put the executive summary in the body.
- Never print or log credentials.

## Step 4 — Orchestrator & Robustness
Deliver `daily_tradein_analyzer.py` as the master entry point, with the analysis, report, archive, and email steps as separate functions (modular, individually testable).
- Wrap each stage in error handling; if a stage fails, log the error to a rotating log file (`analyzer.log` next to the script) and still attempt to archive whatever was produced. Exit with a nonzero code on failure so Task Scheduler shows it.
- No hardcoded absolute paths scattered through the code — centralize paths in the existing `config.py` / `.env`.
- Keep dependencies minimal (`anthropic` is the only new one).
- Provide `--no-email` and `--no-llm` flags for dry runs.

## Step 5 — Scheduling Instructions
Provide concise, copy-pasteable instructions for Windows Task Scheduler:
- Exact `schtasks` command to run daily at a time I choose (default 08:00).
- Correct "Start in" working directory, full path to `python.exe`, "Run whether user is logged on or not", and wake-the-computer notes.
- How to test the scheduled task immediately and where to check logs if it silently fails.

## Acceptance Criteria (definition of done)
1. Running `python daily_tradein_analyzer.py` manually produces a dated report in the archive folder and sends the email successfully.
2. The report contains real computed metrics from my data, zero code blocks, and (when the sample gate passes) concrete strategic proposals.
3. All paths, credentials, and the recipient come from config/`.env` — nothing sensitive hardcoded.
4. Task Scheduler instructions verified to work with one test run.
