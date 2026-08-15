"""Central configuration: watchlists, thresholds, scoring weights, API keys."""
import os

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Repository anchors. Every path below is derived from these, never from the
# caller's cwd -- this model is launched both directly and as a subprocess,
# and Steve imports it from a third directory again.
#
#     <repo>/models/livne/config.py  ->  REPO_ROOT = <repo>
#
# All MUTABLE state lives under <repo>/runtime/ and never inside a source
# package: trade stores, journals, funnel exports, caches and rendered charts
# used to be scattered through the code tree, which made "what is code" vs
# "what is data" ambiguous and put live positions at risk during refactors.
# ---------------------------------------------------------------------------
MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(MODEL_DIR, "..", ".."))
RUNTIME_DIR = os.path.join(REPO_ROOT, "runtime")

# Secrets live in a single repo-root .env shared by both models (previously
# duplicated per-codebase, which let the two drift apart silently).
load_dotenv(os.path.join(REPO_ROOT, ".env"))

# ---------------------------------------------------------------------------
# Model identity + derived runtime paths come from `core.registry`, the single
# place that knows which models exist. Everything below that used to be a
# hand-written path literal (`.../trades_livne.json`) is now derived from
# MODEL_KEY, so a third model needs no edit here at all.
#
# `_bootstrap` must be imported BEFORE `core.*`: this module is imported
# directly by main.py and by the Steve analyzer, neither of which has
# necessarily put the repo root on sys.path yet.
#
# The paths this produces are byte-identical to the literals used before --
# they hold live paper-trading state, and a one-character change would orphan
# that history behind a freshly-created empty store.
# ---------------------------------------------------------------------------
import _bootstrap  # noqa: E402,F401  -- puts repo root on sys.path for core.*

from core.registry import get_model  # noqa: E402

MODEL_KEY = "livne"
_MODEL = get_model(MODEL_KEY)

# ---------------------------------------------------------------------------
# Watchlists (used because full-market screening via free APIs is impractical
# to run daily without paid data feeds). Extend these lists as needed.
#
# This is a curated (not dynamically-fetched) universe of liquid mid/large-cap
# names — S&P 500 / NASDAQ-100 leaders plus high-volume growth/momentum
# stocks — deliberately kept static rather than scraped at runtime (e.g. from
# Wikipedia) to avoid adding a fragile network dependency to every scan cycle.
# The MIN_STOCK_MARKET_CAP / MIN_STOCK_DOLLAR_VOLUME_SMA20 filters below are
# the real safety net against penny stocks or micro-caps slipping in.
# ---------------------------------------------------------------------------
WATCHLIST_STOCKS = [
    # Mega-cap tech / software / semis
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO", "ORCL",
    "ADBE", "CRM", "NFLX", "AMD", "INTC", "QCOM", "TXN", "MU", "CSCO", "IBM",
    "NOW", "INTU", "PANW", "SNPS", "CDNS", "KLAC", "LRCX", "AMAT", "ASML",
    "ANET", "ARM", "SMCI",
    # Healthcare / pharma
    "LLY",
    # Energy
    "CVX",
    # Industrials
    "BA", "CAT", "GE", "HON", "UNP", "RTX", "LMT",
    # High-volume growth / momentum / thematic (incl. crypto-adjacent & space)
    "PLTR", "COIN", "MSTR", "SOFI", "CIFR", "ASTS", "RIOT", "MARA", "HOOD",
    "RBLX", "SNOW", "DDOG", "NET", "CRWD", "ZS", "SHOP", "UBER", "ABNB",
    "DASH", "RIVN", "LCID", "NIO",
    # Broad index / sector ETFs
    "SPY", "QQQ", "IWM", "ARKK", "DIA", "XLF", "XLE", "SMH",
]

WATCHLIST_CRYPTO = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "POLUSDT", "DOTUSDT", "LTCUSDT",
    "NEARUSDT", "ARBUSDT", "OPUSDT", "TRXUSDT", "ATOMUSDT", "FILUSDT",
    "APTUSDT", "SUIUSDT", "SHIBUSDT", "UNIUSDT", "ICPUSDT", "ETCUSDT",
    "XLMUSDT", "HBARUSDT", "INJUSDT", "RENDERUSDT", "PEPEUSDT", "WIFUSDT",
    "TAOUSDT", "ONDOUSDT", "AAVEUSDT", "BCHUSDT",
]

STOCK_BENCHMARKS = {"default": "SPY", "tech": "QQQ"}
CRYPTO_BENCHMARK = "BTCUSDT"

# ---------------------------------------------------------------------------
# Screening thresholds
# ---------------------------------------------------------------------------
# Liquidity is gated on a rolling SMA of dollar volume (Close * Volume) rather
# than a single day's reading, so one noisy bar can't flip a name in or out.
# No static minimum stock price -- a hard $5 floor was dropped in favor of
# the dollar-volume/market-cap filters actually doing the liquidity work.
#
# INTRADAY: lookback cut 20 -> 7 trading days. On an intraday horizon what
# matters is whether a name is liquid and active *this week* -- a 20-day mean
# smooths straight over the volume shift that makes a name tradeable today.
DOLLAR_VOLUME_SMA_LOOKBACK = 7
# Each is a single tunable value chosen as the midpoint of the requested
# band -- the code needs one number to gate on. Adjust directly if you want
# to run nearer either edge of the range.
MIN_STOCK_DOLLAR_VOLUME_SMA20 = 7_500_000    # requested band: $5M-$10M
MIN_CRYPTO_DOLLAR_VOLUME_SMA20 = 15_000_000  # lowered from $35M -- that floor was dropping prominent, liquid
                                              # altcoins (e.g. ADAUSDT ~$18M SMA20) before Stage A ever got a look.
                                              # $15M (top of a $10M-$15M ask) keeps more of the original
                                              # manipulation-resistance margin than $10M would; still excludes
                                              # genuinely thin pairs (e.g. OPUSDT ~$2M) by design.
MIN_STOCK_MARKET_CAP = 1_000_000_000  # $1B floor; excludes micro-caps
VOLUME_SPIKE_MULT = 1.5               # default multiplier for ta_engine.volume_spike() when a caller doesn't override it
RS_LOOKBACK_DAYS = 5                  # relative strength window
EARNINGS_BLACKOUT_HOURS = 48          # exclude stocks with earnings this soon
TOP_N_ASSETS = 2                      # number of setups produced per run

# Minimum daily bars required before a ticker is evaluated. INTRADAY: cut
# 60 -> 25. Daily bars now only supply regime/context and the most recent
# structure shift or consolidation breakout; ~5 weeks is enough for that,
# and demanding a full quarter needlessly excluded names.
MIN_DAILY_HISTORY_BARS = 25

# ---------------------------------------------------------------------------
# Risk/reward floors -- INTRADAY recalibration.
#
# The old swing floors (TP1 >= 1:2, TP2 >= 1:3) are unreachable on a 15m
# trigger and would have starved the funnel to near-zero signals. The hard
# rule is now simply: never risk more than the reward. A high-certainty setup
# offering 1:1-1:2 is a legitimate intraday trade and is dispatched, not
# discarded; TP2 still reaches for 1:2+ whenever structure actually allows it.
# ---------------------------------------------------------------------------
MIN_RISK_REWARD = 1.0   # HARD floor on the final target -- reject anything below 1:1
TP2_TARGET_RR = 2.0     # what TP2 aims for when no structural level provides better
TP2_MAX_RR = 4.0        # ceiling on TP2. Without it, TP2 inherits the zone-timeframe swing
                        # extreme -- an entire multi-month leg away -- and an intraday trade
                        # can never reach it (observed 7.5R on ETH, 14.3R on BTC).

# TP2 must clear TP1 by at least this much R. Without it the two targets could
# land on the SAME price: whenever structural resistance sat beyond TP1_MAX_RR,
# TP1 clamped to exactly 2.0R while TP2's fallback was also 2.0R
# (max(MIN_RISK_REWARD, TP2_TARGET_RR)). Both printed 2.00R at an identical
# price -- observed live on LRCX and SMH -- and validate_min_rr could not catch
# it because both legs individually cleared their floors. A two-tier exit whose
# tiers are the same price is not a two-tier exit: the runner would book the
# TP1 partial and immediately close the remainder at the same level.
#
# 0.75R is chosen so the clamped case (TP1 = 2.0R) yields TP2 = 2.75R, landing
# inside the requested 2.5R-3.0R band WITHOUT raising TP1's floor -- which must
# stay at 1.0R so high-certainty 1:1-1:2 setups are still dispatched.
TP_MIN_SEPARATION_RR = 0.75

# ---------------------------------------------------------------------------
# Moving averages -- trend benchmark
# ---------------------------------------------------------------------------
# EMA20/EMA50 are deprecated as trend inputs. 200 is the absolute primary
# trend benchmark (especially for daily/intraday decisions -- see the ITF
# 200MA-alignment vote in ta.mtfa); 150 is the secondary filter. Single
# source of truth: ta_engine.add_moving_averages/determine_trend, and every
# "200MA" reference across ta.mtfa and backtest_suite.run_backtest, all read
# these two constants.
PRIMARY_MA_PERIOD = 200
SECONDARY_MA_PERIOD = 150

# ---------------------------------------------------------------------------
# Multi-Timeframe Analysis (MTFA) -- INTRADAY mapping ("Option B")
#
# Target holding period is minutes to a few hours, so the whole ladder moved
# down. The previous mapping (Weekly/Monthly context, 12H/8H/4H vote, 4H
# trigger) could not produce an intraday trade at all: the trigger candle
# alone took four hours to close.
#
#   CONTEXT      daily              -- regime / overall direction bias only
#   DISCOVERY    weekly, daily, 12h -- where the key S/R, order blocks and
#                                      pivots actually live (top-down)
#   REFINE       4h, 3h, 1h         -- precision-tighten those zone edges
#   EXECUTION    15m trigger, with 5m + 1h confirmation
#
# Binance intervals are native except 3h, which is resampled from 1h.
# yfinance has no 12h/4h/3h either, so those are resampled from 1h as well;
# 15m/5m are fetched natively (60-day history cap, ample for a 200-period MA).
# ---------------------------------------------------------------------------
ZONE_DISCOVERY_TFS = ["weekly", "daily", "12h"]
ZONE_REFINE_TFS = ["4h", "3h", "1h"]
LTF_TRIGGER_TF = "15m"
LTF_CONFIRM_TFS = ["5m", "1h"]

# EMA200 alignment vote now runs on the execution ladder rather than 12H/8H/4H.
LTF_ALIGNMENT_TFS = ["1h", "15m", "5m"]
LTF_MA_MIN_ALIGNED = 2  # how many of the 3 must agree with PRIMARY_MA_PERIOD trend alignment

# Per-asset-class interval strings. `None` => build by resampling 1h bars.
TF_INTERVALS = {
    "crypto": {"weekly": "1w", "daily": "1d", "12h": "12h", "4h": "4h",
               "3h": None, "1h": "1h", "15m": "15m", "5m": "5m"},
    "stocks": {"weekly": "1wk", "daily": "1d", "12h": None, "4h": None,
               "3h": None, "1h": "1h", "15m": "15m", "5m": "5m"},
}

RSI_PERIOD = 14
RSI_PIVOT_LEFT = 16
RSI_PIVOT_RIGHT = 5
DIVERGENCE_MIN_BAR_GAP = 5     # min bars between compared pivots
DIVERGENCE_MAX_BAR_GAP = 60    # max bars between compared pivots

OB_LOOKBACK_BARS = 80          # bars scanned for order blocks on each zone timeframe
OB_IMPULSE_ATR_MULT = 2.0      # impulse candle must exceed this multiple of ATR
OB_STRUCTURE_LOOKBACK = 20     # bars used to detect the swing high/low the impulse must break

# INTRADAY: proximity tightened 4.0% -> 1.25%. At intraday scale +/-4% spans
# an entire session's range, so nearly every bar counted as "at a zone" and
# the gate stopped discriminating at all.
HTF_ZONE_PROXIMITY_PCT = 0.0125

# Discovery zones are coarse; refinement timeframes must confirm price is at
# the zone within a tighter band before it counts as a precision-refined zone.
ZONE_REFINE_PROXIMITY_PCT = 0.005

# Minimum bars required on a zone/refine timeframe before it is used. INTRADAY:
# the old 60-bar weekly floor is gone -- only the most recent structural swings
# and pivots matter now, not deep history.
MIN_ZONE_HISTORY_BARS = 30

# EMA200 as a structural anchor. A touch/reaction at the 200 EMA on a refine
# timeframe is treated as a high-certainty barrier in its own right, not just
# an alignment vote -- price rarely slices it cleanly on first contact.
EMA200_ANCHOR_PROXIMITY_PCT = 0.004
EMA200_ANCHOR_SCORE_BONUS = 0.15

# Zone-timeframe volume confluence -- a *scoring* input (see
# screener._score_candidate), NOT a gate. Institutional absorption at real
# structure often prints on near-average or even red bars (a "low-volume
# retest"); the mandatory directional volume proof lives on the execution
# timeframe instead (see LTF_VOLUME_* below).
HTF_VOLUME_CONFLUENCE_MULT = 1.00
HTF_VOLUME_CONFLUENCE_LOOKBACK = 20

# ---------------------------------------------------------------------------
# MANDATORY directional volume confirmation on the 15m trigger candle.
#
# This is a hard gate and it is DIRECTIONAL -- a raw volume spike is no longer
# sufficient, because a spike on the wrong side of the market is exactly what
# a failed reversal looks like:
#
#   LONG  -> buyer volume surge: the bullish reversal candle itself must print
#            expanded volume vs its N-bar average.
#   SHORT -> seller volume expansion on the rejection candle, ideally preceded
#            by buyer exhaustion (volume drying up into the rally high).
#
# Exhaustion is measured and reported (it feeds the score and the thesis text)
# but only the expansion leg is mandatory -- requiring both would reject
# legitimate sharp reversals that turn without a slow bleed first.
# ---------------------------------------------------------------------------
LTF_VOLUME_SURGE_MULT = 1.30      # reversal candle volume vs its N-bar SMA -- HARD gate
LTF_VOLUME_LOOKBACK = 20
LTF_EXHAUSTION_LOOKBACK = 5       # bars of the prior push checked for fading volume
LTF_EXHAUSTION_MAX_RATIO = 0.95   # prior-push avg volume below this multiple of baseline = exhaustion

# ---------------------------------------------------------------------------
# Candlestick reversal certainty (high weight).
#
# A volume climax or a market-structure shift ALONE no longer qualifies as a
# trigger -- an explicit, direction-matching reversal formation is required:
#
#   LONG  -> doji followed by a strong bullish engulfing (highest conviction),
#            a bullish engulfing, a hammer, or a high-conviction pin bar.
#   SHORT -> doji followed by a bearish engulfing, a bearish engulfing,
#            a shooting star, or a high-conviction pin bar.
#
# Volume climax / structure shift are retained as *confluence* flags that add
# conviction score, which is how they now earn their keep.
# ---------------------------------------------------------------------------
REVERSAL_PIN_BAR_WICK_RATIO = 0.60   # stricter than the old 0.50 -- "high conviction" pin bar
REVERSAL_SCAN_BARS = 3               # how many recent trigger-TF bars are scanned for the formation
REVERSAL_CONVICTION = {              # score contribution per formation, 0-1
    "doji_then_engulfing": 1.00,
    "engulfing": 0.85,
    "hammer": 0.75,
    "shooting_star": 0.75,
    "pin_bar_rejection": 0.65,
}

# Stage B: the ITF trigger no longer requires price strictly contained inside
# the [0.5, 0.786] Fib golden zone -- a clear support/resistance reaction
# (touch + close back away) at the 0.5 or 0.618 level also qualifies.
FIB_REACTION_TOLERANCE_PCT = 0.005  # how close a wick must come to the level to count as a "touch"
FIB_REACTION_LOOKBACK_BARS = 5      # recent bars checked for the touch-and-react

# ---------------------------------------------------------------------------
# Braking / Reversal Event detector (ta.mtfa.detect_braking_event) -- Stage 7's
# multi-timeframe, multi-condition zoom-in. Scans the last BRAKING_WINDOW_BARS
# bars (not just the single trigger bar) on each checked timeframe (4H/1H
# live; the daily-bar proxy in backtest) for ANY of: a pin-bar rejection, a
# classic candlestick pattern, a volume-climax/absorption bar, or a Market
# Structure Shift -- reused for both Stage 7 entry confirmation and the
# backtest simulator's dynamic exit (called with the opposite direction).
# ---------------------------------------------------------------------------
BRAKING_WINDOW_BARS = 5          # scan the last 3-5 bars, not just the current one
BRAKING_PIN_BAR_WICK_RATIO = 0.5 # rejection wick must occupy >= 50% of the candle's total range
BRAKING_VOLUME_CLIMAX_MULT = 1.5 # volume climax/absorption: volume vs its N-bar average
BRAKING_SMALL_BODY_RATIO = 0.3   # "small body" threshold that makes a high-volume bar count as absorption
BRAKING_STRUCTURE_LOOKBACK = 10  # bars checked for a fresh Market Structure Shift (break of the prior minor swing)

# ---------------------------------------------------------------------------
# Risk management / multi-tier take-profit
# ---------------------------------------------------------------------------
# INTRADAY: TP1 band recalibrated from 1:2-1:3 down to 1:1-1:2. A 15m trigger
# rarely offers 2R to the first structural level; insisting on it rejected
# essentially every valid intraday setup.
TP1_MIN_RR = 1.0    # TP1 must clear at least 1:1
TP1_MAX_RR = 2.0    # TP1 targets the 1:1-1:2 band when a structural level allows
TP1_CLOSE_PCT = 0.5 # fraction of position closed at TP1
ACCOUNT_RISK_PCT = 0.01  # illustrative %-of-account risk used only to label alert size
ILLUSTRATIVE_ACCOUNT_SIZE = 10_000  # purely for the alert's informational position-size note; no real capital is tracked

# ---------------------------------------------------------------------------
# Liquidity-sweep stop buffer (ATR-scaled).
#
#     buffer = max(STOP_ATR_BUFFER_MULT * ATR(trigger_tf), price * STOP_MIN_TICK_PCT)
#
# Replaces the old flat 0.5% cushion, which was catastrophically oversized
# intraday: on a $200 stock it placed the stop a full dollar beyond structure
# while a typical 15m ATR is ~$0.15, so the buffer alone exceeded a realistic
# intraday target and forced every setup to fail the R:R gate.
#
# Scaling by ATR on the trigger timeframe makes the cushion proportional to
# the noise actually present right now, which is the thing that produces
# liquidity sweeps of a structural level. The tick floor keeps the buffer
# non-zero when ATR is unavailable or the instrument is very quiet.
# ---------------------------------------------------------------------------
STOP_ATR_BUFFER_MULT = 0.25    # k in the formula above (requested band: 0.20-0.30)
# 0.005% of price -- a genuine FLOOR, deliberately set an order of magnitude
# below the ATR term so it binds only when ATR is unavailable or the
# instrument is unusually quiet. At 0.02% it was actually exceeding
# 0.25 x ATR(15m) on both a $200 stock and BTC, which silently replaced the
# ATR scaling with a flat percentage -- the exact behaviour being removed.
# 0.005% of $200 is one cent, i.e. a real minimum tick.
STOP_MIN_TICK_PCT = 0.00005

# Hard cap on stop DISTANCE (not the buffer): min(structural distance, N * ATR).
# INTRADAY: 5.0 -> 2.5. Structural levels are now sourced from 4H/1H zones
# while ATR is measured on 15m, so an un-tightened cap would let a 4H swing
# level place a stop many multiples of intraday range away.
STOP_MAX_ATR_MULT = 2.5

# ---------------------------------------------------------------------------
# Dynamic trend-reversal + volume exit (backtest daily-bar early-exit signal).
# Trades have no fixed holding-period cutoff -- this is one of the three ways
# a backtest position can close (alongside SL and TP); see simulator.py.
# ---------------------------------------------------------------------------
DYNAMIC_EXIT_VOLUME_MULT = 1.75       # volume vs N-day SMA to count as "abnormal" (spec range was 1.5x-2.0x)
DYNAMIC_EXIT_VOLUME_LOOKBACK = 20     # volume SMA window
DYNAMIC_EXIT_CONTEXT_BARS = 60        # trailing bars fed into the per-bar reversal check (bounds the cost of
                                       # re-checking every bar of a long-running, no-longer-time-limited trade;
                                       # comfortably covers detect_braking_event's own lookbacks plus MACD(12,26,9)

# ---------------------------------------------------------------------------
# Liquidity / order-book (crypto: Binance + Bybit public depth; stocks: volume
# profile proxy since no free Level-2 book exists for equities)
# ---------------------------------------------------------------------------
BYBIT_BASE_URL = "https://api.bybit.com"
ORDERBOOK_DEPTH_LIMIT = 500
LIQUIDITY_CLUSTER_BAND_PCT = 0.002   # bucket width as % of price when clustering resting size
LIQUIDITY_PROXIMITY_PCT = 0.05       # only clusters within +/-5% of price are considered
LIQUIDITY_TOP_CLUSTERS = 5
VOLUME_PROFILE_LOOKBACK = 120        # bars of history used for the stock volume-profile proxy
VOLUME_PROFILE_BUCKETS = 40

# ---------------------------------------------------------------------------
# Correlation engine (USDT.D / TOTAL market cap / SPY-QQQ)
# ---------------------------------------------------------------------------
MACRO_SNAPSHOT_STORE = os.path.join(RUNTIME_DIR, "stores", "market_snapshots.json")
MACRO_SNAPSHOT_MAX_AGE_DAYS = 30     # prune snapshots older than this
USDT_D_TREND_LOOKBACK_HOURS = 24
TOTAL_MCAP_TREND_LOOKBACK_HOURS = 24
CORRELATION_TREND_MIN_PCT = 0.5      # minimum %% move over the lookback to call a trend (else Neutral)

# ---------------------------------------------------------------------------
# Emergency reversal exit (live/paper-trading only; not simulated in backtest
# since historical intraday volume-anomaly candles aren't stored)
# ---------------------------------------------------------------------------
EMERGENCY_VOLUME_MULT = 3.0
EMERGENCY_LOOKBACK_MINUTES = 15
EMERGENCY_CANDLE_LOOKBACK = 20  # bars of recent intraday history fetched for the check

# ---------------------------------------------------------------------------
# Live funnel diagnostics (screener.funnel_diagnostics) -- per-ticker gate
# trace exported to Excel at the end of every live scan cycle.
# ---------------------------------------------------------------------------
FUNNEL_LOG_DIR = os.path.join(RUNTIME_DIR, "funnel_logs")
# Namespaced per model so the shared directory never collides and retention
# prunes per prefix -- derived from the key rather than written out again.
FUNNEL_LOG_PREFIX = _MODEL.funnel_prefix
# Retention: keep only the newest N reports PER MODEL in that shared
# directory. Unbounded before -- at 4 cycles/hour x 2 models this directory
# grew by ~200 files/day forever. Pruning is per-model-prefix so neither
# model can ever evict the other's reports.
FUNNEL_LOG_RETENTION_PER_MODEL = 50

# ---------------------------------------------------------------------------
# Scoring weights (must sum to 100)
# ---------------------------------------------------------------------------
SCORE_WEIGHTS = {
    "ta_confluence": 35,   # MA / Fib / structure alignment
    "volume": 25,          # liquidity + volume spike
    "relative_strength": 25,
    "catalyst_safety": 15, # no earnings collision / clean news
}

# ---------------------------------------------------------------------------
# Macro regime thresholds
# ---------------------------------------------------------------------------
VIX_HIGH_VOL = 25
VIX_ELEVATED = 20
USDT_DOMINANCE_RISING_PCT = 0.03  # 3% rise over lookback flags risk-off crypto regime

# ---------------------------------------------------------------------------
# News / sentiment
# ---------------------------------------------------------------------------
NEWS_LOOKBACK_HOURS = 48
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
REQUEST_TIMEOUT = 10
BINANCE_BASE_URL = "https://api.binance.com"
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

# ---------------------------------------------------------------------------
# yfinance disk cache (utils.yf_cache) -- shared between the Livne and Biton
# pipelines (two separate OS processes, see live_runner.py's dual-model
# orchestration) so a ticker fetched by one model is served from disk by the
# other instead of hitting Yahoo again. See utils/yf_cache.py for why this is
# a function-result cache rather than requests_cache (yfinance's session
# uses curl_cffi browser impersonation; wrapping that session risks breaking
# it and defeating the whole point).
# ---------------------------------------------------------------------------
YF_CACHE_DIR = os.path.join(RUNTIME_DIR, "cache", "yfinance")
YF_OHLCV_CACHE_SECONDS = 1800       # 30 min -- daily/weekly context bars, which barely move intraday
YF_INTRADAY_CACHE_SECONDS = 180     # 3 min -- 15m/5m/1H execution bars. Must stay well under the
                                     # 15m bar period or the trigger timeframe would be evaluated
                                     # against a bar that closed two cycles ago.
YF_INTRADAY_STALE_FALLBACK_SECONDS = 900  # 15 min -- outage safety net for the intraday fetchers
YF_METADATA_CACHE_SECONDS = 24 * 3600  # 24h -- market cap barely moves intraday; earnings dates are known in advance
YF_STALE_FALLBACK_SECONDS = 72 * 3600  # how far past expiry a cached value may still be served as an emergency
                                        # fallback if every retry fails (e.g. a sustained rate-limit outage) --
                                        # NOT used for live price lookups (get_stock_fast_info), which need to
                                        # stay fresh and instead use a much shorter fallback window (see data_fetch.py)
YF_RETRY_DELAYS = (2, 5, 10)  # seconds between retries on a failed yfinance call (3 retries, exponential-ish backoff)

# ---------------------------------------------------------------------------
# Telegram alerts
#
# The credentials themselves are NOT read here. `TELEGRAM_BOT_TOKEN` /
# `TELEGRAM_CHAT_ID` used to be defined in this file but nothing ever imported
# them -- `core.utils.telegram_notifier` has always read `core.settings`
# directly -- so they were dead config that looked live, and a rotation edited
# here would silently have had no effect.
#
# What this model DOES declare is which named route its messages go to.
# Unconfigured, that route resolves to the same shared default chat as before;
# set TELEGRAM_CHAT_ID_LIVNE (or a TELEGRAM_ROUTES entry) to split it out into
# its own group. See core/settings.py.
# ---------------------------------------------------------------------------
TELEGRAM_ROUTE = _MODEL.telegram_route

# ---------------------------------------------------------------------------
# Chart generation -- rendered PNGs for Telegram alerts (core.charting)
# ---------------------------------------------------------------------------
CHART_OUTPUT_DIR = os.environ.get("CHART_OUTPUT_DIR", os.path.join(RUNTIME_DIR, "charts"))

# ---------------------------------------------------------------------------
# Model identity. Which OTHER models exist, and how they are scheduled, is not
# this file's business any more -- see core/registry.py and run_supervisor.py.
# ---------------------------------------------------------------------------
MODEL_NAME = _MODEL.label

# This model no longer knows that any OTHER model exists. `BITON_MODEL_DIR`,
# `BITON_LIVE_RUNNER` and `BITON_PHASE_TIMEOUT_SECONDS` lived here because
# Livne's runner launched Biton as a subprocess every cycle -- which made Livne
# Biton's scheduler and single point of failure. Scheduling moved to
# run_supervisor.py, and the per-model timeout is now `phase_timeout_seconds`
# on each `core.registry` entry.

# ---------------------------------------------------------------------------
# Paper trading -- Livne's own isolated store/journal. Biton's equivalents sit
# beside them in the same runtime/ tree under its own filenames; the two
# models never read or write each other's files.
# ---------------------------------------------------------------------------
PAPER_TRADING_STORE = _MODEL.trade_store
PAPER_TRADING_JOURNAL_XLSX = _MODEL.journal_xlsx

# ---------------------------------------------------------------------------
# Live runner -- INTRADAY cadence
#
# Livne cycles every 2 minutes: a trade meant to live 20 minutes cannot be
# managed on a 15-minute poll, which gave it roughly one stop/target check for
# its entire lifetime. The cadence itself is declared on this model's
# `core.registry` entry, which is also what the supervisor schedules from --
# so a change here can no longer alter any other model's run rate as a side
# effect (it previously could: Biton's interval was a constant in this file).
#
# One thing is deliberately NOT sped up with it:
#
#   FUNNEL_EXPORT_INTERVAL_MINUTES -- the funnel tracer re-runs the entire
#   gate chain for every watchlist ticker. At a 2-minute cadence that is ~30
#   reports/hour, which would churn through the 50-file retention in under two
#   hours and roughly double scan cost for a purely diagnostic artifact.
# ---------------------------------------------------------------------------
CYCLE_INTERVAL_MINUTES = _MODEL.cycle_interval_minutes
FUNNEL_EXPORT_INTERVAL_MINUTES = 15

# Where file-persisted interval gates keep their last-fire markers (see
# core.scheduling). Needed because the supervisor launches this runner as a
# fresh `--once` process per tick, so an in-memory gate would reset every time.
GATE_STATE_STORE = os.path.join(RUNTIME_DIR, "state", f"gates_{MODEL_KEY}.json")

# Intraday signals go stale fast. A setup that has not been entered within a
# few hours no longer reflects the structure that produced it.
PENDING_EXPIRY_HOURS = 6

# ---------------------------------------------------------------------------
# Scheduled Telegram summaries (core.reporting.summary)
#
# Daily at DAILY_REPORT_HOUR local, weekly additionally on WEEKLY_REPORT_WEEKDAY
# (0=Mon .. 4=Fri). Fired from run_cycle, guarded by a date marker in
# REPORT_STATE_STORE so each report goes out exactly once per day regardless of
# how many cycles fall inside the hour (at a 2-minute cadence, ~30 do).
#
# The state file is per-model: Livne and Biton report independently and must
# never share a marker, or whichever ran first would suppress the other.
# ---------------------------------------------------------------------------
DAILY_REPORT_HOUR = 21
WEEKLY_REPORT_WEEKDAY = 4  # Friday
REPORT_STATE_STORE = _MODEL.report_state_store
# Send the daily summary even when nothing happened. Kept on by default: a
# short "none / none" message each evening is a useful liveness signal that
# the runner is still up. Set False to only report on activity.
SEND_EMPTY_DAILY_REPORT = True

# Livne's lifecycle. CLOSED are terminal; UPDATE are non-terminal status
# changes still worth surfacing (PARTIAL_TP1 banks half the position).
REPORT_CLOSED_STATUSES = _MODEL.closed_statuses
REPORT_UPDATE_STATUSES = _MODEL.update_statuses

# ---------------------------------------------------------------------------
# Execution / fill-price simulation
# ---------------------------------------------------------------------------
MAX_SLIPPAGE_PCT = 0.0015  # 0.15% max adverse slippage modeled on a stop-loss fill

# ---------------------------------------------------------------------------
# Daily analyzer (agents/steve/daily_tradein_analyzer.py)
#
# Steve reports on ALL models and no longer takes ANYTHING model-specific from
# this file. Two groups of constants moved out:
#
#   PAPER_TRADING_STORE_BITON / BACKTEST_TRADES_CSV_BITON -> `core.registry`.
#   Livne's config was carrying Biton's runtime layout: the wrong direction,
#   and it does not survive a third model. Steve now iterates the registry and
#   asks each ModelConfig for its own paths.
#
#   MIN_CLOSED_TRADES_FOR_LLM / REPORT_ARCHIVE_DIR / MAIL_DIR /
#   ANALYZER_LOG_PATH / LLM_REQUEST_PATH -> `core.settings`. Those are the
#   analyzer's own settings; nothing in this model ever read them.
#
# What remains here is only Livne's OWN backtest output path.
# ---------------------------------------------------------------------------
BACKTEST_TRADES_CSV = _MODEL.backtest_csv
