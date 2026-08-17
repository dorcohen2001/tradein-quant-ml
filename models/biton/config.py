"""Central configuration: watchlists, thresholds, scoring weights, API keys."""
import os

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Repository anchors -- mirrors models/livne/config.py. Derived from __file__,
# never from cwd, because this model runs both standalone and as a subprocess
# launched by the supervisor.
#
#     <repo>/models/biton/config.py  ->  REPO_ROOT = <repo>
# ---------------------------------------------------------------------------
MODEL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(MODEL_DIR, "..", ".."))
RUNTIME_DIR = os.path.join(REPO_ROOT, "runtime")

# Single repo-root .env shared with Livne. Previously this codebase had its
# own duplicate .env, so a rotated Telegram token had to be updated twice or
# one model would silently stop alerting. (That stale models/biton/.env has
# since been deleted outright -- it was byte-identical and nothing loaded it.)
load_dotenv(os.path.join(REPO_ROOT, ".env"))

# ---------------------------------------------------------------------------
# Identity + derived runtime paths come from `core.registry` -- see
# models/livne/config.py for the full rationale. Biton's RULES remain frozen;
# only the path/identity plumbing is now shared, and every value it produces is
# byte-identical to the literals this file used before.
# ---------------------------------------------------------------------------
import _bootstrap  # noqa: E402,F401  -- puts repo root on sys.path for core.*

from core.charting.spec import ChartSpec  # noqa: E402
from core.paper_trading.catch_up import CatchUpConfig  # noqa: E402
from core.registry import get_model  # noqa: E402

MODEL_KEY = "biton"
_MODEL = get_model(MODEL_KEY)

# ---------------------------------------------------------------------------
# Watchlists (used because full-market screening via free APIs is impractical
# to run daily without paid data feeds). Extend these lists as needed.
#
# This is a curated (not dynamically-fetched) universe of liquid mid/large-cap
# names — S&P 500 / NASDAQ-100 leaders plus high-volume growth/momentum
# stocks — deliberately kept static rather than scraped at runtime (e.g. from
# Wikipedia) to avoid adding a fragile network dependency to every scan cycle.
# The MIN_STOCK_PRICE / MIN_STOCK_MARKET_CAP / MIN_DOLLAR_VOLUME filters below
# are the real safety net against penny stocks or micro-caps slipping in.
# ---------------------------------------------------------------------------
WATCHLIST_STOCKS = [
    # Mega-cap tech / software / semis
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO", "ORCL",
    "ADBE", "CRM", "NFLX", "AMD", "INTC", "QCOM", "TXN", "MU", "CSCO", "IBM",
    "NOW", "INTU", "PANW", "SNPS", "CDNS", "KLAC", "LRCX", "AMAT", "ASML",
    "ANET", "ARM", "SMCI",
    # Financials
    "JPM", "BAC", "GS", "MS", "WFC", "C", "V", "MA", "AXP", "SCHW", "BLK",
    "SPGI",
    # Healthcare / pharma
    "UNH", "LLY", "JNJ", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "ISRG",
    "VRTX", "REGN",
    # Energy
    "XOM", "CVX", "COP", "SLB", "OXY",
    # Consumer staples / discretionary
    "COST", "WMT", "HD", "PG", "KO", "PEP", "DIS", "NKE", "MCD", "SBUX",
    "LOW", "TGT",
    # Industrials
    "BA", "CAT", "GE", "HON", "UNP", "RTX", "LMT",
    # Communication / telecom
    "CMCSA", "TMUS", "VZ", "T",
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
MIN_DOLLAR_VOLUME = 10_000_000        # $10M minimum daily traded value
MIN_STOCK_PRICE = 5.0                 # exclude penny stocks below this price
MIN_STOCK_MARKET_CAP = 1_000_000_000  # $1B floor; excludes micro-caps
VOLUME_SPIKE_MULT = 1.5               # volume vs 20-day average
MA_PROXIMITY_PCT = 0.02               # within +/-2% of 50/200 EMA
FIB_ZONE = (0.5, 0.618)               # golden zone
RS_LOOKBACK_DAYS = 5                  # relative strength window
EARNINGS_BLACKOUT_HOURS = 48          # exclude stocks with earnings this soon
MIN_RISK_REWARD = 2.0                 # minimum 1:2 R:R for any generated trade
TOP_N_ASSETS = 2                      # number of setups produced per run

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
# Telegram alerts
#
# Credentials are not read here -- see models/livne/config.py for why the old
# TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID pair was dead config. This declares only
# the named route; unconfigured it resolves to the shared default chat, and
# TELEGRAM_CHAT_ID_BITON splits Biton's alerts into their own group.
# ---------------------------------------------------------------------------
TELEGRAM_ROUTE = _MODEL.telegram_route

# ---------------------------------------------------------------------------
# Chart generation -- rendered PNGs for Telegram alerts (core.charting)
# ---------------------------------------------------------------------------
CHART_OUTPUT_DIR = os.environ.get("CHART_OUTPUT_DIR", os.path.join(RUNTIME_DIR, "charts"))

# Biton is a daily structural model, so it keeps the daily chart -- stated
# explicitly rather than inherited by omission, since the default is no longer
# the only option.
CHART_SPEC = ChartSpec.daily()

# ---------------------------------------------------------------------------
# Dual-model identity (see live_runner.py) -- this is the frozen "Biton"
# (legacy) pipeline, run alongside the "Livne" (current/refactored) pipeline
# in the sibling ../TRADEIN codebase as two fully isolated processes. Biton's
# own paper-trading store/journal/funnel exports are deliberately relocated
# into ../TRADEIN so both models' runtime artifacts live in one place,
# without this codebase importing (or being imported by) TRADEIN's modules --
# they share no Python process, only these output file paths.
# ---------------------------------------------------------------------------
MODEL_NAME = _MODEL.label
# Whether this model participates in scheduled execution. The runner refuses
# to run when False unless --force, so a Task Scheduler entry pointing
# straight at live_runner.py cannot quietly bypass a deliberate pause.
MODEL_ENABLED = _MODEL.enabled

# ---------------------------------------------------------------------------
# Paper trading -- Biton's own isolated store/journal, sitting beside Livne's
# in the shared runtime/ tree. Distinct filenames; neither model ever reads
# or writes the other's files.
# ---------------------------------------------------------------------------
PAPER_TRADING_STORE = _MODEL.trade_store
PAPER_TRADING_JOURNAL_XLSX = _MODEL.journal_xlsx

# Where cross-process coordination files live: the one-instance-per-tenant lock
# (core.locking) and any file-persisted gate markers. The lock is what stops a
# manual run and a supervisor-launched cycle from both read-modify-writing the
# store above, where the second save would erase the first's transitions.
STATE_DIR = os.path.join(RUNTIME_DIR, "state")

# ---------------------------------------------------------------------------
# Live funnel diagnostics (screener/funnel_diagnostics.py) -- shared directory
# with Livne's own live_funnel_livne_*.xlsx reports; filenames are namespaced
# per model (see that module) so they never collide, and retention prunes
# per-model-prefix so neither model can evict the other's reports.
# ---------------------------------------------------------------------------
FUNNEL_LOG_DIR = os.path.join(RUNTIME_DIR, "funnel_logs")
FUNNEL_LOG_RETENTION_PER_MODEL = 50
FUNNEL_LOG_PREFIX = _MODEL.funnel_prefix

# ---------------------------------------------------------------------------
# yfinance disk cache (core.utils.yf_cache) -- the same physical directory
# Livne uses, so whichever model fetches a ticker first, the other reads it
# straight off disk instead of hitting Yahoo again. These YF_* values must
# match ../livne/config.py's so a cache entry written by either process is
# honored identically by the other.
# ---------------------------------------------------------------------------
YF_CACHE_DIR = os.path.join(RUNTIME_DIR, "cache", "yfinance")
YF_OHLCV_CACHE_SECONDS = 1800
YF_METADATA_CACHE_SECONDS = 24 * 3600
YF_STALE_FALLBACK_SECONDS = 72 * 3600
YF_RETRY_DELAYS = (2, 5, 10)

# ---------------------------------------------------------------------------
# Live runner
# ---------------------------------------------------------------------------
CYCLE_INTERVAL_MINUTES = _MODEL.cycle_interval_minutes  # minutes between background scan/refresh cycles

# Historical catch-up (core.paper_trading.catch_up). Biton cycles every 15
# minutes, so the 45-minute floor sits at 3x the healthy gap -- comfortably
# clear of a slow cycle, while still catching any real outage. Below it no
# historical bar is fetched at all, which is what keeps steady-state cost at
# zero. Biton is currently deactivated, so its first catch-up will cover the
# pause (bounded by max_lookback_hours).
CATCH_UP_CONFIG = CatchUpConfig(
    min_gap_minutes=45.0,
    max_lookback_hours=72.0,
    crypto_interval="5m",
    stock_interval="5m",
    interval_minutes=5,
)

# ---------------------------------------------------------------------------
# Scheduled Telegram summaries (core.reporting.summary)
#
# Same schedule as Livne but reported SEPARATELY, with its own state file --
# the two models must never share a send-once marker or whichever ran first
# would suppress the other's report.
#
# The check lives in run_cycle rather than the main loop because this runner is
# normally launched by the supervisor with `--once`: the loop never executes, so a
# loop-level check would mean Biton never reported at all. For the same reason
# the marker must be file-persisted -- the process exits every cycle.
# ---------------------------------------------------------------------------
DAILY_REPORT_HOUR = 21
WEEKLY_REPORT_WEEKDAY = 4  # Friday
REPORT_STATE_STORE = _MODEL.report_state_store
SEND_EMPTY_DAILY_REPORT = True

# Biton's lifecycle: single-shot TP, so every reportable status is terminal
# and there are no non-terminal update statuses.
REPORT_CLOSED_STATUSES = _MODEL.closed_statuses
REPORT_UPDATE_STATUSES = _MODEL.update_statuses

# ---------------------------------------------------------------------------
# Execution / fill-price simulation
# ---------------------------------------------------------------------------
MAX_SLIPPAGE_PCT = 0.0015  # 0.15% max adverse slippage modeled on a stop-loss fill

# ---------------------------------------------------------------------------
# Backtest output. The Steve analyzer's own settings (MIN_CLOSED_TRADES_FOR_LLM,
# REPORT_ARCHIVE_DIR, MAILTOMAIL_DIR) used to be duplicated here but were dead
# code: Steve imports Livne's config exclusively and never this one. Removed.
# ---------------------------------------------------------------------------
BACKTEST_TRADES_CSV = _MODEL.backtest_csv
