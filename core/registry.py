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

STAGES AND CLIENT MODELS
------------------------
`stage` separates "is running" from "is reported on". Client strategies are
scaffolded by `scripts/create_client_model.py` as `client_<key>`, land here as
`stage="incubating"`, and are appended between the CLIENT MODELS markers at the
end of `_MODELS` so the generator can add and remove them without ever touching
the first-party entries above.

Their `dedicated_route` defaults to False -- the platform default is the shared
primary hub, with every alert name-prefixed. `--dedicated-route` opts a tenant
into its own channel AND makes its route strict, so a misconfigured dedicated
tenant sends nothing rather than leaking into the hub. That flag is the only
thing that makes a client's signals private; assuming otherwise is the mistake
this paragraph exists to prevent.

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
    # Whether this model participates in scheduled execution and reporting.
    #
    # Deactivating is deliberately a FLAG rather than deleting the entry: the
    # record stays, so every path below still resolves, the model's own
    # config.py still imports cleanly, its history stays readable, and
    # re-enabling is a one-word edit. Removing the entry instead would orphan
    # runtime/stores/trades_<key>.json behind a KeyError.
    #
    # Disabling affects ONLY what runs and what gets reported from here on.
    # It never touches stored state: trades, journals, funnel exports and
    # backtest logs are all left exactly as they are.
    enabled: bool = True

    # Lifecycle stage. Controls VISIBILITY, never execution -- the supervisor
    # runs every enabled model regardless of stage, because an incubating
    # strategy that is not actually traded produces no evidence to graduate on.
    #
    #   incubating  runs on its own interval, alerts to its own route, and is
    #               EXCLUDED from the executive report unless asked for
    #               (`--include-incubating`). A client trialling a strategy
    #               must not silently move the headline numbers the primary
    #               models are judged by.
    #   production  fully integrated: executive report, consolidated
    #               dashboards, central oversight.
    #   deprecated  retained for its history; reported only on request. Prefer
    #               `enabled=False` to actually stop it.
    #
    # Defaults to "incubating" so a newly scaffolded model cannot land in the
    # executive report by omission -- the safe direction. The two live models
    # therefore declare "production" EXPLICITLY below.
    stage: str = "incubating"

    # When True, this model NEVER falls back to the shared default Telegram
    # chat. An unconfigured route means its sends are skipped entirely rather
    # than being delivered to the primary channel.
    #
    # This is the blast-radius guarantee for client work. Route resolution is
    # otherwise deliberately forgiving (unset -> shared default), which is
    # right for first-party models and exactly wrong for a client: a typo in
    # one env var would publish that client's signals into the main group.
    dedicated_route: bool = False
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
        # Explicit, not inherited: the field defaults to "incubating" so new
        # models stay out of the executive report by default, which would
        # silently drop this one out of it.
        stage="production",
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
        stage="production",  # explicit -- see the Livne entry
        # TEMPORARILY DEACTIVATED (2026-08-15). Biton no longer runs under the
        # supervisor and is excluded from Steve's daily report and email.
        #
        # Its state is fully preserved and untouched: 36 trades in
        # runtime/stores/trades_biton.json, the Excel journal, 50 funnel
        # exports and the backtest log all remain on disk exactly as they were.
        #
        # OPERATIONAL WARNING -- this is a PAUSE, not a wind-down. 13 positions
        # were non-terminal at deactivation (11 ACTIVE + 1 PENDING + the
        # long-stuck PARTIAL_TP1 GOOGL record). While disabled, nothing
        # refreshes them: no stop or target is checked and no exit alert is
        # sent. Their recorded status simply freezes. On re-enable they are
        # graded against whatever the price is AT THAT MOMENT, so a stop or
        # target crossed during the pause is not detected retroactively -- the
        # resulting P&L for those 13 will not reflect what actually happened.
        # If that matters, close them out before pausing rather than after.
        #
        # To re-enable: set this back to True. Nothing else needs changing.
        enabled=False,
    ),
    ModelConfig(
        key="horizon",
        label="Horizon model",
        description=("MTFA intraday derived from Livne, recalibrated against the LOSS_SL drag: "
                     "high-conviction reversals only, tighter volume gate, per-asset-class risk, "
                     "a midday session block for equities, and a stagnation exit."),
        cycle_interval_minutes=2,
        # Livne's vocabulary plus STAGNATION_EXIT. It is a REAL close with a
        # realized P&L -- a position cut for going nowhere, before it decays
        # into a full stop -- so it belongs in closed_with_pnl as well.
        # Omitting it there would drop every stagnation close out of win rate,
        # profit factor, expectancy and drawdown: precisely the silent metric
        # corruption this module's docstring records having happened once.
        closed_statuses=("WIN_TP2", "LOSS_SL", "LOSS_SL_BE", "EMERGENCY_EXIT",
                         "STAGNATION_EXIT", "EXPIRED"),
        update_statuses=("PARTIAL_TP1",),
        closed_with_pnl=("WIN_TP2", "LOSS_SL", "LOSS_SL_BE", "EMERGENCY_EXIT",
                         "STAGNATION_EXIT"),
        telegram_route="horizon",
        # Incubating: it runs on its own interval and alerts on its own route,
        # but stays OUT of the executive report until it has earned a place
        # there. A recalibration with no live evidence must not move the
        # headline numbers Livne is judged by.
        stage="incubating",
        # DEACTIVATED 2026-08-19 by operator request, after running live from
        # 2026-08-17. Activated then WITHOUT a completed baseline backtest --
        # a deliberate operator decision; docs/HORIZON_CALIBRATION.md records
        # what was still unestablished.
        #
        # What the live run produced: 22 trades, 21 of them closed --
        # 6 WIN_TP2, 8 LOSS_SL_BE, 2 LOSS_SL, 2 EMERGENCY_EXIT, and 3
        # STAGNATION_EXIT (the new exit fired live, and graded with P&L as
        # intended). That is a two-day sample and settles nothing: do NOT
        # retune against it.
        #
        # OPERATIONAL WARNING -- this is a PAUSE, not a wind-down, and ONE
        # position was non-terminal at deactivation:
        #     ARKK (id 5a179641) LONG, PARTIAL_TP1. TP1 filled at 82.19 and
        #     50% is closed; the remaining 50% carries a stop at the breakeven
        #     price 81.17 (raised from 79.90), with target2 at 83.34.
        # While disabled nothing refreshes it: neither that stop nor the
        # target is checked and no exit alert is sent. Its status simply
        # freezes. On re-enable it is graded against the price AT THAT MOMENT,
        # so a level crossed during the pause is not detected retroactively
        # and the recorded P&L will not reflect what actually happened. The
        # exposure is bounded -- TP1 is already banked and the runner's stop
        # sits at breakeven -- but it is not zero. To avoid it entirely, close
        # ARKK out before resuming rather than after.
        #
        # It stays `stage="incubating"`, so on re-enable it still cannot move
        # the headline numbers Livne is judged by until explicitly graduated.
        #
        # To re-enable: set this back to True. Nothing else needs changing.
        enabled=False,
    ),
    # ------------------------------------------------------------------
    # CLIENT MODELS BEGIN -- managed by scripts/create_client_model.py
    #
    # Entries between these two markers are generated. Each is wrapped in its
    # own `# >>> client:<key> >>>` / `# <<< client:<key> <<<` pair so the tool
    # can add and remove one cleanly without parsing Python or going anywhere
    # near the first-party models above. Hand-editing an entry is fine; just
    # leave the markers intact.
    # ------------------------------------------------------------------
    # CLIENT MODELS END
)

_BY_KEY: dict[str, ModelConfig] = {m.key: m for m in _MODELS}


def all_models() -> tuple[ModelConfig, ...]:
    """EVERY registered model, enabled or not, in canonical (report) order.

    Use `active_models()` for anything that runs or reports. This one is for
    tooling that must see the full roster regardless of state -- diagnostics,
    "what exists on disk" checks, and the tests that pin path derivation.
    """
    return _MODELS


def active_models() -> tuple[ModelConfig, ...]:
    """Models that should actually execute and be reported on.

    This is the roster the supervisor schedules and Steve reports on. A
    disabled model keeps its registry entry -- so its paths resolve, its
    history stays readable, and its own config.py still imports -- it simply
    stops running and stops appearing in reports.
    """
    return tuple(m for m in _MODELS if m.enabled)


def models_by_stage(stage: str) -> tuple[ModelConfig, ...]:
    """Enabled models at one lifecycle stage."""
    return tuple(m for m in active_models() if m.stage == stage)


def production_models() -> tuple[ModelConfig, ...]:
    """The roster the executive report covers by default.

    Enabled AND production. An incubating client strategy runs and alerts, but
    must not move the numbers the primary models are judged by until it has
    earned it (see the graduation checklist in
    docs/CLIENT_ONBOARDING_GUIDE.md).
    """
    return models_by_stage("production")


def incubating_models() -> tuple[ModelConfig, ...]:
    return models_by_stage("incubating")


def client_models() -> tuple[ModelConfig, ...]:
    """Every scaffolded client strategy, whatever its stage."""
    return tuple(m for m in _MODELS if m.key.startswith("client_"))


def disabled_models() -> tuple[ModelConfig, ...]:
    """Registered but deactivated. Surfaced by the supervisor at startup so a
    paused model is visible rather than silently absent -- the failure mode of
    a flag like this is forgetting it is set."""
    return tuple(m for m in _MODELS if not m.enabled)


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
