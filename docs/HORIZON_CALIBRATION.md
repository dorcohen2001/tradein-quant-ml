# Horizon — calibration record

Horizon is Livne's MTFA pipeline with five targeted changes, aimed at two
complaints: the **LOSS_SL drag** and **crypto/stock divergence**. This file
records what was changed, why, and — importantly — what has *not* been proven.

**Status: `stage="incubating"`, `enabled=False`.** It has never traded. It is
scheduled by nobody until someone flips `enabled` in `core/registry.py`.

---

## What changed

| # | Area | Livne | Horizon | Where |
|---|---|---|---|---|
| 1 | Reversal formations | any of 5 | `doji_then_engulfing`, `engulfing` only (conviction ≥ 0.85) | `config.ALLOWED_REVERSAL_FORMATIONS`, `ta/candles.py` |
| 1 | Volume gate | 1.30× | **1.45×** | `config.LTF_VOLUME_SURGE_MULT` |
| 1 | Session filter | none | equities blocked 11:30–13:30 ET (new entries only) | `utils/session.py` |
| 2 | Stop buffer | 0.25× ATR both | crypto **0.45×**, stocks 0.25× | `config.RISK_PROFILES` |
| 2 | TP1 band | 1.0–2.0R both | crypto **0.9–1.6R**, stocks 1.0–2.0R | `config.RISK_PROFILES` |
| 2 | Stop distance cap | 2.5× ATR both | crypto **3.0×**, stocks 2.5× | `config.RISK_PROFILES` |
| 3 | Stagnation exit | none | cut after 6 × 15m bars below 50% progress to TP1 | `paper_trading/tracker.py` |
| 4 | Emergency volume | 3.0× both | crypto **4.5×**, stocks **2.6×** | `config.EMERGENCY_VOLUME_MULT_BY_CLASS` |

### Reasoning, briefly

**Single-bar formations were dropped** (hammer / shooting star / pin bar). They
are the cheapest to print on a 15m chart, carry the lowest conviction weights,
and are the population most associated with the loss drag. Two-bar formations
require the *next* candle to confirm the reversal.

**The volume gate went up, and this is the most aggressive change here.**
Livne's own funnel, over four live cycles, had **62 of 62** rejected candles
come in under 1.30× (median 0.56×). Raising a gate that is already the binding
constraint will reduce trade count sharply. That is the intent — fewer,
higher-momentum entries — but it is the change most likely to need walking back
after a real backtest.

**Crypto and equities were decoupled** because they are not alike: crypto
trades 24/7 with no auction close, wicks further inside a single 15m bar, and
has no circuit breakers. An equity-sized stop cushion sits *inside* crypto's
ordinary noise and gets swept by moves that were never reversals. The wider
stop forces the TP1 band in (or R:R falls under the floor and the funnel
starves) and the distance cap out (or the cap silently undoes the wider stop).

**The emergency exit moved in opposite directions per venue.** It is a multiple
of *recent average* volume. Crypto is spiky around the clock, so a shared 3.0×
fires inside healthy trends and cuts winners early → raised to 4.5×. Equities
concentrate volume into the open and close, where the same ratio is rarer and
more meaningful → lowered to 2.6× so genuine reversals are easier to catch.

**The stagnation exit** cuts an ACTIVE position that has run 6 trigger bars
(90 minutes) without ever travelling 50% of the way to TP1. Progress is the
**running maximum favourable excursion**, persisted on the trade as
`max_progress_r` — not the current bar's extreme. Without that, a position that
ran 60% of the way and pulled back would be cut as "directionless", which is
the opposite of the intent.

Ordering is load-bearing: stop → TP2 → TP1 → stagnation. A bar that took the
stop is a stop; a bar that reached TP1 banks the partial. Grading either as the
milder stagnation outcome would flatter the results.

---

## Backtest coverage — read this before trusting a baseline

`python -m backtest_suite.baseline --start-date … --end-date …` runs **both
watchlists** and reports **asset class × direction**, plus the exit-tag
distribution. Pooling would average away exactly the signal being looked for: a
calibration that helps crypto and hurts equities would read as "no change".

| Calibration | Simulated by the backtest? |
|---|---|
| Reversal whitelist | **yes** — `detect_reversal_formation` is shared |
| Volume gate 1.45× | **yes** — `directional_volume_confirmation` is shared |
| Per-class stop / TP1 / cap | **yes** — `compute_stop`/`compute_targets` read `setup.asset_class` |
| Stagnation exit | **yes** — ported into `backtest_suite/simulator.py` to mirror the live rule |
| Session filter | **no** — lives in the runner's `find_candidates`, which the backtest does not use |
| Emergency exit | **no** — live-only by design, exactly as in Livne (a volume anomaly has no faithful historical replay) |

So a baseline measures four of the six changes. Equity results will be
**optimistic relative to live**, because the backtest still takes midday
entries the live model refuses. Quantifying that gap means either porting the
session filter into the backtest or comparing equity trades by entry hour.

---

## What has NOT been established

- **No live evidence at all.** Zero trades. Every number above is a design
  decision, not a result.
- **No full baseline has been run.** The only backtest executed was a 3-ticker
  smoke run over three weeks — enough to prove the harness works, not enough to
  conclude anything. On that run the stagnation exit fired 8 times (16% of
  exits) and full stops halved, which is the mechanism behaving as designed and
  is *not* evidence that it helps.
- **The 1.45× gate is untested against trade count.** If it starves the funnel,
  the fix is to lower it and re-run, not to loosen something else.
- **Nothing here has been compared against Livne over the same window.**
  `--compare-with livne` prints the command; run both and compare like for like.

## Before enabling

1. Full baseline: both watchlists, ≥ 3 months, `--exec-interval 5m`.
2. The same window through Livne, compared per asset class and direction.
3. Confirm trade count is workable — a strategy that never trades has no
   drawdown and no value.
4. Set `TELEGRAM_ROUTE_HORIZON_CHAT_ID` if it should not share the main channel
   (it currently routes to the shared hub, like every non-dedicated tenant).
5. Then, and only then, `enabled=True` — and re-read the graduation checklist in
   [`CLIENT_ONBOARDING_GUIDE.md`](CLIENT_ONBOARDING_GUIDE.md) before
   `stage="production"`.
