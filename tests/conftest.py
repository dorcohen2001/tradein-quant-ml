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
