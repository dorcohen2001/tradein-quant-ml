"""Daily Paper-Trading Analyzer, Report Generator & Email Dispatcher ("Steve").

No external LLM API is called and no API key is required. The strategic
narrative is produced by a *local* LLM operator (Claude Code running on this
machine) through a two-phase handoff:

  Phase 1 (analyze + request):
      python daily_tradein_analyzer.py
      - Computes aggregate metrics from paper_trading/trades.json (live) and
        backtest_suite/trades_log.csv (simulated), in Python.
      - If at least --min-closed live trades are closed, prints the report
        instructions + compact metrics JSON, writes them to llm_request.json
        next to this script, and exits with code 3 ("narrative requested").
        Nothing is archived or emailed yet.
      - Below the gate (or with --no-llm) it skips the narrative and goes
        straight to archive + email with a deterministic metrics report.

  Phase 2 (deliver):
      python daily_tradein_analyzer.py --narrative path/to/narrative.md
      - Takes the externally generated Markdown narrative, appends the
        deterministic data appendix, archives the report as
        MailToMail/Scripts/report_YYYY-MM-DD.md (overwrite; idempotent), and
        emails it (subject "TRADEIN Daily Report — YYYY-MM-DD", .md attached,
        executive summary in the body).

Other usage:
    python daily_tradein_analyzer.py --no-email      # skip the email stage
    python daily_tradein_analyzer.py --no-llm        # deterministic report, full delivery
    python daily_tradein_analyzer.py --min-closed 5  # override the sample gate

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

# This script lives in Agents/Steve - Daily Analyst/, alongside the trading
# system's own package at TRADEIN/TRADEIN/ (which owns config.py,
# paper_trading/, etc.). Add that package directory to sys.path so the
# imports below resolve regardless of the caller's cwd — mirrors how
# config.py itself anchors every path to its own __file__ rather than to
# whoever imports it.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRADEIN_PACKAGE_DIR = os.path.normpath(os.path.join(_SCRIPT_DIR, "..", "..", "TRADEIN"))
if _TRADEIN_PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _TRADEIN_PACKAGE_DIR)

from config import (
    BACKTEST_TRADES_CSV,
    MAILTOMAIL_DIR,
    MIN_CLOSED_TRADES_FOR_LLM,
    REPORT_ARCHIVE_DIR,
)
from paper_trading.tracker import get_all_trades

LOG_PATH = os.path.join(_SCRIPT_DIR, "analyzer.log")
LLM_REQUEST_PATH = os.path.join(_SCRIPT_DIR, "llm_request.json")

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

CLOSED_WITH_PNL = ("WIN_TP1", "WIN_TP2", "LOSS_SL")
WIN_STATUSES = ("WIN_TP1", "WIN_TP2")

NARRATIVE_INSTRUCTIONS = """\
You are a senior trading-strategy analyst reviewing a systematic paper-trading
program. Below is a JSON payload of pre-computed performance aggregates for
two samples: "live" (real-time paper trades) and "backtest" (historical
simulation of the exact same signal logic; note its entry-fill assumption is
slightly optimistic).

Write a daily strategy report in Markdown with exactly this structure:
# TRADEIN Daily Report — {date}
## Executive Summary  (3-5 sentences, blunt, decision-oriented)
## Performance Snapshot  (key numbers from BOTH samples, clearly labeled live vs. simulated)
## Weaknesses & Loss Patterns  (grounded ONLY in the supplied numbers)
## Strategic Proposals  (ranked by expected impact; bold high-level ideas —
   new indicator families, risk-management frameworks, macro filters, revised
   entry/exit logic, position sizing — each tied to a specific weakness above)
## Concrete Next Actions  (numbered, testable steps)

Hard rules:
- STRICTLY NO CODE: no code blocks, no snippets, no syntax. Plain prose only.
- Use ONLY numbers present in the payload. If a metric is null or absent, say
  it could not be computed — never invent a value.
- If the live sample is small, say so and weight the backtest sample instead.
- Keep it under ~900 words.

When done, save the report as a Markdown file and deliver it with:
    python daily_tradein_analyzer.py --narrative <path-to-file.md>"""


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


def compute_live_metrics() -> dict:
    trades = get_all_trades()
    by_status: dict[str, int] = {}
    for t in trades:
        by_status[t["current_status"]] = by_status.get(t["current_status"], 0) + 1

    closed = [t for t in trades
              if t["current_status"] in CLOSED_WITH_PNL and t.get("pnl_pct") is not None]
    closed.sort(key=lambda t: t.get("exit_date") or t["signal_date"])
    rows = [{"pnl": float(t["pnl_pct"]), "ticker": t["ticker"],
             "asset_class": t["asset_class"], "direction": t["direction"]} for t in closed]
    pnls = [r["pnl"] for r in rows]

    metrics = {
        "sample": "live paper trades (trades.json)",
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


def compute_backtest_metrics() -> dict | None:
    if not os.path.exists(BACKTEST_TRADES_CSV):
        logger.info("No backtest CSV at %s — skipping backtest sample.", BACKTEST_TRADES_CSV)
        return None
    rows = []
    with open(BACKTEST_TRADES_CSV, newline="", encoding="utf-8") as f:
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
        "sample": "simulated backtest signals (trades_log.csv; entry fill assumed at "
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


def _data_appendix(live: dict, backtest: dict | None) -> str:
    return (
        "\n\n---\n\n## Data Appendix (computed, not narrative)\n\n"
        + _metrics_markdown("Live paper trades", live)
        + "\n" + _metrics_markdown("Backtest (simulated)", backtest)
    )


def build_deterministic_report(live: dict, backtest: dict | None, gate_note: str) -> str:
    today = dt.date.today().isoformat()
    return (
        f"# TRADEIN Daily Report — {today}\n\n"
        f"## Executive Summary\n\n{gate_note}\n\n"
        f"## Performance Snapshot\n\n"
        + _metrics_markdown("Live paper trades", live)
        + "\n" + _metrics_markdown("Backtest (simulated)", backtest)
    )


def emit_narrative_request(live: dict, backtest: dict | None) -> None:
    """Phase 1 handoff: print + persist the instructions and metrics payload
    for the local LLM operator (Claude Code). No API call is made."""
    payload = {
        "report_date": dt.date.today().isoformat(),
        "live": live,
        "backtest": backtest,
    }
    request = {"instructions": NARRATIVE_INSTRUCTIONS, "payload": payload}
    with open(LLM_REQUEST_PATH, "w", encoding="utf-8") as f:
        json.dump(request, f, indent=2)

    print("=" * 70)
    print("NARRATIVE REQUESTED — local LLM generation required (no API call made)")
    print(f"Request also written to: {LLM_REQUEST_PATH}")
    print("=" * 70)
    print("\n--- INSTRUCTIONS " + "-" * 52)
    print(NARRATIVE_INSTRUCTIONS)
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
    if MAILTOMAIL_DIR not in sys.path:
        sys.path.append(MAILTOMAIL_DIR)
    from send_email import send_report_email

    subject = f"TRADEIN Daily Report — {dt.date.today().isoformat()}"
    body = (_executive_summary(report_md)
            + "\n\n(Full report attached; archived copy in MailToMail/Scripts.)")
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

    # Stage 1 — analyze
    try:
        live = compute_live_metrics()
        logger.info("Live metrics: %d signals, %d closed.",
                    live["total_signals"], live["closed_trades"])
    except Exception:
        logger.exception("Live metrics stage failed.")
        live = {"error": "live metrics could not be computed (see analyzer.log)"}
        failed = True
    try:
        backtest = compute_backtest_metrics()
    except Exception:
        logger.exception("Backtest metrics stage failed.")
        backtest = None
        failed = True

    # Stage 2 — assemble the report (or hand off for narrative generation)
    closed = live.get("closed_trades", 0)
    if args.narrative:
        try:
            narrative = load_narrative(args.narrative)
        except Exception:
            logger.exception("Could not read narrative file %s.", args.narrative)
            return EXIT_FAILED
        logger.info("Using externally generated narrative from %s.", args.narrative)
        report_md = narrative + _data_appendix(live, backtest)
    elif closed < args.min_closed:
        gate_note = (
            f"Only **{closed}** live paper trades are closed — below the "
            f"{args.min_closed}-trade minimum for meaningful strategy analysis, so the "
            f"strategy narrative was skipped (this is the expected path early on). "
            f"The computed aggregates for both samples are below."
        )
        logger.info("Sample gate: %d closed < %d — deterministic report.",
                    closed, args.min_closed)
        report_md = build_deterministic_report(live, backtest, gate_note)
    elif args.no_llm:
        report_md = build_deterministic_report(
            live, backtest, "Narrative disabled via --no-llm; metrics only.")
    else:
        # Phase 1 handoff: emit the request and stop — no archive/email yet.
        emit_narrative_request(live, backtest)
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
