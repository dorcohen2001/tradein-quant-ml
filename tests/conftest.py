"""Shared pytest configuration.

Puts the repository root on `sys.path` so tests import `core.*` the same way
the models do at runtime (via each model's `_bootstrap`). Tests deliberately
import ONLY from `core`, `scripts` and the repo root -- never from a model
package. Two reasons:

  1. The models define clashing top-level module names (`config`, `utils`,
     `paper_trading`), so importing both into one pytest process is impossible
     by construction.
  2. Importing a model's `config` pulls in its watchlists and network-facing
     data layer, which a unit test has no business touching.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# On Windows in non-interactive / agent / IDE environments without standard console handles,
# DuplicateHandle fails on invalid GetStdHandle(STD_*_HANDLE).
# Providing valid handles (DEVNULL / PIPE) prevents [WinError 6].
if sys.platform == "win32":
    import subprocess
    _orig_popen_init = subprocess.Popen.__init__

    def _safe_popen_init(self, *args, **kwargs):
        if kwargs.get("stdin") is None:
            kwargs["stdin"] = subprocess.DEVNULL
        if kwargs.get("stdout") is None:
            kwargs["stdout"] = subprocess.DEVNULL
        if kwargs.get("stderr") is None:
            kwargs["stderr"] = subprocess.DEVNULL
        return _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _safe_popen_init
