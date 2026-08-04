"""Put the repository root on `sys.path` so `core.*` is importable.

Identical in purpose to `models/livne/_bootstrap.py` -- see that file for the
full rationale. Duplicated deliberately rather than shared: this file is the
very thing that makes `core` reachable, so it cannot itself live in `core`,
and each model must stay independently runnable from its own directory.

    import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*
    from core.utils.telegram_notifier import send_telegram_message
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

if REPO_ROOT not in sys.path:
    sys.path.append(REPO_ROOT)
