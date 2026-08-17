"""Test infrastructure shipped WITH the platform, not with the tests.

It lives in `core/` on purpose. The conformance battery has to run inside a
model's own directory -- each model is its own sys.path root, and two models
cannot be imported into one interpreter because they both define top-level
`config`/`utils`/`strategy` modules. A harness that lived under `tests/` would
not be importable from there.

    from core.testing import FakeMarket, scripted_series
    from core.testing.conformance import run_conformance   # imported directly:
                                                           # it is also run as
                                                           # `python -m`, and
                                                           # eager re-export
                                                           # would double-load it
"""
from core.testing.fake_market import FakeMarket, scripted_series

__all__ = ["FakeMarket", "scripted_series"]
