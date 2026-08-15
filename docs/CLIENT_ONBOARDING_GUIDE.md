# Client Onboarding Guide

**This document is an executable protocol.** When someone says *"I want to
onboard a new client"*, *"add a client strategy"*, or anything equivalent, read
this file first and follow it top to bottom.

---

## 0. Instructions for the AI agent

### Your role

You are a **structured onboarding consultant**. You gather requirements, you
explain the Telegram setup in plain language, you run the generator, you
validate, and you hand back a working sandbox. You are not improvising an
architecture — the scaffolding is already built.

### Hard rules — do not violate these

1. **Ask before you generate.** Work through §1 and get answers. Do not scaffold
   a model from assumptions; a wrong key means a wrong directory, a wrong trade
   store, and a rename later.
2. **Never touch existing models.** `models/livne/`, `models/biton/`, and any
   other existing model directory are off-limits during onboarding. Nothing
   about adding a client requires editing them.
3. **Never touch `runtime/`.** No trade store, journal, funnel export or log is
   read-modify-written during onboarding. The generator does not go near them.
4. **Never hand-edit `core/registry.py` to add a client.** Use the generator —
   it writes between the `CLIENT MODELS` markers and can cleanly reverse itself.
5. **Never put a client's credentials in the chat.** Ask the user to paste them
   into `.env` themselves. You must not read back, echo, or store a bot token.
6. **Do not promote to `production`** without walking the graduation checklist
   in §6 and getting explicit confirmation.

### Workflow

```
§1 Ask the questions  →  §2 Telegram setup (user does this)  →  §3 Generate
   →  §4 Validate  →  §5 Go live  →  §6 Graduate (later)
```

Ask the §1 questions **in order, in small batches** — do not dump twenty
questions at once. Confirm the summary back before generating.

---

## 1. Requirements to gather

### 1a. Identity

| Question | Why it matters | Example |
|---|---|---|
| Short client key? | Becomes `models/client_<key>/` and every runtime filename. Lowercase letters/digits/underscores. **Hard to change later.** | `alpha` |
| Display name? | Shown in every Telegram alert and report. | `Alpha Capital` |
| One-line description? | Appears in the executive report roster. | `Mean reversion on 1h crypto majors` |

> The key `alpha` produces the model key `client_alpha`. If the user gives a key
> that already starts with `client_`, it is not doubled.

### 1b. Strategy

| Question | Options / notes |
|---|---|
| Asset class? | `crypto` (24/7), `stocks` (US regular hours only), or `both` |
| Analysis timeframe? | `5m`, `15m`, `1h`, `4h`, `1d` — the bar the rules evaluate |
| Execution interval? | Minutes between cycles. **Must be well under the timeframe.** A 15m strategy polled every 15m gets ~1 check per bar; 3–5 min is sane. |
| Watchlist? | Which symbols. Defaults to 5 majors per class. |
| Entry conditions? | The actual rules. Capture them precisely — this becomes `strategy/screener.py`. |
| Stop loss rule? | Default is ATR-multiple. Ask for fixed %, structural, or ATR. |
| Take profit rules? | TP1/TP2 as ATR multiples by default. |
| Minimum R:R? | Hard gate; setups below it are rejected. Default `1.5`. |
| Pending expiry? | How long an un-filled signal stays live. Default 12h. |

If the user does not have firm rules yet, say so plainly and scaffold with the
placeholder trend-pullback rule — it runs end to end and is meant to be
replaced. Do not invent rules and present them as a strategy.

> For anything beyond a couple of conditions, send the client
> [`STRATEGY_SPEC_TEMPLATE.md`](STRATEGY_SPEC_TEMPLATE.md) rather than
> interviewing them field by field. It carries a fully worked reference model
> (Livne, with its real parameters) plus the rules for writing conditions that
> can actually be implemented — usually the difference between one round of
> questions and four.

### 1c. Telegram

| Question | Notes |
|---|---|
| Shared hub, or a dedicated channel? | **Default is the shared hub.** Choose dedicated only if this client's signals must be private from other tenants. |
| If dedicated: dedicated bot, or reuse an existing one? | A dedicated bot per client is cleaner and revocable. Reusing one bot across several groups is fine and common. |
| Channel or group? | Either works. A **channel** is broadcast-only; a **group** allows replies. |
| Who administers it? | The bot must be an admin to post. |

**Default is the SHARED PRIMARY HUB.** Unless the client asks for its own
channel, its alerts go to the main `TELEGRAM_CHAT_ID` group alongside every
other tenant, prefixed with `[Model Name]`. That is the intended platform
behaviour, not a fallback — state it plainly so the client knows who can see
their signals.

Only pass `--dedicated-route` when the client's signals must NOT be visible to
whoever reads the main channel. A dedicated tenant becomes strict: if its two
environment variables are missing it sends **nothing** rather than falling back
to the hub. Silence is the designed failure mode for that case.

---

## 2. Telegram setup — instructions to give the user

Give these to the user verbatim. They do this part; you do not.

### Step 1 — Create the bot

1. Open Telegram and search for **@BotFather** (blue check).
2. Send `/newbot`.
3. Give it a display name (e.g. `Alpha Capital Signals`).
4. Give it a username ending in `bot` (e.g. `alpha_capital_signals_bot`).
5. BotFather replies with a **token** like `8123456789:AAF...`.

> **Treat the token like a password.** Anyone holding it can post as the bot.
> Paste it straight into `.env` — not into chat, email, or a ticket.
> If it leaks, send `/revoke` to BotFather and generate a new one.

### Step 2 — Create the destination and add the bot

**For a group:**
1. Create the group and add the members.
2. Add your new bot to it.
3. Group name → *Edit* → *Administrators* → *Add Admin* → select the bot.

**For a channel:**
1. Create the channel.
2. Channel name → *Administrators* → *Add Admin* → select the bot.
3. Grant **Post Messages** (that is the only permission it needs).

### Step 3 — Get the chat ID

Easiest reliable method:

1. Post any message in the group/channel.
2. Forward that message to **@userinfobot**.
3. It replies with the chat ID.

It looks like `-1001234567890`. **Keep the leading minus sign** — groups and
channels always have negative IDs, and dropping it is the single most common
setup mistake.

*Alternative:* add **@RawDataBot** to the group briefly, read `chat.id` from
what it posts, then remove it.

### Step 4 — Put both values in `.env`

Open the repo-root `.env` (create it if missing) and add — replacing `<KEY>`
with the uppercased model key, e.g. `CLIENT_ALPHA`:

```env
TELEGRAM_ROUTE_CLIENT_<KEY>_TOKEN=8123456789:AAF...
TELEGRAM_ROUTE_CLIENT_<KEY>_CHAT_ID=-1001234567890
```

The generator prints the exact two variable names for the key it created —
use those, don't retype from here.

`.env` is gitignored, so these never reach git. They **are** included in
`--mode full` backup archives, which is why those stay on the local machine.

### Step 5 — Verify

```bash
python -m core.utils.telegram_notifier --route client_<key>
```

Expected: `configured=True` and a test message arriving in the client's
channel. If it says `configured=False`, one of the two variables is missing or
misspelled — nothing was sent anywhere.

---

## 3. Generate the model

```bash
python scripts/create_client_model.py --key alpha --name "Alpha Capital" --interval 5 --timeframe 15m --asset-class crypto
```

| Flag | Meaning |
|---|---|
| `--key` | Short key → `models/client_alpha/` |
| `--name` | Display name in alerts and reports |
| `--interval` | Minutes between cycles |
| `--timeframe` | Analysis bar size |
| `--asset-class` | `crypto` / `stocks` / `both` |
| `--description` | One line for the report roster |
| `--dedicated-route` | Give this tenant its own channel instead of the shared hub |
| `--force` | Overwrite an existing directory |
| `--remove <key>` | Deregister and delete the scaffold (runtime state kept) |

This writes to **exactly two places**: the new `models/client_<key>/`
directory, and one entry between the `CLIENT MODELS` markers at the end of
`_MODELS` in `core/registry.py`. Nothing else is touched.

The model is registered as `stage="incubating"`. Routing follows the shared
hub unless `--dedicated-route` was passed.

### What gets generated

```
models/client_<key>/
├── __init__.py
├── _bootstrap.py          sys.path setup so core.* imports
├── config.py              paths from core.registry + the tunable section
├── live_runner.py         execution loop (should not need editing)
├── strategy/
│   ├── __init__.py
│   ├── screener.py        ← THE STRATEGY. Replace evaluate().
│   └── ta_engine.py       ← indicators. Add freely.
└── README.md              strategy docs + parameter spec
```

### Then write the strategy

`strategy/screener.py` ships with a working placeholder (EMA trend + RSI
pullback, ATR-based stops/targets) so the scaffold runs immediately. Replace
`evaluate()` with the client's real rules.

Its contract: return `(ClientSetup | None, reason)`, and **use only bars up to
the last row**. Reading ahead is the classic backtest-vs-live divergence and it
fails silently.

---

## 4. Validation — run all of these

```bash
# 1. Route resolves to the client's own chat (and nowhere else)
python -m core.utils.telegram_notifier --route client_<key>

# 2. Strategy runs, sends nothing, logs nothing
cd models/client_<key> && python live_runner.py --once --dry-run

# 3. Supervisor picks it up on its own interval
python run_supervisor.py --dry-run

# 4. Nothing else regressed
pytest tests/
```

Confirm before declaring success:

- [ ] Dry run exits `0` and prints per-symbol accept/reject reasons
- [ ] Supervisor lists it as `[incubating]` with `dedicated`
- [ ] Full test suite passes
- [ ] `runtime/stores/trades_livne.json` and `trades_biton.json` are unchanged
- [ ] A test alert arrived in the **client's** channel and nowhere else

---

## 5. Go live (incubating)

```bash
python run_supervisor.py
```

The client model now runs on its own interval, in its own process, with its own
timeout, alerting to its own channel. A crash or hang inside it cannot affect
any other model.

While incubating it is **excluded from the executive report**. To look at it:

```bash
python agents/steve/daily_tradein_analyzer.py --include-incubating --no-llm --no-email
```

---

## 6. Graduation checklist — incubating → production

Do **not** promote on request alone. Walk this with the user first:

- [ ] **Sample size.** At least 20–30 closed trades. Below that, win rate is noise.
- [ ] **Trial length.** At least 2–4 weeks of live paper trading, spanning more
      than one market condition.
- [ ] **Rules are final.** No parameter changes during the trial window — every
      change restarts the sample.
- [ ] **Backtest agrees with live.** Live win rate and expectancy are in the
      same neighbourhood as the simulation. A large gap means the live path is
      doing something the backtest is not.
- [ ] **Failure modes understood.** You can describe when it loses, not just that it does.
- [ ] **Telegram delivery is clean.** No missed alerts, no rate-limit failures in the logs.
- [ ] **Client has signed off** on the numbers as representative.

Then, in `core/registry.py`, change that model's entry:

```python
stage="incubating",   →   stage="production",
```

Nothing else changes — the model keeps its key, paths, history and dedicated
route. It simply starts appearing in the executive report. Re-run
`pytest tests/` afterwards.

To pause instead of promote, set `enabled=False`. To retire while keeping
history, set `stage="deprecated"`.

---

## 7. Architectural guarantees (why this is safe)

| Guarantee | Mechanism |
|---|---|
| No path collisions | Every runtime path derives from the model key in `core/registry.py`. Enforced by `tests/test_client_isolation.py`. |
| Predictable routing | Default is the shared hub (every alert name-prefixed). `--dedicated-route` opts into a private channel AND disables the fallback, so a misconfigured dedicated tenant sends nothing rather than leaking to the hub. |
| No crash propagation | The supervisor runs each model as its own subprocess with its own timeout. |
| No report contamination | `stage="incubating"` keeps a trial out of the executive report. |
| No edits to live models | The generator writes only the new directory + one registry entry. |
| Clean rollback | `--remove <key>` deregisters and deletes the scaffold, keeping runtime history. |

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `configured=False` | One or both env vars missing/misspelled | Check exact names from the generator's output |
| Test message never arrives | Bot is not an admin, or chat ID lost its `-` | Re-add as admin; re-check the ID |
| `unknown model 'client_x'` | Registry entry missing | Re-run the generator, or check the `CLIENT MODELS` markers exist |
| Model missing from the report | It is incubating | Expected. Use `--include-incubating`, or graduate it |
| Supervisor skips it | Directory missing, or `enabled=False` | Check `models/client_<key>/live_runner.py` exists |
| Every symbol rejected | Placeholder rules are intentionally narrow | Replace `evaluate()` with the real strategy |
