"""Bot configuration.

Only secrets live in `.env` (API keys, Telegram token/chat id, testnet flag).
Every strategy / risk parameter is a constant below so the .env file stays small.
Edit the values here if you want to tune the bot.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(v: str | None) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Secrets:
    api_key: str
    api_secret: str
    tg_token: str
    tg_chat_id: str
    testnet: bool


def load_secrets() -> Secrets:
    s = Secrets(
        api_key=os.getenv("BINANCE_API_KEY", ""),
        api_secret=os.getenv("BINANCE_API_SECRET", ""),
        tg_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        tg_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        testnet=_bool(os.getenv("BINANCE_TESTNET", "false")),
    )
    missing = [n for n, v in (("BINANCE_API_KEY", s.api_key), ("BINANCE_API_SECRET", s.api_secret)) if not v]
    if missing:
        raise SystemExit(f"Thiếu biến môi trường: {', '.join(missing)} (xem .env.example)")
    return s


# ---------------------------------------------------------------- general
TIMEZONE = "Asia/Ho_Chi_Minh"          # daily report boundary
DB_PATH = os.getenv("BOT_DB_PATH", "data/bot.db")
MAINNET_URL = "https://fapi.binance.com"
TESTNET_URL = "https://testnet.binancefuture.com"
STABLE_ASSETS = ("USDT", "USDC", "FDUSD", "BUSD")  # valued 1:1 in capital reports

# ---------------------------------------------------------------- universe
UNIVERSE_SIZE = 40                     # top N USDT perpetuals by 24h quote volume
MIN_QUOTE_VOLUME = 50_000_000          # USDT, 24h
MIN_LISTING_DAYS = 30                  # skip freshly listed coins
MAX_24H_CHANGE_PCT = 25.0              # skip pumped/dumped coins
EXCLUDED_BASES = ("USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USDP", "EUR")

# ---------------------------------------------------------------- position targets
TARGET_PROFIT_PCT = 0.02               # full TP on full size = 2% of capital
MIN_EXPECTED_PROFIT_PCT = 0.015        # skip setups that can't make 1.5% of capital
TP1_DISTANCE_RATIO = 0.5               # TP1 at 50% of the way to TP
TP1_CLOSE_RATIO = 0.4                  # close 40% at TP1 -> 0.4*0.5 + 0.6*1 = 80% of 2% = 1.6%
MIN_RR = 1.8                           # TP distance >= 1.8 x SL distance
SL_ATR_MULT = 1.2                      # min SL distance (x ATR 1h)
SL_SWING_BUFFER_ATR = 0.25             # SL placed beyond swing by 0.25 ATR
MAX_SL_ATR_MULT = 2.5                  # structure too far -> skip
TP_ATR_MULT = 2.0                      # min TP distance (x ATR 1h)
MIN_SL_PCT, MAX_SL_PCT = 0.005, 0.05
MAX_TP_PCT = 0.08

# ---------------------------------------------------------------- risk / leverage
MAX_OPEN_POSITIONS = 3
MAX_RISK_PCT = 0.015                   # max loss at SL incl. DCA = 1.5% of capital
MAX_MARGIN_PCT = 0.25                  # margin per position <= 25% of capital
MAX_LEVERAGE = 20
LIQ_SAFETY = 3.0                       # liquidation distance >= 3 x SL distance
TAKER_FEE = 0.0005
BE_BUFFER_PCT = 0.0005                 # break-even SL = avg +/- fees + 0.05%
DAILY_MAX_LOSS_PCT = 0.04              # stop opening new trades for the day
SYMBOL_COOLDOWN_HOURS = 6              # after an SL on a symbol
MIN_SCORE = 60

# ---------------------------------------------------------------- position management
DCA_MAX = 1                            # max number of adds
DCA_TRIGGER = 0.45                     # add when price went 45%..80% of the way to SL
DCA_MAX_ZONE = 0.80
DCA_MAX_RATIO = 0.6                    # add at most 60% of the initial size
TRAIL_ATR_MULT = 1.5                   # trailing distance after TP1
TRAIL_MIN_STEP_ATR = 0.2               # only move SL when it improves by >= 0.2 ATR
STALE_HOURS = 12                       # after this, required profit starts to decay
MAX_HOLD_HOURS = 48                    # after this, close at any positive net PnL
MIN_EXIT_PROFIT_PCT = 0.001            # "positive" = net PnL >= 0.1% of capital

# ---------------------------------------------------------------- loop timing (seconds)
MANAGE_INTERVAL = 15
SLOW_CHECK_INTERVAL = 300              # time-exit / reversal / DCA confirmation
ACCOUNTING_INTERVAL = 300              # income sync (deposits, withdrawals, pnl)
