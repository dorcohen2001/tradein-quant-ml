# Strategy Specification Guide & Template

This document is the **input contract** for building a strategy on TRADEIN.
A completed specification is handed to Claude Code, which generates the three
files that hold your logic:

```
models/client_<key>/
├── config.py              your parameters
└── strategy/
    ├── ta_engine.py       your indicators
    └── screener.py        your entry rules
```

Everything else — scheduling, position tracking, Telegram delivery, charting,
catch-up after downtime, backups — is platform infrastructure you inherit.

**Write it so a stranger could implement it without asking you a question.**
That is the bar. Section 3 explains how to hit it.

| Section | Purpose |
|---|---|
| [1. Reference model](#section-1--reference-model-the-livne-benchmark) | A real, running strategy specified end to end |
| [2. Specification sheet](#section-2--client-strategy-specification-sheet) | Copy, fill in, send back |
| [3. Best practices](#section-3--best-practices-for-strategy-design) | How to write rules that survive contact with code |

---

# SECTION 1 — REFERENCE MODEL: THE LIVNE BENCHMARK

Livne is a live model on this platform. Every number below is read from its
actual configuration, not an illustration. Use it as a worked example of the
level of detail a specification needs.

**In one sentence:** find where price is reacting to structure that matters on
high timeframes, then wait for a specific reversal candle with real volume
behind it on a 15-minute chart before entering.

## 1.1 Identity & Market Scope

| | |
|---|---|
| Asset classes | US equities (70 curated large/mid-caps + sector ETFs) and crypto (34 USDT majors) |
| Universe policy | Static curated watchlist, not a screener over the whole market |
| Liquidity floors | Stocks: $7.5M 7-day average dollar volume, $1B market cap. Crypto: $15M |
| Scan cadence | Every **2 minutes** |
| Max new setups | **2 per asset class per cycle** |
| History required | ≥ 25 daily bars before a symbol is evaluated |

**Timeframe ladder** — four tiers, each with a distinct job:

| Tier | Timeframes | Job |
|---|---|---|
| Context | daily | Overall regime and directional bias |
| Discovery | weekly, daily, 12h | Where the structural levels actually live |
| Refinement | 4h, 3h, 1h | Tighten those zone edges to something tradeable |
| Execution | **15m trigger**, 5m + 1h confirm | The entry decision itself |

> The cadence must be far faster than the trigger bar. Livne triggers on 15m and
> scans every 2 minutes — roughly seven looks per bar. A strategy polled once per
> bar gets one chance to see each setup and will miss most of them.

## 1.2 Trend & Regime Filter (higher timeframes)

- **Primary trend benchmark: the 200-period MA.** 150 is the secondary filter.
  EMA 20/50 are explicitly *not* trend inputs here.
- **Alignment vote:** of the three execution timeframes (1h, 15m, 5m), at least
  **2 of 3** must agree with the 200-MA direction. Not all three — that was too
  strict and starved the funnel.
- **Zone discovery:** swing highs/lows, pivots and order blocks are mapped
  top-down on weekly → daily → 12h.
- **Proximity gates:** price must be within **1.25%** of a discovery zone, and
  within **0.5%** once refined on 4h/3h/1h.
- **200 EMA as structure:** a touch within **0.4%** of the 200 EMA on a
  refinement timeframe counts as a barrier in its own right (+0.15 score).
- **Macro regime gate:** the direction must be permitted by the current regime
  (VIX and SPY/QQQ for stocks; USDT.D and total market cap for crypto). A LONG
  in a risk-off crypto regime is rejected before any of the above runs.

## 1.3 Entry Triggers (execution timeframe)

All of the following must hold. Every one is an AND.

**1 — A direction-matching reversal candle** on the 15m trigger, within the last
**3 bars**. A volume climax or structure shift alone is *not* an entry:

| Formation | Direction | Conviction |
|---|---|---|
| Doji then engulfing | either | 1.00 |
| Engulfing | either | 0.85 |
| Hammer | long | 0.75 |
| Shooting star | short | 0.75 |
| Pin bar rejection (wick ≥ 60% of range) | either | 0.65 |

**2 — Mandatory directional volume proof.** The reversal candle's volume must be
≥ **1.30×** its 20-bar average, *on the correct side*:

- **LONG** — buyer surge on the bullish reversal candle itself.
- **SHORT** — seller expansion on the rejection candle, ideally preceded by
  buyer exhaustion (prior 5-bar push averaging < 0.95× baseline).

Exhaustion is measured and scored but is not mandatory — demanding both would
reject sharp reversals that turn without a slow bleed first.

**3 — Location.** Price is at a refined HTF zone, *or* reacting at the 0.5/0.618
Fibonacci level (a wick within 0.5% of the level plus a close back away, within
the last 5 bars). Strict containment in the 0.5–0.786 golden zone is not required.

**Confluence (scored, not gating):** RSI(14) divergence, unmitigated order
blocks, HTF volume, liquidity skew from order-book depth.

**Score weighting:** TA confluence 35, volume 25, relative strength 25,
catalyst safety 15.

## 1.4 Risk Management & Exit Architecture

**Stop loss** — structural, with an ATR-scaled buffer:

```
buffer = max(0.25 × ATR(15m), price × 0.005%)
stop   = structural level ∓ buffer
capped so that |entry − stop| ≤ 2.5 × ATR(15m)
```

The buffer is ATR-scaled rather than a flat percentage because a flat 0.5%
cushion on a $200 stock is a full dollar while 15m ATR is ~$0.15 — the buffer
alone exceeded a realistic intraday target and failed every R:R check.

**Two-tier exit:**

| | Target | Action |
|---|---|---|
| TP1 | 1.0–2.0R (nearest structural level in band) | Close **50%**, move stop to breakeven |
| TP2 | 2.0R default, capped at 4.0R | Close the remainder |

TP2 must clear TP1 by at least **0.75R**. Without that floor both legs could
resolve to the same price — observed live — and a two-tier exit whose tiers are
identical is not a two-tier exit.

**Hard R:R floor: 1:1.** Never risk more than the reward. A high-certainty 1:1–1:2
intraday setup is a legitimate trade; insisting on 1:3 starved the funnel to zero.

**Emergency exit:** a 3× volume anomaly against the position within 15 minutes
closes it early. Live only — not replayed historically.

**Fills:** 0.15% adverse slippage modelled on stop fills. Stops fill worse, never
better.

## 1.5 Signal Validity & Invalidation

| | |
|---|---|
| Pending expiry | **6 hours** — an unfilled setup goes stale and is cancelled |
| Fill test | Entry-zone **overlap** with the bar's traded range, not "last price is inside the zone" |
| Earnings blackout | Stocks with earnings within 48h are excluded |
| Duplicate guard | One open position per symbol at a time |

**Lifecycle:** `PENDING → ACTIVE → PARTIAL_TP1 → WIN_TP2 / LOSS_SL_BE`, or
`ACTIVE → LOSS_SL` if the stop hits before TP1. `EMERGENCY_EXIT` can close from
either open state. `EXPIRED` never entered and carries no P&L.

> Expiry is measured in **hours, not days**, because the horizon is intraday. A
> 15m-triggered setup left pending for five days would fill against a completely
> different market.

---

# SECTION 2 — CLIENT STRATEGY SPECIFICATION SHEET

Copy everything between the lines, fill in every `[BRACKET]`, and send it back.
Write `N/A` where something genuinely does not apply — an empty field reads as
"forgotten" and will come back to you as a question.

```text
===================================================================
TRADEIN CLIENT STRATEGY SPECIFICATION SHEET
===================================================================

1. IDENTITY & SCHEDULE
- Strategy Name:            [e.g., Alpha Momentum]
- Client/Tenant Key:        [lowercase_key, e.g., alpha_momentum]
- One-line description:     [how you'd describe it in a sentence]
- Asset Class:              [US Equities / Crypto / Both]
- Watchlist / Universe:     [explicit ticker list, or "same as Livne default"]
- Scan Interval:            [e.g., 2m, 5m, 15m] (must be well under the base TF)
- Base Candlestick TF:      [e.g., 15m, 1h, 1d]  (the bar entries trigger on)
- Max new setups per cycle: [e.g., 2 per asset class]
- Trading hours:            [US regular hours only / 24-7 / specific window]

2. HIGHER-TIMEFRAME CONTEXT        (write "None" for a single-timeframe strategy)
- Macro Timeframe:          [e.g., 1d, 4h, None]
- Trend Direction Filter:   [e.g., Close > EMA200 on the daily]
- Structural Levels Used:   [e.g., swing highs/lows, order blocks, None]
- Zone Proximity Tolerance: [e.g., within 1.0% of the level]
- Regime Constraints:       [e.g., longs only when SPY > its 200 EMA; None]

3. TECHNICAL INDICATORS REQUIRED
- Indicator 1:  [e.g., EMA — lengths 20, 50, 200 — applied to Close]
- Indicator 2:  [e.g., RSI — length 14 — overbought 70, oversold 30]
- Indicator 3:  [e.g., ATR — length 14 — used for stop sizing]
- Indicator 4:  [e.g., Volume SMA — length 20 — surge multiplier 1.5x]
- Indicator 5:  [add as needed, or N/A]

4. ENTRY LOGIC        (each condition must be checkable against one bar of data)
- Long Trigger — ALL of:
  1. [Trend/regime state, e.g., EMA20 > EMA50 AND Close > EMA200]
  2. [Pullback/oscillator state, e.g., RSI(14) < 40 within the last 3 bars]
  3. [Trigger event, e.g., bullish engulfing closing above the prior high]
  4. [Volume/confluence requirement, e.g., volume >= 1.5x its 20-bar average]
- Short Trigger — ALL of:
  1. [...]
  2. [...]
  3. [...]
  4. [...]
- Are shorts traded at all?  [Yes / Long-only]
- Any hard exclusions?       [e.g., no entries within 48h of earnings]

5. EXIT & RISK RULES
- Stop Loss (SL):          [e.g., lowest low of the last 5 bars − 0.5 × ATR(14)]
- Stop distance cap:       [e.g., never more than 3 × ATR; or None]
- Take Profit 1 (TP1):     [e.g., 1.5R — close 50% of the position]
- Action after TP1:        [e.g., move stop to breakeven / trail by 1 ATR / none]
- Take Profit 2 (TP2):     [e.g., 3.0R — close the remainder]
- Minimum R:R to accept:   [e.g., 1:2.0 — setups below this are rejected]
- Signal Expiry:           [e.g., cancel a pending setup after 4 unfilled bars]
- Emergency exit rule:     [e.g., close on a 3x volume move against us; None]
- Max concurrent positions:[e.g., 5 total, 1 per symbol]

6. TELEGRAM ALERT PREFERENCES
- Delivery Mode:           [Primary Shared Hub / Dedicated Isolated Channel]
- If dedicated: channel or group?   [channel = broadcast, group = replies]
- Chart snapshot on alerts:         [Yes / No]
- Show exact Entry/SL/TP prices:    [Yes / No]
- Show R:R per target:              [Yes / No]
- Show position size illustration:  [Yes / No — % risk of what account size?]
- Daily summary:                    [Yes / No — at what local time?]

7. ANYTHING ELSE
- [Rules that did not fit above, edge cases you already know about, behaviour
   you have seen in manual trading that the rules above do not yet capture]
===================================================================
```

---

# SECTION 3 — BEST PRACTICES FOR STRATEGY DESIGN

## 3.1 Write conditions a computer can evaluate

Every condition must resolve to true or false against a specific bar. If two
people could read it differently, it will be implemented differently.

| Ambiguous ❌ | Unambiguous ✅ |
|---|---|
| "Buy the dip in an uptrend" | "EMA20 > EMA50 AND RSI(14) < 40 on the trigger bar" |
| "Strong volume" | "Volume ≥ 1.5 × the 20-bar volume SMA" |
| "Near support" | "Low within 0.5% of the 20-bar swing low" |
| "Wait for confirmation" | "The next bar closes above the trigger bar's high" |
| "Exit if momentum fades" | "Close 50% if RSI(14) crosses back below 50" |

Name the **indicator, its length, its input series, and the comparison**. "RSI is
low" is three unanswered questions.

## 3.2 State the bar and the moment

A rule needs a timeframe *and* a timing. "RSI < 30" leaves open:

- On which timeframe — the trigger bar or a higher one?
- On the **closed** bar, or the one still forming?
- On the current bar only, or anywhere in the last N?

Livne says: *"a reversal formation within the last 3 bars of the 15m trigger
timeframe."* That is checkable. Prefer closed bars — a condition that is true
mid-bar and false at close will fire inconsistently depending on poll timing.

## 3.3 Never look ahead

Every rule may use only data available **at that bar's close**. This sounds
obvious and is the single most common way a backtest looks brilliant and the live
version does not: rules like "enter at the low of the day" or "when the bar turns
out to be the swing low" need information that does not exist yet.

If you cannot compute it live in real time, it cannot be a rule.

## 3.4 Define risk before reward

Order of specification matters, because everything else derives from it:

1. **Where is the stop?** Anchor it to structure — a swing point, a level, an ATR
   distance. Never a round number picked because it "feels right".
2. **What is 1R?** `|entry − stop|`. Every target is a multiple of it.
3. **What R:R do you accept?** Below this floor the setup is rejected, however
   good it looks.

Targets expressed in R survive across instruments and volatility regimes. Targets
expressed in dollars or fixed percentages do not.

## 3.5 Give the stop room the instrument actually needs

A fixed percentage buffer breaks across price scales. On a $200 stock a 0.5%
cushion is $1.00 while 15m ATR is ~$0.15 — six times the noise it is meant to
absorb, which pushes every target out of reach and fails the R:R gate.

Scale buffers by ATR, and keep a small absolute floor for when ATR is
unavailable or the instrument is unusually quiet.

## 3.6 Say what invalidates the setup

Entries get all the attention; the invalidation rules decide the P&L:

- **Expiry** — how long may a pending setup wait before it is stale? Match the
  holding horizon: hours for intraday, days for swing.
- **Fill definition** — did price *trade through* the zone, or must it *close*
  inside? Zones are narrow and price often passes through between polls. Livne
  uses range overlap, and switching to a point-in-time check silently
  under-reported fills and skewed every win-rate number.
- **Same-bar ambiguity** — if one bar covers both your stop and your target, OHLC
  cannot say which came first. The platform assumes the **stop**. If you want a
  different convention, say so.

## 3.7 Prefer few strong conditions over many weak ones

Each AND multiplies rarity. Livne is four hard gates plus a scored confluence
layer; adding a fifth gate roughly halved signal count for no measurable win-rate
gain. If you want a factor to *matter* without blocking, make it a **score
input** rather than a gate — that is exactly what the scoring weights are for.

## 3.8 Start with the rules you already trade

The best specification is a description of what you already do manually, written
precisely. Do not invent a strategy for the document. If a rule exists because
you have watched it work, say so — that context is more valuable than a tidier
rule you have never traded.

## 3.9 Expect the first version to be wrong

Every strategy here is scaffolded as `stage="incubating"`: it runs on its own
schedule, alerts to its own destination, and is **excluded from executive
reporting** until it has earned a place. Expect to refine parameters after
seeing live behaviour.

What you should *not* do is change rules mid-trial — every change restarts the
sample. See the graduation checklist in
[`CLIENT_ONBOARDING_GUIDE.md`](CLIENT_ONBOARDING_GUIDE.md).

## 3.10 Checklist before you send it

- [ ] Every indicator has a length and an input series
- [ ] Every threshold is a number, not an adjective
- [ ] Every condition names its timeframe
- [ ] Long and short rules are stated separately (or shorts explicitly excluded)
- [ ] The stop is anchored to something observable
- [ ] Targets are expressed in R
- [ ] A minimum R:R is stated
- [ ] Pending-setup expiry is stated
- [ ] No rule needs future information
- [ ] Scan interval is meaningfully shorter than the base timeframe

---

## What happens next

1. You send the completed sheet.
2. It is scaffolded with `scripts/create_client_model.py` — an isolated tenant
   with its own trade store, journal, charts and Telegram route.
3. Your rules are implemented in `strategy/screener.py` and
   `strategy/ta_engine.py`; parameters land in `config.py`.
4. Dry runs and the full test suite verify nothing else regressed.
5. It runs as `incubating` and alerts you on every signal.
6. When the graduation checklist is met, it is promoted to `production`.

Full operator walkthrough: [`CLIENT_ONBOARDING_GUIDE.md`](CLIENT_ONBOARDING_GUIDE.md).
