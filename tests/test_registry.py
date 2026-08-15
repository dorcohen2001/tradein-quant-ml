"""Registry resolution and path derivation.

The most important tests in this file are the ones pinning derived paths
against the LITERALS the models used before the registry existed. Those files
hold live paper-trading state; a path that changes by one character does not
raise, it silently starts an empty store and orphans the history. The literals
below are therefore written out by hand on purpose -- deriving them from the
registry would make the test tautological.
"""
from __future__ import annotations

import os

import pytest

from core.registry import ModelConfig, all_models, available_models, get_model, model_keys


# Exactly what each model's config.py contained before Phase 1.
PRE_REFACTOR = {
    "livne": {
        "trade_store": os.path.join("runtime", "stores", "trades_livne.json"),
        "report_state_store": os.path.join("runtime", "stores", "report_state_livne.json"),
        "journal_xlsx": os.path.join("runtime", "journals", "trade_journal_livne.xlsx"),
        "backtest_csv": os.path.join("runtime", "backtest", "trades_log_livne.csv"),
        "funnel_prefix": "live_funnel_livne_",
        "label": "Livne model",
        "cycle_interval_minutes": 2,
        "closed_statuses": ("WIN_TP2", "LOSS_SL", "LOSS_SL_BE", "EMERGENCY_EXIT", "EXPIRED"),
        "update_statuses": ("PARTIAL_TP1",),
        "closed_with_pnl": ("WIN_TP2", "LOSS_SL", "LOSS_SL_BE", "EMERGENCY_EXIT"),
    },
    "biton": {
        "trade_store": os.path.join("runtime", "stores", "trades_biton.json"),
        "report_state_store": os.path.join("runtime", "stores", "report_state_biton.json"),
        "journal_xlsx": os.path.join("runtime", "journals", "trade_journal_biton.xlsx"),
        "backtest_csv": os.path.join("runtime", "backtest", "trades_log_biton.csv"),
        "funnel_prefix": "live_funnel_biton_",
        "label": "Biton model",
        "cycle_interval_minutes": 15,
        "closed_statuses": ("WIN_TP1", "WIN_TP2", "LOSS_SL", "EXPIRED"),
        "update_statuses": (),
        "closed_with_pnl": ("WIN_TP1", "WIN_TP2", "LOSS_SL"),
    },
}


@pytest.mark.parametrize("key", sorted(PRE_REFACTOR))
def test_derived_paths_match_pre_refactor_literals(key):
    """A changed path silently orphans live trade history -- pin every one."""
    model = get_model(key)
    expected = PRE_REFACTOR[key]
    for attr in ("trade_store", "report_state_store", "journal_xlsx", "backtest_csv"):
        assert getattr(model, attr).endswith(expected[attr]), attr


@pytest.mark.parametrize("key", sorted(PRE_REFACTOR))
def test_identity_and_lifecycle_match_pre_refactor(key):
    model = get_model(key)
    expected = PRE_REFACTOR[key]
    assert model.label == expected["label"]
    assert model.cycle_interval_minutes == expected["cycle_interval_minutes"]
    assert model.closed_statuses == expected["closed_statuses"]
    assert model.update_statuses == expected["update_statuses"]
    assert model.closed_with_pnl == expected["closed_with_pnl"]
    assert model.funnel_prefix == expected["funnel_prefix"]


def test_expired_excluded_from_pnl_statuses_but_present_in_closed():
    """EXPIRED never entered, so it has no P&L and must not drag the averages
    -- but it IS reportable as closed. Conflating the two sets is precisely the
    bug that once zeroed out Livne's LOSS_SL_BE and EMERGENCY_EXIT trades."""
    for model in all_models():
        assert "EXPIRED" in model.closed_statuses
        assert "EXPIRED" not in model.closed_with_pnl


def test_status_vocabularies_are_not_interchangeable():
    """Livne never emits WIN_TP1; Biton never emits PARTIAL_TP1. If these ever
    become equal, a shared constant has crept back in."""
    livne, biton = get_model("livne"), get_model("biton")
    assert "WIN_TP1" not in livne.closed_with_pnl
    assert "PARTIAL_TP1" in livne.update_statuses
    assert "WIN_TP1" in biton.closed_with_pnl
    assert "PARTIAL_TP1" not in biton.update_statuses


def test_keys_are_unique_and_ordered():
    keys = model_keys()
    assert len(keys) == len(set(keys))
    assert keys == tuple(m.key for m in all_models())


def test_unknown_model_raises_listing_valid_keys():
    """A typo'd key must fail loudly at startup, not resolve to an empty store
    that reads as 'this model has never traded'."""
    with pytest.raises(KeyError) as excinfo:
        get_model("livne_typo")
    message = str(excinfo.value)
    assert "livne" in message and "biton" in message


def test_registered_models_exist_on_disk():
    assert {m.key for m in available_models()} == set(model_keys())


def test_model_config_is_frozen():
    """Reference data shared across processes: a mutable record would let one
    model's code alter another's paths at runtime."""
    with pytest.raises(Exception):
        get_model("livne").key = "hacked"


def test_new_model_needs_no_path_declarations():
    """The registration contract: a key plus metadata yields every path."""
    fresh = ModelConfig(
        key="demo", label="Demo model", cycle_interval_minutes=5,
        closed_statuses=("WIN", "EXPIRED"), update_statuses=(), closed_with_pnl=("WIN",),
    )
    assert fresh.trade_store.endswith(os.path.join("stores", "trades_demo.json"))
    assert fresh.journal_xlsx.endswith(os.path.join("journals", "trade_journal_demo.xlsx"))
    assert fresh.funnel_prefix == "live_funnel_demo_"
    assert fresh.chart_prefix == "demo_"
    assert fresh.telegram_route == "default"  # inherits the shared chat until configured
    assert fresh.exists is False  # models/demo/ does not exist


def test_chart_prefixes_are_distinct_per_model():
    """Charts share one directory; identical prefixes would let concurrent
    models overwrite each other's PNGs."""
    prefixes = [m.chart_prefix for m in all_models()]
    assert len(prefixes) == len(set(prefixes))
