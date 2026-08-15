"""Shared settings for `core`, read straight from the environment.

`core` must not import a model's `config` -- the dependency arrow points
models -> core, never back (see `core/__init__.py`). But two core modules do
need configuration: the Telegram transport needs bot credentials, and the
news engine needs an optional NewsAPI key and a lookback window.

Everything here is genuinely model-agnostic: both models always used byte-
identical values for these (same bot, same chat, same 48h lookback, same 10s
timeout), which is precisely why these modules were safe to deduplicate into
`core` in the first place. Anything the two models are allowed to DISAGREE
about stays in their own `config.py`.

The repo-root `.env` is loaded here explicitly rather than relying on a
model's `config` having already called `load_dotenv()` -- that would make
correctness depend on module import order.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# override=False: a value already exported in the real environment wins over
# the .env file, which is what lets a scheduled task or CI inject its own.
load_dotenv(os.path.join(REPO_ROOT, ".env"), override=False)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --- HTTP ---------------------------------------------------------------
REQUEST_TIMEOUT = _int_env("REQUEST_TIMEOUT", 10)

# --- Telegram -----------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# How many times a send is attempted before it is reported as failed, and the
# base for the exponential backoff between attempts (1s, 2s, 4s). Previously
# there was no retry at all: a single connection blip or a 429 silently
# dropped the alert, printed one line to stderr, and the trade went unreported.
TELEGRAM_MAX_ATTEMPTS = _int_env("TELEGRAM_MAX_ATTEMPTS", 3)
TELEGRAM_BACKOFF_BASE_SECONDS = float(os.environ.get("TELEGRAM_BACKOFF_BASE_SECONDS", "") or 1.0)

# --- Telegram routing ---------------------------------------------------
# Every message used to go to exactly one hard-coded chat: the token and chat
# id were read once at import and baked into the payload, and the send
# functions had no parameter for either. Routing more than one model, or the
# same model to more than one group, was impossible without editing code.
#
# A route is now a NAME that resolves to a (token, chat_id) pair, and each
# model declares its route in `core.registry`. Resolution order, first hit
# wins, per field:
#
#   1. the TELEGRAM_ROUTES JSON blob, e.g.
#        TELEGRAM_ROUTES={"livne":{"chat_id":"-100123"},"biton":{"chat_id":"-100456","token":"..."}}
#   2. per-route environment variables: TELEGRAM_CHAT_ID_<NAME>,
#      TELEGRAM_BOT_TOKEN_<NAME>  (e.g. TELEGRAM_CHAT_ID_LIVNE)
#   3. the shared defaults above.
#
# Step 3 is what makes this backward-compatible: with nothing new configured
# -- which is the current deployment -- EVERY route resolves to exactly the
# destination everything already used, so behaviour is unchanged until someone
# opts in. A route may override only the chat and inherit the bot, which is
# the common case of one bot posting into several groups.
TELEGRAM_ROUTES_JSON = os.environ.get("TELEGRAM_ROUTES", "")


def _parse_routes(raw: str) -> dict[str, dict]:
    """Never fatal. A malformed TELEGRAM_ROUTES must not take the alerting path
    down with it -- it degrades to "no JSON routes configured", and every route
    then falls through to per-route env vars and the shared default."""
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[settings] TELEGRAM_ROUTES is not valid JSON ({exc}); ignoring it "
              f"and falling back to per-route env vars / defaults.", file=sys.stderr)
        return {}
    if not isinstance(parsed, dict):
        print("[settings] TELEGRAM_ROUTES must be a JSON object; ignoring it.", file=sys.stderr)
        return {}
    return {str(k): v for k, v in parsed.items() if isinstance(v, dict)}


_ROUTES = _parse_routes(TELEGRAM_ROUTES_JSON)

DEFAULT_TELEGRAM_ROUTE = "default"


@dataclass(frozen=True)
class TelegramRoute:
    """A resolved send destination."""

    name: str
    token: str
    chat_id: str
    # True when this route was resolved with fallback DISABLED. Carried so
    # diagnostics can distinguish "misconfigured" from "deliberately silent".
    strict: bool = False

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)


def route_env_names(name: str) -> tuple[str, str]:
    """The (token, chat_id) environment variable names for a route.

    Exposed so the onboarding guide and the scaffolder print exactly what the
    resolver reads, rather than a second copy of the convention that can drift
    from it.
    """
    suffix = name.strip().upper().replace("-", "_").replace(" ", "_")
    return (f"TELEGRAM_ROUTE_{suffix}_TOKEN", f"TELEGRAM_ROUTE_{suffix}_CHAT_ID")


def get_telegram_route(name: str | None = None, strict: bool = False) -> TelegramRoute:
    """Resolve a route name to credentials, per the order documented above.

    Resolved at CALL time, not import time, so a test (or a supervisor that
    sets per-child environment) can change the destination without reimporting
    the module.

    `strict=True` REMOVES the shared-default fallback: a route with no
    configuration of its own resolves to empty credentials, and the notifier
    then skips the send instead of delivering it. That is the isolation
    guarantee for client strategies -- with the normal forgiving behaviour, one
    unset environment variable would publish a client's signals straight into
    the primary production channel. Silence is recoverable; a leak is not.
    """
    name = name or DEFAULT_TELEGRAM_ROUTE
    entry = _ROUTES.get(name, {})
    token_var, chat_var = route_env_names(name)
    suffix = name.strip().upper().replace("-", "_").replace(" ", "_")

    token = (
        str(entry.get("token") or "")
        or os.environ.get(token_var, "")
        # Legacy form, kept working so existing deployments are unaffected.
        or os.environ.get(f"TELEGRAM_BOT_TOKEN_{suffix}", "")
    )
    chat_id = (
        str(entry.get("chat_id") or "")
        or os.environ.get(chat_var, "")
        or os.environ.get(f"TELEGRAM_CHAT_ID_{suffix}", "")
    )

    if not strict:
        token = token or TELEGRAM_BOT_TOKEN
        chat_id = chat_id or TELEGRAM_CHAT_ID

    return TelegramRoute(name=name, token=token, chat_id=chat_id, strict=strict)


def configured_route_names() -> tuple[str, ...]:
    """Route names with an EXPLICIT configuration of their own (JSON entry or
    per-route env var) -- i.e. those that do not simply inherit the default.
    Used by diagnostics to show where messages will actually land."""
    names = set(_ROUTES)
    for key in os.environ:
        for prefix in ("TELEGRAM_CHAT_ID_", "TELEGRAM_BOT_TOKEN_"):
            if key.startswith(prefix):
                names.add(key[len(prefix):].lower())
    return tuple(sorted(names))

# --- News / sentiment ---------------------------------------------------
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")
NEWS_LOOKBACK_HOURS = _int_env("NEWS_LOOKBACK_HOURS", 48)

# --- Runtime artifact retention (core.maintenance) ----------------------
# Both of these directories previously grew without any bound. Ages, not
# counts: they are content-keyed caches, so "how old" is the meaningful axis.
#
# 7 days for the yfinance cache is deliberately more than double the 72h
# YF_STALE_FALLBACK_SECONDS window past which even the emergency stale-serve
# path refuses an entry -- so nothing still reachable is ever pruned.
# 0 disables a sweep entirely.
RUNTIME_CACHE_RETENTION_DAYS = _int_env("RUNTIME_CACHE_RETENTION_DAYS", 7)
CHART_RETENTION_DAYS = _int_env("CHART_RETENTION_DAYS", 14)
# How often the supervisor runs the sweep. Daily is plenty for directories
# that accumulate over weeks.
MAINTENANCE_INTERVAL_MINUTES = _int_env("MAINTENANCE_INTERVAL_MINUTES", 1440)

# --- Daily analyzer ("Steve", agents/steve) -----------------------------
# These belong to the ANALYZER, not to any model. They previously lived in
# models/livne/config.py, which made one model's config the carrier for a tool
# that reports on ALL models -- so Steve could not run if Livne were ever
# renamed or retired, and a third model inherited the same asymmetry. They are
# model-agnostic by definition, so `core` is where they belong.
MIN_CLOSED_TRADES_FOR_LLM = _int_env("MIN_CLOSED_TRADES_FOR_LLM", 20)
REPORT_ARCHIVE_DIR = os.path.join(REPO_ROOT, "mail", "reports")
MAIL_DIR = os.path.join(REPO_ROOT, "mail")
ANALYZER_LOG_PATH = os.path.join(REPO_ROOT, "runtime", "logs", "analyzer.log")
LLM_REQUEST_PATH = os.path.join(REPO_ROOT, "runtime", "logs", "llm_request.json")
