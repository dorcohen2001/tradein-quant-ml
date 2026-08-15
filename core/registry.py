"""Single source of truth for which models exist and where their files live.

Before this module the set of models was encoded in at least six places --
Livne's config carried Biton's store/CSV paths, Livne's runner hard-coded a
subprocess call to one named sibling, Steve held a `MODELS` tuple plus a
separate `CLOSED_WITH_PNL` dict, each model's funnel exporter wrote its own
filename prefix as a literal, and the chart test harness had a hard-coded
`--model` choice list. Adding a third model meant finding all of them.

Now every one of those derives from an entry here.

WHY THIS IS ALLOWED TO LIVE IN `core`
-------------------------------------
`core` must never import from `models.*` (see `core/__init__.py`), and this
module does not. It holds only *metadata about* models -- strings, numbers,
and paths computed from a key. It imports nothing from a model, executes no
strategy code, and contains no trading threshold. A model's `config.py` reads
its own entry from here; the arrow still points models -> core.

PATHS ARE DERIVED, NOT DECLARED
-------------------------------
Every runtime path is `<artifact>_<key>.<ext>`. That was already the de-facto
convention -- `trades_livne.json`, `report_state_biton.json`,
`trade_journal_livne.xlsx`, `live_funnel_biton_*.xlsx` -- so deriving them
from the key reproduces the existing filenames EXACTLY. This is load-bearing:
these files hold live paper-trading state (133 Livne + 36 Biton trades at the
time of writing), and a path that changed by even one character would silently
orphan that history and start an empty store. `tests/test_registry.py` pins
the derived values against the literals the models used before this refactor.

STATUS VOCABULARIES ARE PER-MODEL AND NOT INTERCHANGEABLE
---------------------------------------------------------
The two models run genuinely different lifecycles, and conflating them
silently corrupts every downstream metric (this has already happened once:
Biton's status set was applied to both, dropping every Livne LOSS_SL_BE and
EMERGENCY_EXIT trade from win rate, profit factor and drawdown). Three
distinct sets are therefore tracked separately:

    closed_statuses    terminal, for the daily/weekly Telegram summary.
                       INCLUDES EXPIRED -- a signal that never filled is still
                       worth reporting as closed out.
    update_statuses    non-terminal changes still worth reporting (Livne's
                       PARTIAL_TP1 banks half the position). Empty for Biton,
                       which is single-shot.
    closed_with_pnl    terminal AND carrying a realized `pnl_pct`, for Steve's
                       metrics. EXCLUDES EXPIRED: it never entered, so it has
                       no P&L and would drag every average toward zero.

To register a third model, add one `ModelConfig` to `_MODELS` below and create
`models/<key>/`. Nothing else needs to change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIR = os.path.join(REPO_ROOT, "runtime")
MODELS_DIR = os.path.join(REPO_ROOT, "models")


@dataclass(frozen=True)
class ModelConfig:
    """One model's identity, cadence, lifecycle vocabulary and derived paths.

    Frozen: this is reference data read by several processes, and a module that
    let a caller mutate another model's record would reintroduce exactly the
    cross-contamination it exists to remove.
    """

    key: str                                # directory name, e.g. "livne"
    label: str                              # human name used in alerts/reports
    cycle_interval_minutes: float           # how often this model's own loop runs
    closed_statuses: tuple[str, ...]
    update_statuses: tuple[str, ...]
    closed_with_pnl: tuple[str, ...]
    description: str = ""
    # Which entry in `core.settings.TELEGRAM_ROUTES` this model's messages go
    # to. "default" is the historical single-destination behaviour, so an
    # unconfigured deployment keeps sending exactly where it always did.
    telegram_route: str = "default"
    # Bounds a supervisor-launched cycle for this model. A hung child is not an
    # exception, so no try/except can rescue it -- only a timeout can.
    phase_timeout_seconds: int = 480

    # --- filesystem layout ------------------------------------------------
    @property
    def model_dir(self) -> str:
        return os.path.join(MODELS_DIR, self.key)

    @property
    def live_runner(self) -> str:
        return os.path.join(self.model_dir, "live_runner.py")

    @property
    def exists(self) -> bool:
        """Whether this model's code is actually present on disk. The
        supervisor checks this rather than assuming: a registered-but-missing
        model must be skipped with a clear message, not crash the run."""
        return os.path.isfile(self.live_runner)

    # --- runtime artifacts (all derived from `key`) -----------------------
    @property
    def trade_store(self) -> str:
        return os.path.join(RUNTIME_DIR, "stores", f"trades_{self.key}.json")

    @property
    def report_state_store(self) -> str:
        return os.path.join(RUNTIME_DIR, "stores", f"report_state_{self.key}.json")

    @property
    def journal_xlsx(self) -> str:
        return os.path.join(RUNTIME_DIR, "journals", f"trade_journal_{self.key}.xlsx")

    @property
    def backtest_csv(self) -> str:
        return os.path.join(RUNTIME_DIR, "backtest", f"trades_log_{self.key}.csv")

    @property
    def funnel_prefix(self) -> str:
        """Filename prefix for this model's funnel reports. They share one
        directory, and retention prunes per prefix, so this is what stops one
        model evicting another's diagnostics."""
        return f"live_funnel_{self.key}_"

    @property
    def chart_prefix(self) -> str:
        """Rendered charts share `runtime/charts/`. Including the key stops two
        models that alert on the same ticker on the same day from writing the
        same path -- harmless while Biton ran as a sequential subprocess, a real
        race once models run concurrently under the supervisor."""
        return f"{self.key}_"


_MODELS: tuple[ModelConfig, ...] = (
    ModelConfig(
        key="livne",
        label="Livne model",
        description="Current multi-timeframe (MTFA) intraday pipeline; multi-tier TP1/breakeven/TP2 lifecycle.",
        cycle_interval_minutes=2,
        closed_statuses=("WIN_TP2", "LOSS_SL", "LOSS_SL_BE", "EMERGENCY_EXIT", "EXPIRED"),
        update_statuses=("PARTIAL_TP1",),
        closed_with_pnl=("WIN_TP2", "LOSS_SL", "LOSS_SL_BE", "EMERGENCY_EXIT"),
        telegram_route="livne",
    ),
    ModelConfig(
        key="biton",
        label="Biton model",
        description="Frozen legacy single-timeframe daily pipeline; single-shot TP.",
        cycle_interval_minutes=15,
        closed_statuses=("WIN_TP1", "WIN_TP2", "LOSS_SL", "EXPIRED"),
        update_statuses=(),
        closed_with_pnl=("WIN_TP1", "WIN_TP2", "LOSS_SL"),
        telegram_route="biton",
    ),
)

_BY_KEY: dict[str, ModelConfig] = {m.key: m for m in _MODELS}


def all_models() -> tuple[ModelConfig, ...]:
    """Every registered model, in canonical (report) order."""
    return _MODELS


def model_keys() -> tuple[str, ...]:
    return tuple(m.key for m in _MODELS)


def get_model(key: str) -> ModelConfig:
    """Look up one model. Raises KeyError listing the valid keys -- a typo'd
    model name should fail loudly at startup, not silently resolve to an empty
    trade store that looks like "this model has simply never traded"."""
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"unknown model {key!r}; registered models are {', '.join(model_keys())}"
        ) from None


def available_models() -> tuple[ModelConfig, ...]:
    """Registered models whose code is actually present on disk."""
    return tuple(m for m in _MODELS if m.exists)
