"""Put the repository root on `sys.path` so `core.*` is importable.

This model runs as its own OS process with `models/livne/` as the working
directory (that is what makes `config`, `ta.mtfa`, `screener.*` resolve as
top-level modules, and it is what keeps the two models' import namespaces
completely separate). The trade-off is that the repo root -- where `core/`
lives -- is NOT on `sys.path` by default.

Rather than requiring a `pip install -e .`, a `.pth` file, or a PYTHONPATH
environment variable that a Task Scheduler entry could silently lose, every
module that imports `core.*` imports this first:

    import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*
    from core.utils.telegram_notifier import send_telegram_message

Import it as a plain `import` statement placed ABOVE the `from core...`
imports, so the path is registered before `core` is resolved. Importing it
more than once is free (module caching) and appending is idempotent.

The repo root is APPENDED, not prepended, so this model's own modules always
win a name clash against anything at the repository root.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

if REPO_ROOT not in sys.path:
    sys.path.append(REPO_ROOT)
