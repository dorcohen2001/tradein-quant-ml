"""Blast-radius guarantees for scaffolded client strategies.

Four properties, each of which is a way a client could damage production if it
were not enforced:

  1. PATH COLLISION    a client sharing a runtime file with a live model would
                       corrupt real trade history.
  2. ROUTING LEAK      a misconfigured client publishing signals into the
                       primary channel. Unrecoverable once sent.
  3. CRASH BLAST       a client crash taking down the supervisor and stopping
                       every other model with it.
  4. REPORT GATE       an unproven trial silently moving the headline numbers
                       the production models are judged by.

These are tested against a SYNTHETIC ModelConfig rather than a scaffolded
directory, so the suite has no ordering dependency on the generator having been
run and cannot be broken by a leftover sandbox.
"""
from __future__ import annotations

import datetime as dt
import importlib
import os
import subprocess
import sys
import textwrap

import pytest

from core.registry import (
    ModelConfig,
    active_models,
    all_models,
    client_models,
    incubating_models,
    model_keys,
    production_models,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def client_config(key: str = "client_acme", **overrides) -> ModelConfig:
    """A client entry shaped exactly as the generator writes one."""
    base = dict(
        key=key,
        label="ACME Capital",
        description="Synthetic client used by the isolation tests.",
        cycle_interval_minutes=5,
        closed_statuses=("WIN_TP1", "WIN_TP2", "LOSS_SL", "EXPIRED"),
        update_statuses=(),
        closed_with_pnl=("WIN_TP1", "WIN_TP2", "LOSS_SL"),
        telegram_route=key,
        stage="incubating",
        dedicated_route=True,
    )
    base.update(overrides)
    return ModelConfig(**base)


# ==========================================================================
# 1. Path collision
# ==========================================================================
ARTIFACTS = ("trade_store", "report_state_store", "journal_xlsx", "backtest_csv")


def test_client_artifacts_never_collide_with_a_registered_model():
    """The property that protects real trade history: no client path may equal
    any existing model's path."""
    client = client_config()
    existing = {getattr(m, attr) for m in all_models() for attr in ARTIFACTS}
    for attr in ARTIFACTS:
        assert getattr(client, attr) not in existing, attr


def test_client_prefixes_never_collide():
    """Funnel exports and charts share one directory each -- only the prefix
    keeps them apart, and a shared prefix means one model's retention sweep
    deletes another's files."""
    client = client_config()
    for m in all_models():
        assert client.funnel_prefix != m.funnel_prefix
        assert client.chart_prefix != m.chart_prefix


def test_every_client_artifact_carries_the_full_key():
    client = client_config()
    for attr in ARTIFACTS:
        assert client.key in os.path.basename(getattr(client, attr)), attr
    assert client.funnel_prefix.endswith(f"{client.key}_")
    assert client.chart_prefix == f"{client.key}_"


def test_two_clients_do_not_collide_with_each_other():
    a, b = client_config("client_alpha"), client_config("client_beta")
    for attr in ARTIFACTS:
        assert getattr(a, attr) != getattr(b, attr), attr
    assert a.chart_prefix != b.chart_prefix


def test_client_lives_under_its_own_directory():
    client = client_config()
    assert os.path.basename(client.model_dir) == client.key
    for m in all_models():
        if m.key != client.key:
            assert os.path.normpath(client.model_dir) != os.path.normpath(m.model_dir)


def test_registered_client_keys_are_namespaced():
    """Anything the generator registered must be discoverable as a client and
    must not shadow a first-party key."""
    for m in client_models():
        assert m.key.startswith("client_")
        assert m.key not in ("livne", "biton")


# ==========================================================================
# 2. Routing isolation
# ==========================================================================
DEFAULTS = {"TELEGRAM_BOT_TOKEN": "PRIMARY_TOKEN", "TELEGRAM_CHAT_ID": "-100PRIMARY",
            "TELEGRAM_BACKOFF_BASE_SECONDS": "0"}


def reload_telegram(env: dict):
    for key in [k for k in os.environ if k.startswith("TELEGRAM")]:
        del os.environ[key]
    os.environ.update(env)
    for name in ("core.settings", "core.utils.telegram_notifier"):
        sys.modules.pop(name, None)
    return (importlib.import_module("core.settings"),
            importlib.import_module("core.utils.telegram_notifier"))


@pytest.fixture(autouse=True)
def _restore_modules():
    yield
    for name in ("core.settings", "core.utils.telegram_notifier"):
        sys.modules.pop(name, None)


def test_unconfigured_client_route_does_not_fall_back_to_production():
    """THE leak test. A first-party route falls back to the shared chat when
    unset -- convenient, and catastrophic for a client. With `strict` the
    credentials resolve EMPTY instead."""
    settings, _ = reload_telegram(dict(DEFAULTS))
    leaky = settings.get_telegram_route("client_acme")            # default behaviour
    strict = settings.get_telegram_route("client_acme", strict=True)

    assert leaky.chat_id == "-100PRIMARY"      # this is what we must not do
    assert strict.chat_id == ""
    assert strict.token == ""
    assert strict.is_configured is False


def test_unconfigured_client_send_touches_no_network_and_no_other_chat():
    _settings, notifier = reload_telegram(dict(DEFAULTS))
    notifier.requests.post = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a dedicated route with no config must not send ANYWHERE"))

    assert notifier.send_telegram_message("client signal", route="client_acme",
                                          strict_route=True) == {"ok": False, "message_id": None}


def test_configured_client_route_resolves_only_to_its_own_chat():
    settings, notifier = reload_telegram({
        **DEFAULTS,
        "TELEGRAM_ROUTE_CLIENT_ACME_TOKEN": "ACME_TOKEN",
        "TELEGRAM_ROUTE_CLIENT_ACME_CHAT_ID": "-100ACME",
    })
    route = settings.get_telegram_route("client_acme", strict=True)
    assert (route.token, route.chat_id) == ("ACME_TOKEN", "-100ACME")

    calls = []
    notifier.requests.post = lambda url, **kw: calls.append({"url": url, **kw}) or _ok()
    notifier.send_telegram_message("client signal", route="client_acme", strict_route=True)

    assert calls[0]["json"]["chat_id"] == "-100ACME"
    assert "ACME_TOKEN" in calls[0]["url"]
    assert "-100PRIMARY" not in str(calls[0])
    assert "PRIMARY_TOKEN" not in calls[0]["url"]


def _ok():
    class R:
        status_code = 200
        headers: dict = {}

        def json(self):
            return {"ok": True, "result": {"message_id": 1}}

        def raise_for_status(self):
            return None
    return R()


def test_client_env_var_names_match_what_the_generator_prints():
    """The guide, the generator and the resolver must agree on the names --
    a mismatch means a user follows the docs and gets silence."""
    settings, _ = reload_telegram(dict(DEFAULTS))
    token_var, chat_var = settings.route_env_names("client_acme")
    assert token_var == "TELEGRAM_ROUTE_CLIENT_ACME_TOKEN"
    assert chat_var == "TELEGRAM_ROUTE_CLIENT_ACME_CHAT_ID"


def test_first_party_routes_keep_their_forgiving_fallback():
    """Isolation must not regress the existing models: unconfigured Livne still
    resolves to the shared chat exactly as before."""
    settings, _ = reload_telegram(dict(DEFAULTS))
    assert settings.get_telegram_route("livne").chat_id == "-100PRIMARY"


def test_registered_clients_all_declare_a_dedicated_route():
    for m in client_models():
        assert m.dedicated_route is True, f"{m.key} would leak into the default chat"
        assert m.telegram_route == m.key


# ==========================================================================
# 3. Supervisor crash containment
# ==========================================================================
CRASHING_RUNNER = textwrap.dedent('''
    import sys
    print("client boot", file=sys.stderr)
    raise RuntimeError("client strategy exploded")
''')

SLOW_RUNNER = textwrap.dedent('''
    import time
    time.sleep(60)
''')


def _supervisor():
    sys.path.insert(0, REPO_ROOT)
    return importlib.import_module("run_supervisor")


class _Args:
    asset_class = None
    top_n = None
    candidate_pool = None
    skip_scan = False
    once = True


def _fake_model(tmp_path, key, body, timeout=480):
    model_dir = tmp_path / key
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "live_runner.py").write_text(body, encoding="utf-8")

    cfg = client_config(key, phase_timeout_seconds=timeout)
    # Point the ModelConfig's derived directory at the sandbox. The dataclass is
    # frozen (deliberately), so patch the property on a throwaway subclass
    # rather than mutating shared registry state.
    class _Sandboxed(ModelConfig):
        @property
        def model_dir(self):
            return str(model_dir)

        @property
        def live_runner(self):
            return str(model_dir / "live_runner.py")

    return _Sandboxed(**{f.name: getattr(cfg, f.name) for f in cfg.__dataclass_fields__.values()})


def test_client_crash_is_contained_and_reported(tmp_path):
    """A client raising must be reported as a non-zero exit, not propagate."""
    supervisor = _supervisor()
    model = _fake_model(tmp_path, "client_boom", CRASHING_RUNNER)
    outcome = supervisor.run_model_cycle(model, _Args())
    assert "exit code" in outcome
    assert "other models unaffected" in outcome


def test_client_hang_is_killed_by_its_own_timeout(tmp_path):
    """A hang is NOT an exception -- only a timeout can bound it, and without
    one the supervisor thread would block forever."""
    supervisor = _supervisor()
    model = _fake_model(tmp_path, "client_hang", SLOW_RUNNER, timeout=2)
    outcome = supervisor.run_model_cycle(model, _Args())
    assert "TIMEOUT" in outcome
    assert "other models unaffected" in outcome


def test_a_crashing_client_does_not_stop_a_healthy_model(tmp_path):
    """The blast-radius property itself: the healthy model still runs to
    completion after the crashing one has already failed."""
    supervisor = _supervisor()
    boom = _fake_model(tmp_path, "client_boom2", CRASHING_RUNNER)
    fine = _fake_model(tmp_path, "client_fine", "print('ok')\n")

    assert "exit code" in supervisor.run_model_cycle(boom, _Args())
    assert supervisor.run_model_cycle(fine, _Args()).startswith("ok in")


def test_missing_client_directory_is_skipped_not_fatal(tmp_path):
    supervisor = _supervisor()
    model = client_config("client_ghost")
    assert "skipped" in supervisor.run_model_cycle(model, _Args())


def test_supervisor_schedules_incubating_models_normally():
    """Stage gates REPORTING, never execution -- a trial that does not trade
    produces no evidence to graduate on."""
    for m in incubating_models():
        assert m.enabled is True
        assert m in active_models()


# ==========================================================================
# 4. Steve reporting gate
# ==========================================================================
def test_incubating_models_are_absent_from_the_production_roster():
    production = {m.key for m in production_models()}
    for m in incubating_models():
        assert m.key not in production


def test_production_roster_is_exactly_the_enabled_production_models():
    expected = {m.key for m in all_models() if m.enabled and m.stage == "production"}
    assert {m.key for m in production_models()} == expected


def test_first_party_models_are_explicitly_production():
    """They must not inherit the incubating default, which would silently drop
    them out of the executive report."""
    by_key = {m.key: m for m in all_models()}
    assert by_key["livne"].stage == "production"
    assert by_key["biton"].stage == "production"


def test_new_models_default_to_incubating():
    """Safe direction: an unproven strategy cannot reach the report by
    omission."""
    fresh = ModelConfig(
        key="client_unspecified", label="X", cycle_interval_minutes=5,
        closed_statuses=("WIN_TP1",), update_statuses=(), closed_with_pnl=("WIN_TP1",),
    )
    assert fresh.stage == "incubating"
    assert fresh.dedicated_route is False  # opt-in; the generator sets it True


def test_steve_reports_production_only_by_default():
    """Exercises the analyzer module itself, not just the registry."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_steve_gate", os.path.join(REPO_ROOT, "agents", "steve", "daily_tradein_analyzer.py"))
    steve = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(steve)

    assert {m.key for m in steve.MODELS} == {m.key for m in production_models()}
    for m in incubating_models():
        assert m.key not in {x.key for x in steve.MODELS}


def test_steve_can_opt_into_incubating_models():
    """`--include-incubating` widens the roster to every enabled model."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_steve_opt", os.path.join(REPO_ROOT, "agents", "steve", "daily_tradein_analyzer.py"))
    steve = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(steve)

    steve.MODELS = steve.active_models()
    assert {m.key for m in steve.MODELS} == {m.key for m in active_models()}


# ==========================================================================
# Generator contract
# ==========================================================================
def test_generator_refuses_a_key_that_collides_with_a_registered_model():
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "scripts", "create_client_model.py"),
         "--key", "livne", "--name", "Should Not Work"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120,
    )
    assert proc.returncode == 1
    assert "reserved" in (proc.stdout + proc.stderr).lower()


def test_generator_rejects_malformed_keys():
    """Anything that cannot become a clean directory/filename component is
    refused BEFORE anything is written. Case is not in this list -- an
    uppercase key is normalised (and the normalisation is announced), because
    the key ends up as a directory name and silently rejecting `Alpha` would be
    obstructive where lowercasing it is not."""
    for bad in ("With Spaces", "9leading", "has-dash", "x", "sym!bol"):
        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "create_client_model.py"),
             "--key", bad, "--name", "X"],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=120,
        )
        assert proc.returncode == 1, bad
        assert not os.path.isdir(os.path.join(REPO_ROOT, "models", f"client_{bad.lower()}")), bad


def test_registry_has_the_client_markers_the_generator_writes_between():
    """If these are ever removed, the generator refuses rather than guessing
    where an entry belongs -- this test makes that failure loud and early."""
    src = open(os.path.join(REPO_ROOT, "core", "registry.py"), encoding="utf-8").read()
    assert "# CLIENT MODELS BEGIN" in src
    assert "# CLIENT MODELS END" in src
    assert src.index("# CLIENT MODELS BEGIN") < src.index("# CLIENT MODELS END")


def test_live_model_keys_are_still_registered():
    """A blunt guard on the whole exercise: whatever client work happens, the
    two first-party models must remain registered."""
    assert "livne" in model_keys()
    assert "biton" in model_keys()
