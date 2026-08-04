"""Central configuration: watchlists, thresholds, scoring weights, API keys."""
import os

from dotenv import load_dotenv

load_dotenv()

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
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Chart generation (sibling ./graphs project renders setup charts for alerts)
# ---------------------------------------------------------------------------
GRAPHS_DIR = os.environ.get(
    "GRAPHS_DIR",
    os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "Graphs")),
)

# ---------------------------------------------------------------------------
# Dual-model identity (see live_runner.py) -- this is the frozen "Biton"
# (legacy) pipeline, run alongside the "Livne" (current/refactored) pipeline
# in the sibling ../TRADEIN codebase as two fully isolated processes. Biton's
# own paper-trading store/journal/funnel exports are deliberately relocated
# into ../TRADEIN so both models' runtime artifacts live in one place,
# without this codebase importing (or being imported by) TRADEIN's modules --
# they share no Python process, only these output file paths.
# ---------------------------------------------------------------------------
MODEL_NAME = "Biton model"

_TRADEIN_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "TRADEIN"))

# ---------------------------------------------------------------------------
# Paper trading
# ---------------------------------------------------------------------------
PAPER_TRADING_STORE = os.path.join(_TRADEIN_ROOT, "paper_trading", "trades_biton.json")
PAPER_TRADING_JOURNAL_XLSX = os.path.join(_TRADEIN_ROOT, "journals", "trade_journal_biton.xlsx")

# ---------------------------------------------------------------------------
# Live funnel diagnostics (screener/funnel_diagnostics.py) -- shared directory
# with Livne's own live_funnel_livne_*.xlsx reports; filenames are namespaced
# per model (see that module) so they never collide.
# ---------------------------------------------------------------------------
FUNNEL_LOG_DIR = os.path.join(_TRADEIN_ROOT, "funnel_logs")

# ---------------------------------------------------------------------------
# yfinance disk cache (utils.yf_cache) -- shared with the Livne (../TRADEIN)
# pipeline via the same physical directory, so whichever model fetches a
# ticker first, the other reads it straight off disk instead of hitting
# Yahoo again. Values must match ../TRADEIN/config.py's YF_* constants so a
# cache entry written by either process is honored identically by the other.
# ---------------------------------------------------------------------------
YF_CACHE_DIR = os.path.join(_TRADEIN_ROOT, ".cache", "yfinance")
YF_OHLCV_CACHE_SECONDS = 1800
YF_METADATA_CACHE_SECONDS = 24 * 3600
YF_STALE_FALLBACK_SECONDS = 72 * 3600
YF_RETRY_DELAYS = (2, 5, 10)

# ---------------------------------------------------------------------------
# Live runner
# ---------------------------------------------------------------------------
CYCLE_INTERVAL_MINUTES = 15  # minutes between background scan/refresh cycles

# ---------------------------------------------------------------------------
# Execution / fill-price simulation
# ---------------------------------------------------------------------------
MAX_SLIPPAGE_PCT = 0.0015  # 0.15% max adverse slippage modeled on a stop-loss fill

# ---------------------------------------------------------------------------
# Daily analyzer (daily_tradein_analyzer.py)
# ---------------------------------------------------------------------------
MIN_CLOSED_TRADES_FOR_LLM = 20  # below this, skip the narrative handoff (sample too small)
REPORT_ARCHIVE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "MailToMail", "Scripts")
)
MAILTOMAIL_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "MailToMail")
)
BACKTEST_TRADES_CSV = os.path.join(
    os.path.dirname(__file__), "backtest_suite", "trades_log.csv"
)
