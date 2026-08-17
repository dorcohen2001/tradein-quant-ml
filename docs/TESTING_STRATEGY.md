# Testing strategy: 100 models, zero model inspections

**The rule this document exists to enforce:** a change to `core/` is verified
without opening, editing, or hand-running a single model directory.

That rule was broken repeatedly while the audit work was done — verifying a
data-layer change meant `cd models/livne && python -c "..."` and reading the
output. That is affordable at two models. At ten it is a day. At a hundred it
does not happen at all, which means core changes ship unverified and the first
person to notice is a client.

---

## 1. The shape of the problem

A model is not a library the platform calls; it is a **plug-in the platform
runs**. Three facts follow:

1. **Models cannot share an interpreter.** Each is its own `sys.path` root and
   they all define top-level `config`, `utils`, `strategy`. Any harness that
   touches more than one model *must* use subprocesses. This is a constraint,
   not a preference.
2. **Models are owned by different people.** A client's `screener.py` is
   theirs. Platform CI may not require edits to it, and platform tests may not
   depend on what it decides.
3. **Models are the least trustworthy code in the system.** They are newest,
   least reviewed, and most often written under time pressure during
   onboarding. The platform must be robust to a bad one, and must be able to
   *identify* the bad one without a human reading it.

---

## 2. The three test layers

| Layer | What it proves | Touches models? | Network? | Runtime |
|---|---|---|---|---|
| **Core unit** (`tests/test_*.py`) | The platform's own logic: lock, outbox, refresh window, retention, intervals, escalation | no | no | ~15 s |
| **Contract conformance** (`tests/test_model_contract.py`) | *Every registered tenant* satisfies the platform invariants | as a black box, in a subprocess | no | ~1 s/model |
| **Live smoke** (nightly, non-blocking) | The venues still answer, credentials still work | no | yes | minutes |

Only the first two gate a merge. The third is allowed to fail without blocking:
a Yahoo outage is not a reason to stop shipping.

### 2.1 Core is validated against a *reference* tenant, never a real one

`core/runtime/tenant.py` is exercised end to end by `FakeStrategy` in
`tests/test_tenant_runner.py` — a complete tenant in ~30 lines. Cycle order,
`--skip-scan`, containment of a failing screener, dedup, lock refusal and exit
codes are all asserted there. **No real model is involved**, so no core change
requires reading one.

The reference tenant is also the honest measure of the plug-in interface: if
adding a core feature makes `FakeStrategy` long or awkward, the interface is
wrong — and that shows up before a hundred model owners have to implement it.

### 2.2 Contract conformance replaces per-model inspection

`core/testing/conformance.py` is the executable definition of what a tenant
must do. It knows exactly one thing about a model: that it exposes
`build_runner()`. It then asserts platform invariants only — never whether the
strategy is any *good*, which is the owner's business:

| Invariant | Why it is a platform concern |
|---|---|
| `registered`, `state_dir_is_derived` | Without these the tenant lock and path namespacing cannot work |
| `statuses_are_declared` | A status outside the registry vocabulary silently drops the trade from win rate, profit factor and drawdown — this has happened before |
| `terminal_is_final` | The catch-up replay overlaps the live path by a bar; a resurrectable trade corrupts history |
| `bar_replay_is_idempotent` | Same reason, at the boundary bar |
| `chronology`, `loss_is_negative`, `short_loss_is_negative` | A tenant reporting a profit on a stop-out poisons every aggregate the client is shown |
| `survives_provider_blackout` | One venue outage must not crash a tenant |
| `cli_accepts_supervisor_flags` | A tenant that rejects a flag `run_supervisor` sends looks like a crash |
| `path_namespaced:*`, `route_is_own` | The two failures that damage *another* client |

Run it for one model:

```bash
cd models/client_alpha
PYTHONPATH=/path/to/TRADEIN python -m core.testing.conformance
```

Run it for the entire fleet — this is what CI does:

```bash
pytest tests/test_model_contract.py
```

`tests/test_model_contract.py` contains **no model names**. It enumerates
`core.registry.available_models()` and parametrises over them. The hundredth
tenant adds zero test code.

### 2.3 The battery must be able to fail

A conformance suite that cannot fail manufactures confidence. Four tests build
a deliberately broken synthetic tenant and assert the battery catches it:
a resurrecting lifecycle, an undeclared status, inverted P&L accounting, and a
model that raises on import (which must produce a *failing report*, not take
the session down — otherwise one bad tenant hides the other ninety-nine).

---

## 3. Data-layer changes are validated with scripted markets

`core/testing/fake_market.py` provides `FakeMarket` and `scripted_series`.
Neither live data nor recorded cassettes are used:

* **Live data** makes tests fail on a plane, fail when a venue throttles, and
  pass or fail based on what a stock did this morning.
* **Cassettes** go stale, need per-symbol refreshing, and encode whatever the
  market happened to do that day — never the case you actually want.

Scripted series produce the interesting shapes on demand. The IPO regression
(`tests/test_short_history.py`) is the worked example: a stub provider with a
listing date reproduces "possibly delisted; no price data found" in
milliseconds, on a machine that has never heard of ARM.

**Every pathological market shape gets a fixture, not a ticker:** short history
(IPO), no history (delisted), a gap (halt), a truncated response (provider
retention cap), an empty response, a rate-limited response.

---

## 4. Contract versioning, for migrating a fleet

`CONTRACT_VERSION` in `core/testing/conformance.py` is bumped when the platform
adds or changes an invariant. Each conformance report records the version it
was checked against. With a hundred tenants:

1. Add the invariant, bump the version.
2. Existing tenants keep passing if they already satisfy it (most will).
3. Those that do not appear as named failures in one CI run — a **list**, not
   an investigation.
4. Fix or grandfather them in waves; the report says exactly which and why.

That is the difference between "the platform changed, go check a hundred
directories" and "the platform changed, here are the four tenants affected".

---

## 5. The boundary, stated as a rule

> **A core change must never require an edit under `models/`.**

Worth enforcing mechanically in CI: a commit touching both `core/**` and
`models/**` (excluding a freshly generated scaffold) is either a leaking
abstraction or a change that belongs in two commits. When the rule is about to
be broken, the fix is almost always to add a default to `TenantStrategy` rather
than to require every tenant to implement something new.

---

## 6. What is still missing

Named rather than implied:

* **Strategy-layer tests.** `ta/mtfa.py` (716 lines), the trade generator and
  the risk manager have no tests. The conformance battery checks the
  *lifecycle*, not the *signal*. Recorded-frame fixtures plus R:R invariants
  (TP1 ≥ 1.0R, TP2 − TP1 ≥ 0.75R, stop ≤ 2.5 ATR) would have caught the
  identical-TP1/TP2 bug the config comments describe.
* **Backtest↔live parity as a graduation gate** — replay the last N days
  through the same `apply_bar` and diff against the store.
* **A conformance check for scan cost** — a tenant whose scan makes 4,000
  requests is a fleet problem, and nothing currently notices.
* **Per-tenant resource budgets** in the registry, enforced by the rate
  governor.
