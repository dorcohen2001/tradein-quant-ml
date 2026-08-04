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
# Liquidity is gated on a 20-day SMA of dollar volume (Close * Volume) rather
# than a single day's reading, so one noisy bar can't flip a name in or out.
# No static minimum stock price -- a hard $5 floor was dropped in favor of
# the dollar-volume/market-cap filters actually doing the liquidity work.
DOLLAR_VOLUME_SMA_LOOKBACK = 20
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
MIN_RISK_REWARD = 3.0                 # minimum 1:3 R:R (final target) for any generated trade
TOP_N_ASSETS = 2                      # number of setups produced per run

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
# Multi-Timeframe Analysis (MTFA)
# ---------------------------------------------------------------------------
# Binance kline intervals are native; yfinance has no 4H/8H/12H so those are
# built by resampling 1H bars (see utils.data_fetch.resample_ohlc).
HTF_INTERVALS = {"crypto": {"monthly": "1M", "weekly": "1w"},
                  "stocks": {"monthly": "1mo", "weekly": "1wk"}}
ITF_INTERVALS_CRYPTO = ["12h", "8h", "4h"]        # native Binance intervals
ITF_RESAMPLE_RULES_STOCKS = ["12h", "8h", "4h"]   # pandas resample rules from 1H bars

RSI_PERIOD = 14
RSI_PIVOT_LEFT = 16
RSI_PIVOT_RIGHT = 5
DIVERGENCE_MIN_BAR_GAP = 5     # min bars between compared pivots
DIVERGENCE_MAX_BAR_GAP = 60    # max bars between compared pivots

OB_LOOKBACK_BARS = 80          # HTF bars scanned for order blocks
OB_IMPULSE_ATR_MULT = 2.0      # impulse candle must exceed this multiple of ATR
OB_STRUCTURE_LOOKBACK = 20     # bars used to detect the swing high/low the impulse must break
HTF_ZONE_PROXIMITY_PCT = 0.04  # price must be within +/-4% of a HTF support/resistance/OB/Fib level to count as
                                # "at the zone" -- widened from 2% to catch setups developing just outside a tight band
ITF_MA200_MIN_ALIGNED = 2      # how many of the 3 ITF timeframes must agree with PRIMARY_MA_PERIOD trend alignment

# Stage A confluence *scoring* input (see screener._score_candidate), NOT a
# hard gate -- weekly volume vs its N-week average at the matched HTF zone.
# Institutional absorption/accumulation at real structure often prints on
# near-average or even red weekly bars (a "low-volume retest"); directional
# confirmation and volume triggers belong downstream in Stage B (braking
# event + ITF volume spike). 1.00 = baseline average; a zone touch on any
# volume still passes Stage A, above-average volume just scores a bonus.
HTF_VOLUME_CONFLUENCE_MULT = 1.00
HTF_VOLUME_CONFLUENCE_LOOKBACK = 20    # weeks

ITF_VOLUME_SPIKE_MULT = 1.10    # ITF reversal candle volume vs N-period SMA -- adaptive scaling: 1.05-1.10x is
                                 # enough for a Daily/Weekly swing execution timeframe (was 1.5x)
ITF_VOLUME_LOOKBACK = 20

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
TP1_MIN_RR = 2.0    # TP1 must clear at least 1:2 (recalibrated floor)
TP1_MAX_RR = 3.0    # TP1 targets the 1:2-1:3 band when a structural level allows
TP1_CLOSE_PCT = 0.5 # fraction of position closed at TP1
ACCOUNT_RISK_PCT = 0.01  # illustrative %-of-account risk used only to label alert size
ILLUSTRATIVE_ACCOUNT_SIZE = 10_000  # purely for the alert's informational position-size note; no real capital is tracked
STOP_STRUCTURAL_BUFFER_PCT = 0.005  # extra cushion placed beyond the chosen structural stop level
STOP_MAX_ATR_MULT = 5.0  # hard cap on stop distance: min(structural distance, this many ATRs) -- guards
                          # against a stale/extreme structural level (e.g. a far-away 52-week high/low)
                          # blowing the risk distance out to something unrealistic (and TP1/TP2 negative)

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
YF_OHLCV_CACHE_SECONDS = 1800       # 30 min -- daily/intraday-1H history bars
YF_METADATA_CACHE_SECONDS = 24 * 3600  # 24h -- market cap barely moves intraday; earnings dates are known in advance
YF_STALE_FALLBACK_SECONDS = 72 * 3600  # how far past expiry a cached value may still be served as an emergency
                                        # fallback if every retry fails (e.g. a sustained rate-limit outage) --
                                        # NOT used for live price lookups (get_stock_fast_info), which need to
                                        # stay fresh and instead use a much shorter fallback window (see data_fetch.py)
YF_RETRY_DELAYS = (2, 5, 10)  # seconds between retries on a failed yfinance call (3 retries, exponential-ish backoff)

# ---------------------------------------------------------------------------
# Telegram alerts
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Chart generation -- rendered PNGs for Telegram alerts (core.charting)
# ---------------------------------------------------------------------------
CHART_OUTPUT_DIR = os.environ.get("CHART_OUTPUT_DIR", os.path.join(RUNTIME_DIR, "charts"))

# ---------------------------------------------------------------------------
# Dual-model identity (see live_runner.py) -- this codebase is the "Livne"
# model (current/refactored pipeline), run alongside the "Biton" model (the
# frozen legacy pipeline in ../biton) as two fully isolated processes.
# ---------------------------------------------------------------------------
MODEL_NAME = "Livne model"

# Livne's runner drives Biton each cycle as a subprocess. BITON_PHASE_TIMEOUT
# bounds that call: without it a hung child blocks Livne's own position
# management indefinitely (a hang is not an exception, so no try/except can
# catch it). Sized generously -- a full scan+funnel pass over both watchlists
# is minutes, not seconds -- while still fitting inside one 15-minute cycle
# alongside Livne's own work.
BITON_MODEL_DIR = os.path.normpath(os.path.join(MODEL_DIR, "..", "biton"))
BITON_LIVE_RUNNER = os.path.join(BITON_MODEL_DIR, "live_runner.py")
BITON_PHASE_TIMEOUT_SECONDS = 480  # 8 minutes per phase

# ---------------------------------------------------------------------------
# Paper trading -- Livne's own isolated store/journal. Biton's equivalents sit
# beside them in the same runtime/ tree under its own filenames; the two
# models never read or write each other's files.
# ---------------------------------------------------------------------------
PAPER_TRADING_STORE = os.path.join(RUNTIME_DIR, "stores", "trades_livne.json")
PAPER_TRADING_JOURNAL_XLSX = os.path.join(RUNTIME_DIR, "journals", "trade_journal_livne.xlsx")

# ---------------------------------------------------------------------------
# Live runner
# ---------------------------------------------------------------------------
CYCLE_INTERVAL_MINUTES = 15  # minutes between background scan/refresh cycles

# ---------------------------------------------------------------------------
# Execution / fill-price simulation
# ---------------------------------------------------------------------------
MAX_SLIPPAGE_PCT = 0.0015  # 0.15% max adverse slippage modeled on a stop-loss fill

# ---------------------------------------------------------------------------
# Daily analyzer (agents/steve/daily_tradein_analyzer.py)
#
# Steve reports on BOTH models, so it needs each model's store/journal paths.
# It imports THIS config (Livne's) as the canonical one and derives Biton's
# siblings from the shared runtime/ tree, rather than importing two configs
# with clashing top-level module names.
# ---------------------------------------------------------------------------
MIN_CLOSED_TRADES_FOR_LLM = 20  # below this, skip the narrative handoff (sample too small)
REPORT_ARCHIVE_DIR = os.path.join(REPO_ROOT, "mail", "reports")
MAIL_DIR = os.path.join(REPO_ROOT, "mail")
BACKTEST_TRADES_CSV = os.path.join(RUNTIME_DIR, "backtest", "trades_log_livne.csv")
BACKTEST_TRADES_CSV_BITON = os.path.join(RUNTIME_DIR, "backtest", "trades_log_biton.csv")
PAPER_TRADING_STORE_BITON = os.path.join(RUNTIME_DIR, "stores", "trades_biton.json")
ANALYZER_LOG_PATH = os.path.join(RUNTIME_DIR, "logs", "analyzer.log")
LLM_REQUEST_PATH = os.path.join(RUNTIME_DIR, "logs", "llm_request.json")
