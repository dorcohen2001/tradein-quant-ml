"""Daily Paper-Trading Analyzer, Report Generator & Email Dispatcher ("Steve").

No external LLM API is called and no API key is required. The strategic
narrative is produced by a *local* LLM operator (Claude Code running on this
machine) through a two-phase handoff:

Covers EVERY model registered in `core.registry` in one report -- currently
Livne (multi-timeframe intraday pipeline) and Biton (frozen legacy pipeline),
but the roster, the per-model report sections and the email subject are all
derived from the registry, so a newly registered model appears automatically.
Each model gets its own clearly separated section -- their numbers are never
pooled, because they are different strategies with different lifecycles,
different status vocabularies and different minimum R:R.

  Phase 1 (analyze + request):
      python agents/steve/daily_tradein_analyzer.py
      - Computes aggregate metrics per model from runtime/stores/trades_*.json
        (live) and runtime/backtest/trades_log_*.csv (simulated), in Python.
      - If at least --min-closed live trades are closed across both models,
        prints the report instructions + compact metrics JSON, writes them to
        runtime/logs/llm_request.json, and exits with code 3 ("narrative
        requested"). Nothing is archived or emailed yet.
      - Below the gate (or with --no-llm) it skips the narrative and goes
        straight to archive + email with a deterministic metrics report.

  Phase 2 (deliver):
      python agents/steve/daily_tradein_analyzer.py --narrative path/to/file.md
      - Takes the externally generated Markdown narrative, appends the
        deterministic data appendix, archives the report as
        mail/reports/report_YYYY-MM-DD.md (overwrite; idempotent), and
        emails it as a SINGLE message covering both models (subject
        "TRADEIN Daily Report — YYYY-MM-DD (<registered models>)", .md attached,
        executive summary in the body).

Other usage:
    ... --no-email      # skip the email stage
    ... --no-llm        # deterministic report, full delivery
    ... --min-closed 5  # override the sample gate

Exit codes: 0 = success, 1 = a stage failed (Task Scheduler surfaces this),
3 = metrics emitted, awaiting an externally generated narrative (phase 2).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import statistics
import sys
from logging.handlers import RotatingFileHandler

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# This script lives in agents/steve/ and no longer imports ANY model's config.
#
# It used to insert Livne's model directory on sys.path and import its
# `config` as "the canonical one", because the two models define clashing
# top-level module names (`config`, `paper_trading`, `utils`) and cannot both
# be imported into one process. That worked, but it made a tool which reports
# on every model depend on one specific model existing, and it meant Biton's
# paths were declared inside Livne's config.
#
# Both problems disappear by importing from `core` instead: `core.registry`
# knows every model's paths and status vocabulary, and `core.settings` holds
# the analyzer's own settings. No model package is imported at all, so the
# name-clash constraint simply does not apply any more.
#
# Each model's trades are still read straight from its JSON store rather than
# through its tracker -- that avoids running one model's schema migration over
# another model's records.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_SCRIPT_DIR, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.registry import all_models
from core.settings import (
    ANALYZER_LOG_PATH,
    LLM_REQUEST_PATH,
    MAIL_DIR,
    MIN_CLOSED_TRADES_FOR_LLM,
    REPORT_ARCHIVE_DIR,
)

LOG_PATH = ANALYZER_LOG_PATH
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NARRATIVE_REQUESTED = 3

logger = logging.getLogger("daily_tradein_analyzer")
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)
logger.addHandler(logging.StreamHandler(sys.stderr))
# send_email.py's basicConfig installs a root handler; don't propagate to it
# or every analyzer log line after the email stage prints twice.
logger.propagate = False

# Every registered model, in canonical report order. Previously a hard-coded
# tuple here plus a separate hard-coded CLOSED_WITH_PNL dict; both are now the
# registry's job, so adding a model needs no edit in this file.
#
# The per-model status vocabulary that `CLOSED_WITH_PNL` used to hold now lives
# on each ModelConfig as `closed_with_pnl`. It stays strictly per-model for the
# reason it was split in the first place: Livne runs a multi-tier
# TP1/breakeven lifecycle and never emits WIN_TP1 at all, while Biton runs the
# older single-shot lifecycle and does. Applying one model's set to the other
# silently dropped every Livne LOSS_SL_BE and EMERGENCY_EXIT trade -- each
# carrying a real pnl_pct -- from win rate, profit factor, expectancy and
# drawdown. EXPIRED is excluded for every model: it never entered, so it has
# no P&L.
MODELS = all_models()


def _model_roster() -> str:
    """The 'which models exist' paragraph of the narrative prompt, built from
    the registry so a newly registered model is described to the analyst
    automatically instead of silently missing from the brief."""
    return "\n".join(
        f'  - "{m.label}" — {m.description or "no description registered."}'
        for m in MODELS
    )


def _model_sections() -> str:
    """The per-model heading block of the requested report structure."""
    return "\n".join(
        f"## {m.label}\n"
        f"### Performance Snapshot  (live vs. simulated, clearly labeled)\n"
        f"### Weaknesses & Loss Patterns"
        for m in MODELS
    )


def narrative_instructions() -> str:
    """Built at call time rather than as a module constant, so the roster and
    the section list always reflect the models actually registered."""
    count = len(MODELS)
    return f"""\
You are a senior trading-strategy analyst reviewing a systematic paper-trading
program that runs {count} INDEPENDENT MODELS CONCURRENTLY in production:

{_model_roster()}

They are separate strategies on the same watchlists. The payload below gives,
per model, a "live" sample (real-time paper trades) and a "backtest" sample
(historical simulation of that same model's signal logic; note its entry-fill
assumption is slightly optimistic).

Write a daily strategy report in Markdown with exactly this structure:
# TRADEIN Daily Report — {{date}}
## Executive Summary  (3-5 sentences, blunt, decision-oriented, covering ALL models)
{_model_sections()}
## Comparative Read  (where the models agree/diverge, and what that implies)
## Strategic Proposals  (ranked by expected impact; bold high-level ideas —
   new indicator families, risk-management frameworks, macro filters, revised
   entry/exit logic, position sizing — each tied to a specific weakness above,
   and each stating WHICH model it applies to)
## Concrete Next Actions  (numbered, testable steps)

Hard rules:
- STRICTLY NO CODE: no code blocks, no snippets, no syntax. Plain prose only.
- Use ONLY numbers present in the payload. If a metric is null or absent, say
  it could not be computed — never invent a value.
- NEVER pool different models' numbers into a single blended figure, and never
  compare one model's number against another's without saying which is which.
- If a live sample is small, say so and weight that model's backtest instead.
- Keep it under ~1200 words.

When done, save the report as a Markdown file and deliver it with:
    python agents/steve/daily_tradein_analyzer.py --narrative <path-to-file.md>"""


# ---------------------------------------------------------------------------
# Stage 1 — metrics
# ---------------------------------------------------------------------------
def _pnl_stats(pnls: list[float]) -> dict:
    if not pnls:
        return {}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    stats = {
        "trades_with_pnl": len(pnls),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 1),
        "avg_win_pct": round(statistics.mean(wins), 2) if wins else None,
        "avg_loss_pct": round(statistics.mean(losses), 2) if losses else None,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy_pct_per_trade": round(statistics.mean(pnls), 2),
        "total_return_pct_sum": round(sum(pnls), 2),
        "best_trade_pct": round(max(pnls), 2),
        "worst_trade_pct": round(min(pnls), 2),
    }
    # Per-trade risk-adjusted ratio and drawdown only when the sample supports it
    if len(pnls) >= 10 and statistics.pstdev(pnls) > 0:
        stats["per_trade_sharpe"] = round(statistics.mean(pnls) / statistics.pstdev(pnls), 2)
        downside = [p for p in pnls if p < 0]
        if downside and statistics.pstdev(downside) > 0:
            stats["per_trade_sortino"] = round(
                statistics.mean(pnls) / statistics.pstdev(downside), 2)
    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    stats["max_drawdown_pct_points"] = round(max_dd, 2)
    streak = worst_streak = 0
    for p in pnls:
        streak = streak + 1 if p <= 0 else 0
        worst_streak = max(worst_streak, streak)
    stats["max_consecutive_losses"] = worst_streak
    return stats


def _group_stats(rows: list[dict], key: str) -> dict:
    groups: dict[str, list[float]] = {}
    for r in rows:
        groups.setdefault(r[key], []).append(r["pnl"])
    return {
        k: {"trades": len(v),
            "win_rate_pct": round(sum(1 for p in v if p > 0) / len(v) * 100, 1),
            "avg_pnl_pct": round(statistics.mean(v), 2)}
        for k, v in sorted(groups.items())
    }


def _read_store(path: str) -> list[dict]:
    """Read a model's paper-trading JSON store directly. A missing store is an
    empty sample (the model may simply not have traded yet); a corrupt one is
    surfaced loudly rather than silently reported as zero trades."""
    if not os.path.exists(path):
        logger.info("No trade store at %s — treating as an empty sample.", path)
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def compute_live_metrics(model) -> dict:
    """Aggregate one model's live paper trades. Takes the ModelConfig itself
    rather than a (key, path) pair so the store location and the P&L-bearing
    status set can never be mismatched -- pairing one model's store with
    another's vocabulary is exactly the bug that once zeroed out Livne's
    LOSS_SL_BE and EMERGENCY_EXIT trades."""
    store_path = model.trade_store
    trades = _read_store(store_path)
    by_status: dict[str, int] = {}
    for t in trades:
        by_status[t["current_status"]] = by_status.get(t["current_status"], 0) + 1

    closed_statuses = model.closed_with_pnl
    closed = [t for t in trades
              if t["current_status"] in closed_statuses and t.get("pnl_pct") is not None]
    closed.sort(key=lambda t: t.get("exit_date") or t["signal_date"])
    rows = [{"pnl": float(t["pnl_pct"]), "ticker": t["ticker"],
             "asset_class": t["asset_class"], "direction": t["direction"]} for t in closed]
    pnls = [r["pnl"] for r in rows]

    metrics = {
        "sample": f"live paper trades ({os.path.basename(store_path)})",
        "total_signals": len(trades),
        "status_counts": by_status,
        "closed_trades": len(closed),
        **_pnl_stats(pnls),
    }
    if rows:
        metrics["by_asset_class"] = _group_stats(rows, "asset_class")
        metrics["by_direction"] = _group_stats(rows, "direction")
        metrics["recent_closed_trades"] = [
            {"ticker": t["ticker"], "direction": t["direction"],
             "status": t["current_status"], "pnl_pct": t["pnl_pct"],
             "signal_date": t["signal_date"][:10],
             "exit_date": (t.get("exit_date") or "")[:10]}
            for t in closed[-10:]
        ]
    return metrics


def compute_backtest_metrics(csv_path: str) -> dict | None:
    if not os.path.exists(csv_path):
        logger.info("No backtest CSV at %s — skipping backtest sample.", csv_path)
        return None
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                rows.append({"pnl": float(row["pnl_pct"]), "ticker": row["ticker"],
                             "asset_class": row["asset_class"],
                             "direction": row["direction"], "outcome": row["outcome"]})
            except (KeyError, ValueError):
                continue
    if not rows:
        return None
    pnls = [r["pnl"] for r in rows]
    outcomes: dict[str, int] = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    return {
        "sample": f"simulated backtest signals ({os.path.basename(csv_path)}; entry fill assumed at "
                  "signal-date mid-entry — slightly optimistic vs. live)",
        "total_signals": len(rows),
        "outcome_counts": outcomes,
        **_pnl_stats(pnls),
        "by_asset_class": _group_stats(rows, "asset_class"),
        "by_direction": _group_stats(rows, "direction"),
    }


# ---------------------------------------------------------------------------
# Stage 2 — report assembly / narrative handoff
# ---------------------------------------------------------------------------
def _metrics_markdown(title: str, m: dict | None) -> str:
    if not m:
        return f"### {title}\n\nNo data available.\n"
    lines = [f"### {title}", ""]
    for k, v in m.items():
        if k in ("recent_closed_trades",):
            continue
        if isinstance(v, dict):
            lines.append(f"- **{k}**:")
            lines.extend(f"  - {gk}: {gv}" for gk, gv in v.items())
        else:
            lines.append(f"- **{k}**: {v}")
    lines.append("")
    return "\n".join(lines)


def _model_section(label: str, sample: dict) -> str:
    """One model's block: its live sample and its own backtest sample, always
    labelled, so different models' numbers can never be read as one pooled
    figure. They run different strategies and are expected to disagree."""
    return (
        f"## {label}\n\n"
        + _metrics_markdown(f"{label} — live paper trades", sample["live"])
        + "\n" + _metrics_markdown(f"{label} — backtest (simulated)", sample["backtest"])
    )


def _data_appendix(samples: dict[str, dict]) -> str:
    out = ["\n\n---\n\n# Data Appendix (computed, not narrative)\n"]
    for model, sample in _ordered(samples):
        out.append(_model_section(model.label, sample))
    return "\n".join(out)


def _ordered(samples: dict[str, dict]):
    """(ModelConfig, sample) pairs in canonical registry order, skipping any
    model that produced no sample this run."""
    return [(m, samples[m.key]) for m in MODELS if m.key in samples]


def build_deterministic_report(samples: dict[str, dict], gate_note: str) -> str:
    today = dt.date.today().isoformat()
    parts = [f"# TRADEIN Daily Report — {today}\n", f"## Executive Summary\n\n{gate_note}\n"]
    for model, sample in _ordered(samples):
        parts.append(_model_section(model.label, sample))
    return "\n".join(parts)


def emit_narrative_request(samples: dict[str, dict]) -> None:
    """Phase 1 handoff: print + persist the instructions and metrics payload
    for the local LLM operator (Claude Code). No API call is made."""
    payload = {
        "report_date": dt.date.today().isoformat(),
        "models": {m.key: {"label": m.label, **sample} for m, sample in _ordered(samples)},
    }
    request = {"instructions": narrative_instructions(), "payload": payload}
    os.makedirs(os.path.dirname(LLM_REQUEST_PATH), exist_ok=True)
    with open(LLM_REQUEST_PATH, "w", encoding="utf-8") as f:
        json.dump(request, f, indent=2)

    print("=" * 70)
    print("NARRATIVE REQUESTED — local LLM generation required (no API call made)")
    print(f"Request also written to: {LLM_REQUEST_PATH}")
    print("=" * 70)
    print("\n--- INSTRUCTIONS " + "-" * 52)
    print(narrative_instructions())
    print("\n--- METRICS PAYLOAD (JSON) " + "-" * 42)
    print(json.dumps(payload, indent=2))
    print("=" * 70)
    logger.info("Narrative request emitted to stdout and %s (exit code %d).",
                LLM_REQUEST_PATH, EXIT_NARRATIVE_REQUESTED)


def load_narrative(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        narrative = f.read().strip()
    if not narrative:
        raise ValueError(f"narrative file {path} is empty")
    return narrative


# ---------------------------------------------------------------------------
# Stage 3 & 4 — archive + email
# ---------------------------------------------------------------------------
def archive_report(report_md: str) -> str:
    os.makedirs(REPORT_ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(REPORT_ARCHIVE_DIR, f"report_{dt.date.today().isoformat()}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_md)
    logger.info("Report archived to %s", path)
    return path


def _executive_summary(report_md: str) -> str:
    """First ~15 lines after the Executive Summary heading, for the email body."""
    lines = report_md.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.lstrip("# ").startswith("Executive Summary")) + 1
    except StopIteration:
        return "\n".join(lines[:15])
    body = []
    for l in lines[start:]:
        if l.startswith("#"):
            break
        body.append(l)
        if len(body) >= 15:
            break
    return "\n".join(body).strip() or "See attached report."


def email_report(report_path: str, report_md: str) -> bool:
    """One email covering both models -- the report body already separates
    them into their own sections, so a single message keeps the daily
    comparison in one place rather than splitting it across two inboxes."""
    if MAIL_DIR not in sys.path:
        sys.path.append(MAIL_DIR)
    from send_email import send_report_email

    roster = " + ".join(m.label.replace(" model", "") for m in MODELS)
    subject = f"TRADEIN Daily Report — {dt.date.today().isoformat()} ({roster})"
    body = (_executive_summary(report_md)
            + "\n\n(Full report attached; archived copy in mail/reports.)")
    return send_report_email(subject, body, attachments=[report_path])


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="TRADEIN daily paper-trading analyzer")
    parser.add_argument("--no-email", action="store_true", help="skip the email stage")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip the narrative handoff; deliver the deterministic report")
    parser.add_argument("--narrative", metavar="FILE",
                        help="phase 2: deliver an externally generated narrative Markdown file")
    parser.add_argument("--min-closed", type=int, default=MIN_CLOSED_TRADES_FOR_LLM,
                        help="minimum closed live trades before requesting a narrative "
                             "(default: %(default)s)")
    args = parser.parse_args()

    logger.info("=== daily_tradein_analyzer start ===")
    failed = False

    # Stage 1 — analyze EVERY registered model. One model failing must not
    # suppress another's report: each is captured independently and an error is
    # recorded in place of that model's metrics.
    samples: dict[str, dict] = {}
    for model in MODELS:
        try:
            live = compute_live_metrics(model)
            logger.info("%s live metrics: %d signals, %d closed.",
                        model.label, live["total_signals"], live["closed_trades"])
        except Exception:
            logger.exception("%s live metrics stage failed.", model.label)
            live = {"error": f"{model.label} live metrics could not be computed (see analyzer.log)"}
            failed = True
        try:
            backtest = compute_backtest_metrics(model.backtest_csv)
        except Exception:
            logger.exception("%s backtest metrics stage failed.", model.label)
            backtest = None
            failed = True
        samples[model.key] = {"live": live, "backtest": backtest}

    # Stage 2 — assemble the report (or hand off for narrative generation).
    # The narrative gate looks at the COMBINED closed-trade count across all
    # models: the report covers all of them, so the question is whether there
    # is enough evidence overall to be worth a strategic narrative.
    closed = sum(s["live"].get("closed_trades", 0) for s in samples.values())
    if args.narrative:
        try:
            narrative = load_narrative(args.narrative)
        except Exception:
            logger.exception("Could not read narrative file %s.", args.narrative)
            return EXIT_FAILED
        logger.info("Using externally generated narrative from %s.", args.narrative)
        report_md = narrative + _data_appendix(samples)
    elif closed < args.min_closed:
        per_model = ", ".join(
            f"{m.label} {sample['live'].get('closed_trades', 0)}"
            for m, sample in _ordered(samples)
        )
        gate_note = (
            f"Only **{closed}** live paper trades are closed across all models "
            f"({per_model}) — below the {args.min_closed}-trade minimum for meaningful "
            f"strategy analysis, so the strategy narrative was skipped (this is the "
            f"expected path early on). The computed aggregates for each model are below."
        )
        logger.info("Sample gate: %d closed < %d — deterministic report.",
                    closed, args.min_closed)
        report_md = build_deterministic_report(samples, gate_note)
    elif args.no_llm:
        report_md = build_deterministic_report(
            samples, "Narrative disabled via --no-llm; metrics only.")
    else:
        # Phase 1 handoff: emit the request and stop — no archive/email yet.
        emit_narrative_request(samples)
        return EXIT_NARRATIVE_REQUESTED

    # Stage 3 — archive whatever was produced
    try:
        report_path = archive_report(report_md)
    except Exception:
        logger.exception("Archive stage failed.")
        return EXIT_FAILED

    # Stage 4 — email
    if args.no_email:
        logger.info("--no-email: skipping email stage.")
    else:
        try:
            if email_report(report_path, report_md):
                logger.info("Email sent.")
            else:
                logger.error("Email stage reported failure.")
                failed = True
        except Exception:
            logger.exception("Email stage failed.")
            failed = True

    logger.info("=== daily_tradein_analyzer done (failed=%s) ===", failed)
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
