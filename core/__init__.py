"""Shared, model-agnostic infrastructure used by BOTH trading models.

Hard rule for this package: `core` contains **no strategy logic and no
tunable trading thresholds**. Only I/O adapters, caching, notification
transport, and chart rendering live here. Anything that decides *whether* or
*how* to trade belongs to a model (`models/livne`, `models/biton`), because
the two models are deliberately allowed to disagree.

Consequently `core` must never import from `models.*` or from a model's
top-level `config` -- the dependency arrow points one way only:

    models/livne  ->  core
    models/biton  ->  core

The one exception is `core.charting.test_chart_alert`, a manual test harness
(not library code) which selects a model at runtime via `--model`.

Both models run as fully isolated OS processes with their own working
directory, so `core` is reached by putting the repository root on `sys.path`
-- see each model's `_bootstrap.py`.
"""
