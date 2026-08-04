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

import os

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

# --- News / sentiment ---------------------------------------------------
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")
NEWS_LOOKBACK_HOURS = _int_env("NEWS_LOOKBACK_HOURS", 48)
