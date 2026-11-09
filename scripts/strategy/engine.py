import truststore
truststore.inject_into_ssl()

from kiteconnect import KiteConnect, KiteTicker
from kiteconnect.exceptions import PermissionException, InputException, DataException
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time as dtime
import pytz
import time
import os
import json
import urllib.request
import inspect
import signal
import threading
from typing import List, Dict, Optional
from editable_config import load_editable_config
from strategy import runtime, variants
import sys
import os
import io
from pathlib import Path

# This engine lives in scripts/strategy/. Every path below stays relative to
# scripts/ exactly as when the strategy was a single scripts/vNN_*.py file
# (.env lookup, logs/, data/, and the live-order lock that app.py checks).
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Settings that differ per variant (strategy/variants/<name>.py), chosen by the launcher.
_VARIANT = variants.load(runtime.require_variant())

# -----------------------------------------------------------------------------
# Execution engine logic overview
# -----------------------------------------------------------------------------
# Entry model
#    - Uses minute-level candles and indicator warmup for entry signals.
#    - Applies a 5-minute multi-timeframe trend filter using EMA-based trend.
#    - Confirms entries on option premium movement and reversal scores.
#    - Long CE entry is taken on bullish MTF trend with CE confirmation.
#    - Long PE entry is taken on bearish MTF trend with PE confirmation.

# Fixes
# - Updated to use fixed 50-point SL for all trades, with no ATR component.
# - Added dynamic trailing stop logic to move SL to cost at 50 points profit, then trail every 10 points with a 10% trailing buffer.
# - Adjusted profit target and loss limit to reflect the new fixed SL approach, aiming for a more balanced risk-reward.
# - Added a profit lock mechanism to secure profits after reaching a certain threshold, allowing for partial profit booking while letting the rest run with a trailing stop.
# - TRAILING_STOP_HIT exit reason added to differentiate between regular SL hits and trailing SL hits.
# - autoselect option symbols logic now selects the nearest OTM strikes based on the spot price at app start, rounded to the nearest configured step (e.g. 50 points), for better adaptability across different market conditions and expiries.

def load_env_file(env_path: str = None, override: bool = True) -> None:
    if env_path is None:
        script_dir = SCRIPTS_DIR
        project_env = os.path.join(os.path.dirname(script_dir), ".env")
        script_env = os.path.join(script_dir, ".env")
        env_path = project_env if os.path.exists(project_env) else script_env
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8-sig") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and (override or key not in os.environ):
                os.environ[key] = value


load_env_file()

DEFAULT_LOGS_DIR = os.path.join(os.path.dirname(SCRIPTS_DIR), "logs")
STRATEGY_LOG_DIR = os.path.abspath(os.environ.get("STRATEGY_LOG_DIR", DEFAULT_LOGS_DIR).strip() or DEFAULT_LOGS_DIR)
_PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)
_OPTION_DAILY_CANDIDATES = (
    Path(_PROJECT_ROOT) / "data" / "nifty_options" / "daily",
    Path(SCRIPTS_DIR) / "data" / "nifty_options" / "daily",
)
OPTION_DAILY_DIR = Path(
    os.environ.get("OPTION_DAILY_DIR", "")
    or next(
        (
            str(path)
            for path in _OPTION_DAILY_CANDIDATES
            if path.exists()
        ),
        str(_OPTION_DAILY_CANDIDATES[0]),
    )
)
UTC = pytz.UTC
# =========================
# COMMAND-LINE OVERRIDES
# (parsed before USER INPUTS so they can override config values)
# Usage examples:
#   py scripts/v28_strategy_main.py --date 2026-06-09        (or v30_strategy_main.py)
#   py scripts/v28_strategy_main.py --date 2026-06-09 --mode BACKTEST
#   py scripts/v28_strategy_main.py --mode PAPERTRADE_LIVE --after-hours-replay --date 2026-07-23
# =========================
import argparse as _argparse
_cli = _argparse.ArgumentParser(add_help=False)
_cli.add_argument("date_pos", nargs="?", default=None, help="Trade date YYYY-MM-DD (positional)")
_cli.add_argument("--date", default=None, help="Trade date YYYY-MM-DD (overrides TRADE_DATE)")
_cli.add_argument("--mode", default=None, help="Run mode: BACKTEST | PAPERTRADE_LIVE | LIVE_ORDER")
_cli.add_argument(
    "--option-data-source",
    choices=("DOWNLOADED", "KITE"),
    default=None,
    help="Backtest option candle source (overrides BACKTEST_OPTION_DATA_SOURCE)",
)
_cli.add_argument("--after-hours-replay", action="store_true", help="Allow PAPERTRADE_LIVE to replay the completed day after market close")
# The launcher passes its command line via strategy.runtime; importing the engine
# elsewhere (e.g. unit tests) parses an empty one, so defaults apply.
_cli_args, _ = _cli.parse_known_args(runtime.CLI_ARGV)
# positional arg wins if --date not supplied
if _cli_args.date is None and _cli_args.date_pos is not None:
    _cli_args.date = _cli_args.date_pos

BACKTEST_OPTION_DATA_SOURCE = (
    _cli_args.option_data_source
    or os.getenv("BACKTEST_OPTION_DATA_SOURCE", "DOWNLOADED")
).strip().upper()
if BACKTEST_OPTION_DATA_SOURCE not in {"DOWNLOADED", "KITE"}:
    raise ValueError(
        "BACKTEST_OPTION_DATA_SOURCE must be either DOWNLOADED or KITE"
    )

# =========================
# USER INPUTS
# =========================

EDITABLE_CONFIG = load_editable_config()

API_KEY = os.getenv("API_KEY", "").strip()
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "").strip()

UNDERLYING_NAME = "NIFTY"
UNDERLYING_TOKEN = 256265

# Exact option symbols for the selected day
TRADE_DATE = _cli_args.date if _cli_args.date else "2026-07-07"   # YYYY-MM-DD
CE_SYMBOL = "NIFTY2660923500CE"
PE_SYMBOL = "NIFTY2660923500PE"
OPTION_EXCHANGE = "NFO"

# Auto select option symbols (used only when AUTO_SELECT_OPTION_SYMBOLS = True)
AUTO_SELECT_OPTION_SYMBOLS = True  # If True, CE_SYMBOL and PE_SYMBOL are ignored and auto-selected based on the rules below
AUTO_SELECT_EXPIRY = ""      # e.g. "2026-03-24"; keep blank for nearest available expiry
AUTO_SELECT_STRIKE_STEP = 50
AUTO_SELECT_SPOT_REF = "SIGNAL_CLOSE"  # Select from the latest completed 1-minute spot candle at each entry opportunity
AUTO_SELECT_SESSION_LOCKED = False  # Allow strike selection to follow the signal-candle close
AUTO_SELECT_REFRESH_INTERVAL_MINUTES = 1  # Re-evaluate while flat on each completed 1-minute candle
AUTO_SELECT_STRIKE_SWITCH_BUFFER_POINTS = 12.5  # Require this many points beyond strike midpoint before switching strikes
AUTO_SELECT_MIN_SWITCH_INTERVAL_SECONDS = 45    # Minimum gap between strike switches in live modes

# Execution mode
RUN_MODE = _cli_args.mode if _cli_args.mode else "PAPERTRADE_LIVE"       # BACKTEST, PAPERTRADE_LIVE, or LIVE_ORDER
AFTER_HOURS_PAPER_REPLAY = bool(_cli_args.after_hours_replay)
LOG_IDENTIFIER = _VARIANT.LOG_IDENTIFIER  # Appended to log filenames; set per variant in strategy/variants/
EXPORT_EXCEL = True
PAPERTRADE_FRESH_COUNT_ON_RESTART = True  # True: allow fresh entries only from app start time when PAPERTRADE_LIVE starts mid-session
READ_HISTORY_FROM_DAY_START = True          # Keep full-day candles for indicator warmup even if app starts mid-session
ALLOW_ENTRY_ONLY_AFTER_APP_START = True     # In PAPERTRADE_LIVE/LIVE_ORDER, do not allow a fresh entry before the app start minute

# Auto-select diagnostics dedupe keys to avoid repetitive logs on each refresh cycle.
_LAST_AUTOSELECT_EXPIRY_LOG_KEY = None
_LAST_AUTOSELECT_SYMBOLS_LOG_KEY = None
_SIGNAL_CLOSE_STRIKE_LOG_KEYS = set()

# Guard mode
# If ENABLE_MTM_GUARD = True  -> use daily profit/loss MTM guard + MAX_TRADES, whichever hits first
# If ENABLE_MTM_GUARD = False -> use only MAX_TRADES
ENABLE_MTM_GUARD = True
MAX_TRADES = EDITABLE_CONFIG["MAX_TRADES"]

ENABLE_CONSECUTIVE_LOSS_GUARD = True
MAX_CONSECUTIVE_LOSSES = 2

# =========================
# PER SYMBOL LIMIT
# =========================
MAX_CONCURRENT_TRADES = 1
MAX_CONCURRENT_TRADES_CE = 1
MAX_CONCURRENT_TRADES_PE = 1
   # Max parallel open positions allowed at the same time
LOT_SIZE = 65   # NIFTY lot size (fixed by exchange)
LOTS     = 1    # Number of lots to trade — increase this to trade more lots
QTY      = LOT_SIZE * LOTS
DAILY_PROFIT_TARGET_RS = 5000 * LOTS
DAILY_LOSS_LIMIT_RS = -3250 * LOTS
ENABLE_PROFIT_LOCK = True
PROFIT_LOCK_TRIGGER_RS = 2500 * LOTS
PROFIT_LOCK_PERCENT = 0.70
PROFIT_LOCK_AFTER_TRADE_NO = 1  # activate after first completed trade
ENABLE_TRAILING_PROFIT_EXIT = True  # Enable trailing profit after first 50-point profit
# Trailing ladders [(profit_points, lock_points), ...] are set per variant in strategy/variants/.
TRAILING_PROFIT_STAGE1 = _VARIANT.TRAILING_PROFIT_STAGE1
TRAILING_PROFIT_STAGE2 = _VARIANT.TRAILING_PROFIT_STAGE2
TRAILING_PROFIT_STAGE2_TIME = "10:30"  # HH:MM (IST); at or after this time, switch to TRAILING_PROFIT_STAGE2
ENABLE_MFE_EXIT = bool(EDITABLE_CONFIG["ENABLE_MFE_EXIT"])
MFE_MIN_POINTS = 10.0
MFE_EXIT_AFTER_MINUTES = 10
REENTRY_COOLDOWN_MINUTES = 3  # Wait this many minutes after an exit before allowing any fresh entry in every mode

# Entry execution safety
MAX_ENTRY_PREMIUM = 200.0  # Skip fresh entries when the signal option premium is above this value
MAX_ENTRY_SLIPPAGE_POINTS_ALLOWED = 8.0
SKIP_ENTRY_IF_TOO_MUCH_SLIPPAGE = False
BLOCK_SAME_SYMBOL_REENTRY_WITHIN_MINUTES = 5
SAME_SYMBOL_MIN_REENTRY_DROP_POINTS = 20.0  # After a same-symbol exit, only re-enter if option price is at least this many points below the previous exit price (0 = disabled)
ENTRY_CONFIRM_DELAY_SECONDS = 2
SIMULATE_LIVE_EXECUTION_IN_PAPERTRADE = False
SIMULATE_LIVE_EXECUTION_IN_BACKTEST = False
PAPER_ENTRY_SLIPPAGE_MODE = "NONE"    # FIXED / PCT / NONE
PAPER_ENTRY_SLIPPAGE_POINTS = 2.0
PAPER_ENTRY_SLIPPAGE_PCT = 0.005
PAPER_EXIT_SLIPPAGE_MODE = "NONE"     # FIXED / PCT / NONE
PAPER_EXIT_SLIPPAGE_POINTS = 1.5
PAPER_EXIT_SLIPPAGE_PCT = 0.004
SIMULATE_INTRAMINUTE_EMERGENCY_SL = True
SIMULATE_INTRAMINUTE_TARGET_HIT = True
# Signal age is measured from the signal candle's start, so a normal entry is
# already ~62s old (candle + confirm delay). 150s allows that plus slack; an
# older signal is stale (e.g. 28 Jul: an 11-minute-old signal filled 36 pts away).
MAX_ENTRY_SIGNAL_AGE_SECONDS = 150

# Runtime visibility / tick monitoring
TICK_STALL_WARN_SECONDS = 20
TICK_STALL_RESUBSCRIBE_SECONDS = 60
TICK_STALL_RESUBSCRIBE_COOLDOWN_SECONDS = 45
TICK_STALL_RECONNECT_SECONDS = 150
TICK_STALL_RECONNECT_COOLDOWN_SECONDS = 120
IDLE_STATUS_INTERVAL_SECONDS = 30
OPEN_PNL_LOG_INTERVAL_SECONDS = 30
ENTRY_DIAGNOSTICS = True

# GIFT Nifty context (log only — never blocks an entry).
# PAPERTRADE_LIVE / LIVE_ORDER fetch a GIFT Nifty quote at each fresh BUY and
# record it on the BUY event (console + Excel events sheet). If the quote
# cannot be fetched the BUY proceeds normally and the columns are left blank.
# Backtests have no GIFT Nifty history, so the columns stay blank there.
ENABLE_GIFT_NIFTY_LOG = True
GIFT_NIFTY_QUOTE_KEYS = ["NSEIX:GIFT NIFTY", "NSEIX:GIFTNIFTY"]
GIFT_NIFTY_CACHE_SECONDS = 5.0         # Reuse a recent quote so repeated gate passes don't spam the API
GIFT_NIFTY_ERROR_LOG_THROTTLE_SECONDS = 300.0

# Per-symbol stale-price protection (PAPERTRADE_LIVE): the websocket can stop
# delivering fresh ticks for one specific option leg (thin/illiquid strike,
# missed packet, etc.) while the overall connection and the spot leg keep
# ticking normally. The global tick-stall detector above does not catch this
# because it only tracks the time of the *last tick of any kind*. These
# settings drive a REST (kite.ltp) safety-net refresh scoped to the open
# position's own option symbol so SL/trailing/target evaluation is never
# blind for long even if that symbol's own tick stream silently stalls.
OPEN_POSITION_LTP_STALE_SECONDS = 20
OPEN_POSITION_LTP_REFRESH_COOLDOWN_SECONDS = 10

# Live order execution
ENABLE_LIVE_ORDERS = True   # Safety switch: must be True and RUN_MODE = "LIVE_ORDER" to place real orders
LIVE_ORDER_PRODUCT = "MIS"  # MIS for intraday, NRML for carry
LIVE_ORDER_VARIETY = "regular"
LIVE_ORDER_ORDER_TYPE = "MARKET"
ENABLE_MARKET_PROTECTION = True
LIVE_ORDER_MARKET_PROTECTION = -1  # 0=no protection, 1-100=custom %, -1=automatic protection by Kite
LIVE_ORDER_TRIGGER_PRICE_BUFFER = 0.05  # trigger buffer for broker-side emergency SL order
LIVE_ORDER_TAG = "RP_ALGO"
ENABLE_MARKET_TO_LIMIT_FALLBACK = True
MARKET_TO_LIMIT_BUFFER_PCT = 0.5
OPTION_TICK_SIZE = 0.05
ENABLE_COVER_ORDER_ENTRY = False  # If True, place BUY as Cover Order (CO) with broker-attached SL trigger — CO not allowed for NFO
ENABLE_EMERGENCY_SL_ORDER = True  # If True, place a separate SL order immediately after entry to act as a backstop in case of manual exit or broker issues
BROKER_SYNC_POLL_SECONDS = 2.0
BROKER_ORDER_POLL_RETRIES = 20
BROKER_ORDER_POLL_INTERVAL_SECONDS = 1.0

# Base data interval from Kite
BASE_INTERVAL = "minute"

# =========================
# STRATEGY PARAMETERS
# =========================
TRACKING_START = EDITABLE_CONFIG["TRACKING_START"]
NO_NEW_ENTRY_AFTER = EDITABLE_CONFIG["NO_NEW_ENTRY_AFTER"]
FORCE_EXIT = EDITABLE_CONFIG["FORCE_EXIT"]

# 1-minute entry model
EMA_LEN = 20
VOL_LOOKBACK = 10
SWING_LOOKBACK = 5
OPTION_CONFIRM_BARS = 1
VWAP_DISTANCE_PCT = 0.10
VOL_MULT = 1.05
ENTRY_SCORE_THRESHOLD = _VARIANT.ENTRY_SCORE_THRESHOLD  # set per variant in strategy/variants/
OPPOSITE_EXIT_SCORE = 5

# 5-minute trend filter
MTF_INTERVAL = EDITABLE_CONFIG["MTF_INTERVAL"]
MTF_MODE = EDITABLE_CONFIG["MTF_MODE"]
MTF_EMA_LEN = 20
MTF_STRICTNESS = EDITABLE_CONFIG["MTF_STRICTNESS"]  # Same source live mode reloads on each tick.
MTF_STRICTNESS_DEBUG = False  # Set to True to see detailed MTF trend calculations
GAP_CONFIRMATION_THRESHOLD_PCT = 0.20
GAP_DELAY_THRESHOLD_PCT = 0.40
GAP_DELAY_ENTRY_TIME = "09:30"

# Risk / trade management
FIXED_SL_POINTS = EDITABLE_CONFIG["FIXED_SL_POINTS"]
BREAKEVEN_TRIGGER = 0.20
TRAIL_TRIGGER = 0.40
TRAIL_PCT = 0.10
MIN_PREMIUM = 10.0

IST = pytz.timezone("Asia/Kolkata")
_BROKER_SYNC_RETRY_LOG: Dict[str, datetime] = {}
_LAST_AUTO_MTF_LOG_KEY = None


# =========================
# HELPERS
# =========================
def normalize_run_mode(mode: str) -> str:
    mode = str(mode).strip().upper()
    valid = {"BACKTEST", "PAPERTRADE_LIVE", "LIVE_ORDER"}
    if mode not in valid:
        raise ValueError(f"RUN_MODE must be one of {valid}, got: {mode}")
    return mode


_LIVE_PROCESS_LOCK_HANDLE = None


def acquire_live_process_lock(run_mode: str) -> bool:
    """Allow only one live broker session across all strategy versions."""
    global _LIVE_PROCESS_LOCK_HANDLE
    if run_mode != "LIVE_ORDER":
        return True

    lock_path = os.path.join(SCRIPTS_DIR, "live_order_strategy.lock")
    handle = None
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(" ")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} mode={run_mode}\n")
        handle.flush()
        _LIVE_PROCESS_LOCK_HANDLE = handle
        return True
    except (OSError, IOError):
        if handle is not None:
            handle.close()
        print(
            f"[START BLOCKED] A live strategy process is already running; "
            f"not starting another {run_mode} process."
        )
        return False


def make_entry_event_id(side: object, entry_time: object, option_symbol: object) -> str:
    return f"{str(side or '').upper()}|{entry_time}|{str(option_symbol or '').upper()}"


def validate_kite_login_on_start(kite: KiteConnect, run_mode: str) -> None:
    active_mode = normalize_run_mode(run_mode)
    if not API_KEY or not ACCESS_TOKEN:
        print("Kite login validation failed: API_KEY or ACCESS_TOKEN is missing.")
        print(f"Run generate_token.py and update .env before starting {active_mode} mode.")
        raise SystemExit(1)

    try:
        print(f"Validating Kite login for {active_mode} startup...")
        profile = kite.profile()
        user_name = profile.get("user_name") or profile.get("user_id") or "unknown user"
        print(f"Kite login validated for {user_name}")
    except Exception as exc:
        print(f"Kite login validation failed: {exc}")
        print("Run generate_token.py and retry with a fresh ACCESS_TOKEN.")
        raise SystemExit(1)

def mtm_guard_status(current_mtm_rs: float) -> tuple[bool, str]:
    global profit_lock_active, profit_lock_level, completed_trades_for_profit_lock

    if ENABLE_PROFIT_LOCK:
        if (
            (not profit_lock_active)
            and completed_trades_for_profit_lock >= PROFIT_LOCK_AFTER_TRADE_NO
            and current_mtm_rs >= PROFIT_LOCK_TRIGGER_RS
        ):
            profit_lock_active = True
            profit_lock_level = current_mtm_rs * PROFIT_LOCK_PERCENT

        if profit_lock_active and current_mtm_rs <= profit_lock_level:
            return True, "PROFIT_LOCK_HIT"

    if current_mtm_rs >= DAILY_PROFIT_TARGET_RS:
        return True, "DAILY_PROFIT_TARGET"
    if current_mtm_rs <= DAILY_LOSS_LIMIT_RS:
        return True, "DAILY_LOSS_LIMIT"
    return False, ""

profit_lock_active = False
profit_lock_level = 0.0
completed_trades_for_profit_lock = 0

def parse_hhmm_to_time(value: str) -> dtime:
    return datetime.strptime(str(value).strip(), "%H:%M").time()


def safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def get_active_trailing_stages(current_time=None, stage1_triggered: bool = False) -> list:
    """Return active trailing-profit stage list based on time.

    Switch to STAGE2 unconditionally once time >= TRAILING_PROFIT_STAGE2_TIME.
    Because get_trailing_stop_for_profit always uses max(), switching to the
    tighter STAGE2 mid-trade can only advance the trailing stop, never lower it.
    """
    if current_time is not None:
        try:
            t_str = current_time.strftime("%H:%M") if hasattr(current_time, "strftime") else str(current_time)
            if t_str >= TRAILING_PROFIT_STAGE2_TIME:
                return TRAILING_PROFIT_STAGE2
        except Exception:
            pass
    return TRAILING_PROFIT_STAGE1


def get_trailing_stop_for_profit(entry_price: float, pnl_points: float, current_stop: float, current_time=None, stage1_triggered: bool = False) -> float:
    trailing_stop = current_stop
    for profit_target, stop_profit in get_active_trailing_stages(current_time, stage1_triggered):
        if pnl_points >= float(profit_target):
            trailing_stop = max(trailing_stop, entry_price + float(stop_profit))
    return trailing_stop


def get_trailing_profit_status(pnl_points: float, current_time=None, stage1_triggered: bool = False) -> str:
    active_stages = get_active_trailing_stages(current_time, stage1_triggered)
    stage_set = "S2" if active_stages is TRAILING_PROFIT_STAGE2 else "S1"
    triggered = []
    for idx, (profit_target, stop_profit) in enumerate(active_stages, start=1):
        if pnl_points >= float(profit_target):
            triggered.append(f"{stage_set}:{idx}:{int(float(profit_target))}->{int(float(stop_profit))}")
    return ",".join(triggered)


def update_stage2_peak(prev_peak, price, current_time=None):
    """Highest option price since the stage-2 switch; None while stage 1 is active."""
    if get_active_trailing_stages(current_time) is not TRAILING_PROFIT_STAGE2:
        return None
    price = safe_float(price, 0.0)
    prev = safe_float(prev_peak, 0.0)
    peak = max(prev, price)
    return peak if peak > 0 else None


def get_trailing_peak_points(entry_price: float, best_price, stage2_peak, current_time=None) -> float:
    """Peak profit that drives the active trailing ladder.

    Stage 2 counts only the peak reached since it became active, so a stage-1
    peak cannot place the new stop above the market at the switch.
    """
    if get_active_trailing_stages(current_time) is TRAILING_PROFIT_STAGE2:
        peak = safe_float(stage2_peak, 0.0)
        return peak - entry_price if peak > 0 else 0.0
    return safe_float(best_price, entry_price) - entry_price


def get_entry_sl_points(entry_number: int = 1) -> float:
    return safe_float(FIXED_SL_POINTS, 0.0)


def get_entry_stop_price(entry_price: float, entry_number: int = 1) -> float:
    """Calculate the initial entry stop price using fixed points only."""
    entry_price = safe_float(entry_price, 0.0)
    if entry_price <= 0:
        return 0.0

    fixed_sl_points = get_entry_sl_points(entry_number)
    if fixed_sl_points > 0:
        stop_price = max(OPTION_TICK_SIZE, entry_price - fixed_sl_points)
    else:
        stop_price = 0.0

    return round_to_tick(stop_price, OPTION_TICK_SIZE)


def safe_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def round_to_tick(price: float, tick_size: float = OPTION_TICK_SIZE) -> float:
    p = max(float(tick_size), safe_float(price, float(tick_size)))
    t = max(0.01, safe_float(tick_size, 0.05))
    return round(round(p / t) * t, 2)


def build_fallback_limit_price(kite: KiteConnect, tradingsymbol: str, transaction_type: str) -> float:
    ltp = get_option_symbol_ltp(kite, tradingsymbol, OPTION_EXCHANGE)
    if ltp <= 0:
        raise RuntimeError(f"Could not fetch LTP for fallback LIMIT order: {tradingsymbol}")
    buffer_pct = max(0.0, safe_float(MARKET_TO_LIMIT_BUFFER_PCT, 0.5)) / 100.0
    tx = str(transaction_type).upper()
    if tx == str(kite.TRANSACTION_TYPE_BUY).upper() or tx == "BUY":
        raw_price = ltp * (1.0 + buffer_pct)
    else:
        raw_price = ltp * (1.0 - buffer_pct)
    return round_to_tick(max(OPTION_TICK_SIZE, raw_price), OPTION_TICK_SIZE)


def get_public_ip_addresses(timeout_seconds: int = 5) -> Dict[str, str]:
    services = {
        "ipv4": "https://api.ipify.org?format=json",
        "ipv6": "https://api64.ipify.org?format=json",
    }
    out: Dict[str, str] = {}
    for key, url in services.items():
        try:
            with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
                ip_value = str((payload or {}).get("ip") or "").strip()
                out[key] = ip_value
        except Exception:
            out[key] = ""
    return out


def print_live_order_preflight() -> None:
    if RUN_MODE != "LIVE_ORDER":
        return

    active_api_key = str(API_KEY or "")
    if len(active_api_key) >= 6:
        api_key_masked = f"{active_api_key[:3]}...{active_api_key[-3:]}"
    else:
        api_key_masked = active_api_key or "unavailable"

    ip_info = get_public_ip_addresses()
    ipv4 = ip_info.get("ipv4") or "unavailable"
    ipv6 = ip_info.get("ipv6") or "unavailable"
    print("Live-order preflight:")
    print(f"- Market protection enabled: {ENABLE_MARKET_PROTECTION}")
    if ENABLE_MARKET_PROTECTION:
        print(f"- Market protection value: {LIVE_ORDER_MARKET_PROTECTION}")
    print(f"- Active API key: {api_key_masked}")
    print(f"- Public IPv4: {ipv4}")
    print(f"- Public IPv6: {ipv6}")
    print("- Ensure your active public IP is allowlisted in Kite developer console before placing orders.")


def get_last_price(kite: KiteConnect, instrument_token: int, exchange: str = "NSE") -> float:
    # Prefer spot helper for configured underlying; fall back to direct LTP keys.
    if safe_int(instrument_token, 0) == safe_int(UNDERLYING_TOKEN, -1):
        try:
            return get_spot_ltp(kite, UNDERLYING_NAME)
        except Exception:
            pass

    keys = [f"{exchange}:{instrument_token}", str(instrument_token)]
    for key in keys:
        try:
            ltp_response = kite.ltp([key])
        except Exception:
            continue
        if not isinstance(ltp_response, dict) or not ltp_response:
            continue
        row = ltp_response.get(key) or next(iter(ltp_response.values()), None)
        if isinstance(row, dict) and row.get("last_price") is not None:
            return safe_float(row.get("last_price"), 0.0)

    return 0.0


def get_option_symbol_ltp(kite: KiteConnect, tradingsymbol: str, exchange: str = OPTION_EXCHANGE) -> float:
    symbol = str(tradingsymbol or "").strip()
    if not symbol:
        return 0.0
    key = f"{exchange}:{symbol}"
    try:
        ltp_response = kite.ltp([key])
    except Exception:
        return 0.0
    if not isinstance(ltp_response, dict) or not ltp_response:
        return 0.0
    row = ltp_response.get(key) or next(iter(ltp_response.values()), None)
    if isinstance(row, dict) and row.get("last_price") is not None:
        return safe_float(row.get("last_price"), 0.0)
    return 0.0


def normalize_order_status(status: str) -> str:
    return str(status or '').strip().upper()


def is_transient_kite_api_error(exc: Optional[Exception]) -> bool:
    if exc is None:
        return False
    message = " ".join(
        part.lower()
        for part in [
            type(exc).__name__,
            str(exc),
            repr(exc),
        ]
        if part
    )
    transient_tokens = (
        "readtimeout",
        "timeout",
        "timed out",
        "timedout",
        "502 bad gateway",
        "503 service unavailable",
        "504 gateway timeout",
        "bad gateway",
        "gateway timeout",
        "unknown content-type",
        "connection reset",
        "connection aborted",
        "connection error",
        "connectionpool",
        "temporarily unavailable",
        "server disconnected",
        "socket timeout",
        "remote disconnected",
        "ssl",
        "network unreachable",
        "brokenpipe",
        "reset by peer",
        "connection refused",
    )
    return any(token in message for token in transient_tokens)


def is_insufficient_funds_error(exc: Optional[Exception]) -> bool:
    if exc is None:
        return False
    message = " ".join(
        part.lower()
        for part in [type(exc).__name__, str(exc), repr(exc)]
        if part
    )
    return "insufficient funds" in message or (
        "margin required" in message and "margin available" in message
    )


def log_broker_sync_retry(location: str, context: str, retry_seconds: float = BROKER_SYNC_POLL_SECONDS, throttle_minutes: float = 1.0) -> None:
    now = datetime.now(IST)
    key = f"{location}|{context}"
    last_logged = _BROKER_SYNC_RETRY_LOG.get(key)
    if last_logged is not None:
        if (now - last_logged).total_seconds() < max(15.0, throttle_minutes * 60.0):
            return
    _BROKER_SYNC_RETRY_LOG[key] = now
    print(
        f"[BROKER SYNC RETRY] {now.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
        f"location={location} | retry_in={retry_seconds:.1f}s | reason={context}"
    )


def get_order_history(kite: KiteConnect, order_id: str) -> List[Dict]:
    try:
        hist = kite.order_history(order_id)
        return hist if isinstance(hist, list) else []
    except Exception as exc:
        if is_transient_kite_api_error(exc):
            print(f"[WARN] order_history transient Kite API error for order_id={order_id}: {exc}")
        return []


def get_order_snapshot(kite: KiteConnect, order_id: str) -> Dict:
    try:
        orders = kite.orders() or []
    except Exception as exc:
        if is_transient_kite_api_error(exc):
            log_broker_sync_retry("get_order_snapshot", f"order_id={order_id} reason={exc}")
            return {}
        raise

    if isinstance(orders, list):
        for order in reversed(orders):
            if str(order.get('order_id')) == str(order_id):
                return dict(order)
    hist = get_order_history(kite, order_id)
    if hist:
        return dict(hist[-1])
    return {}


def wait_for_order_terminal(kite: KiteConnect, order_id: str, retries: int = BROKER_ORDER_POLL_RETRIES, poll_seconds: float = BROKER_ORDER_POLL_INTERVAL_SECONDS) -> Dict:
    last = {}
    for _ in range(max(1, retries)):
        last = get_order_snapshot(kite, order_id)
        status = normalize_order_status(last.get('status'))
        if status in {'COMPLETE', 'REJECTED', 'CANCELLED'}:
            return last
        time.sleep(poll_seconds)
    return last


def extract_avg_price(order_row: Dict) -> float:
    for key in ('average_price', 'average_price_filled', 'price'):
        if order_row.get(key) not in (None, ''):
            return safe_float(order_row.get(key))
    return 0.0


def extract_order_time(order_row: Dict):
    for key in ('exchange_update_timestamp', 'exchange_timestamp', 'order_timestamp', 'exchange_timestamp', 'updated_at'):
        val = order_row.get(key)
        if val:
            ts = pd.Timestamp(val)
            if hasattr(ts, 'to_pydatetime'):
                ts = ts.to_pydatetime()
            if ts.tzinfo is None:
                ts = IST.localize(ts)
            else:
                ts = ts.astimezone(IST)
            return ts
    return datetime.now(IST)


def cancel_order_if_open(kite: KiteConnect, order_id: str, variety: str = LIVE_ORDER_VARIETY) -> bool:
    if not order_id:
        return False
    snap = get_order_snapshot(kite, order_id)
    status = normalize_order_status(snap.get('status'))
    if status in {'COMPLETE', 'CANCELLED', 'REJECTED'}:
        return False
    try:
        kite.cancel_order(variety=variety, order_id=order_id)
        return True
    except Exception:
        return False


def cancel_order_and_wait(kite: KiteConnect, order_id: str) -> None:
    oid = str(order_id or '').strip()
    if oid and cancel_order_if_open(kite, oid):
        for _ in range(10):
            time.sleep(0.2)
            status = normalize_order_status(get_order_snapshot(kite, oid).get('status'))
            if status in {'CANCELLED', 'REJECTED', 'COMPLETE'}:
                break


def restore_emergency_sl_order(kite: KiteConnect, broker_entry: Dict, tradingsymbol: str) -> None:
    """Re-arm the broker-side SL after a failed exit so an open position is not left unprotected."""
    if not str(broker_entry.get("emergency_sl_order_id") or "").strip():
        return
    try:
        open_qty = get_net_position_qty(kite, tradingsymbol)
        entry_price = safe_float(broker_entry.get("entry_price_actual"), 0.0)
        if open_qty <= 0 or entry_price <= 0:
            return
        broker_entry.update(place_emergency_sl_order(kite, tradingsymbol, open_qty, entry_price))
        print(f"[EMERGENCY SL RESTORED] {tradingsymbol} qty={open_qty} order_id={broker_entry.get('emergency_sl_order_id')}")
    except Exception as exc:
        print(f"[EMERGENCY SL WARNING] Could not restore SL for {tradingsymbol} after failed exit: {exc}. Check the broker terminal.")



def get_net_position_qty(kite: KiteConnect, tradingsymbol: str, exchange: str = OPTION_EXCHANGE, product: str = LIVE_ORDER_PRODUCT) -> int:
    try:
        positions = kite.positions() or {}
    except Exception as exc:
        if is_transient_kite_api_error(exc):
            log_broker_sync_retry("get_net_position_qty", f"symbol={tradingsymbol} reason={exc}")
            raise
        return 0
    rows = []
    for bucket in ('net', 'day'):
        data = positions.get(bucket) or []
        if isinstance(data, list):
            rows.extend(data)
    for row in rows:
        if str(row.get('tradingsymbol')) == str(tradingsymbol) and str(row.get('exchange')) == str(exchange) and str(row.get('product')) == str(product):
            return safe_int(row.get('quantity') or row.get('net_quantity') or 0, 0)
    return 0


def get_dynamic_emergency_sl_points(entry_price: float, entry_number: int = 1) -> float:
    return get_entry_sl_points(entry_number)

def has_any_open_local_position(live_state: Dict) -> bool:
    """Return True when the configured concurrent-position cap is reached."""
    open_positions = 0
    for _, broker_entry in (live_state.get("broker_positions") or {}).items():
        if safe_int(broker_entry.get("broker_open_qty"), 0) > 0:
            open_positions += 1
            if open_positions >= safe_int(MAX_CONCURRENT_TRADES, 1):
                return True
    return False


def get_open_local_position_count(live_state: Dict) -> int:
    open_positions = 0
    for _, broker_entry in (live_state.get("broker_positions") or {}).items():
        if safe_int(broker_entry.get("broker_open_qty"), 0) > 0:
            open_positions += 1
    return open_positions


def has_any_open_broker_position(kite: KiteConnect) -> bool:
    try:
        positions = kite.positions() or {}
    except Exception:
        return False

    rows = []
    for bucket in ("net", "day"):
        data = positions.get(bucket) or []
        if isinstance(data, list):
            rows.extend(data)

    for row in rows:
        qty = safe_int(row.get("quantity") or row.get("net_quantity") or 0, 0)
        if qty != 0 and str(row.get("exchange")) == OPTION_EXCHANGE and str(row.get("product")) == LIVE_ORDER_PRODUCT:
            return True
    return False


def place_emergency_sl_order(kite: KiteConnect, tradingsymbol: str, qty: int, entry_price: float, entry_number: int = 1) -> Dict:
    if (not ENABLE_LIVE_ORDERS) or (not ENABLE_EMERGENCY_SL_ORDER):
        return {}
    sl_points = get_dynamic_emergency_sl_points(entry_price, entry_number)
    sl_price = round(max(0.05, entry_price - sl_points), 1)
    trigger_price = round(max(0.05, sl_price + LIVE_ORDER_TRIGGER_PRICE_BUFFER), 1)
    order_id = place_kite_order_with_market_protection(kite, dict(
        variety=LIVE_ORDER_VARIETY,
        exchange=OPTION_EXCHANGE,
        tradingsymbol=tradingsymbol,
        transaction_type=kite.TRANSACTION_TYPE_SELL,
        quantity=int(qty),
        product=LIVE_ORDER_PRODUCT,
        # A stop-limit order can be filled or rejected at an unexpected
        # price during a fast move. SL-M prevents the protective order from
        # becoming executable before the configured trigger is reached.
        order_type='SL-M',
        trigger_price=trigger_price,
        validity='DAY',
        tag=LIVE_ORDER_TAG,
    ))
    return {
        'emergency_sl_order_id': order_id,
        'emergency_sl_price': sl_price,
        'emergency_sl_trigger_price': trigger_price,
        'emergency_sl_pct_used': sl_points,
        'emergency_sl_order_type': 'SL-M',
    }


def sync_broker_position_state(kite: KiteConnect, broker_entry: Dict) -> Dict:
    if not broker_entry:
        return {}
    out = dict(broker_entry)
    option_symbol = str(out.get('option_symbol') or out.get('entry_order_symbol') or '')
    qty = safe_int(out.get('qty') or QTY, QTY)

    try:
        open_qty = get_net_position_qty(kite, option_symbol)
    except Exception as exc:
        if is_transient_kite_api_error(exc):
            log_broker_sync_retry("sync_broker_position_state", f"symbol={option_symbol} reason={exc}")
            return out
        raise
    out['broker_open_qty'] = open_qty
    exit_detected = open_qty <= 0

    emergency_sl_order_id = str(out.get('emergency_sl_order_id') or '').strip()
    if emergency_sl_order_id:
        sl_snap = get_order_snapshot(kite, emergency_sl_order_id)
        out['emergency_sl_order_status'] = normalize_order_status(sl_snap.get('status'))
        if out['emergency_sl_order_status'] == 'COMPLETE':
            out['manual_or_broker_exit_detected'] = True
            out['broker_exit_reason'] = 'EMERGENCY_SL_HIT'
            out['broker_exit_order_id'] = emergency_sl_order_id
            out['broker_exit_avg_price'] = extract_avg_price(sl_snap)
            out['broker_exit_time'] = extract_order_time(sl_snap)
            return out

    if exit_detected:
        out['manual_or_broker_exit_detected'] = True
        exit_order_id = str(out.get('exit_order_id') or '').strip()
        strategy_exit_complete = False
        if exit_order_id:
            exit_snap = get_order_snapshot(kite, exit_order_id)
            if normalize_order_status(exit_snap.get('status')) == 'COMPLETE':
                strategy_exit_complete = True
                out['broker_exit_order_id'] = exit_order_id
                out['broker_exit_avg_price'] = extract_avg_price(exit_snap)
                out['broker_exit_time'] = extract_order_time(exit_snap)

        if strategy_exit_complete:
            out['broker_exit_reason'] = 'STRATEGY_EXIT'
        else:
            out['broker_exit_reason'] = str(out.get('broker_exit_reason') or 'MANUAL_OR_EXTERNAL_EXIT')

        if not out.get('broker_exit_order_id'):
            try:
                orders = kite.orders() or []
            except Exception as exc:
                if is_transient_kite_api_error(exc):
                    log_broker_sync_retry("sync_broker_position_state", f"symbol={option_symbol} fallback_order_list reason={exc}")
                    return out
                raise
            entry_time = out.get('entry_time')
            entry_ts = pd.Timestamp(entry_time) if entry_time is not None else None
            if entry_ts is not None and hasattr(entry_ts, 'to_pydatetime'):
                entry_ts = entry_ts.to_pydatetime()
            if entry_ts is not None and entry_ts.tzinfo is None:
                entry_ts = IST.localize(entry_ts)
            for order in reversed(orders):
                if str(order.get('tradingsymbol')) != option_symbol:
                    continue
                if normalize_order_status(order.get('status')) != 'COMPLETE':
                    continue
                if str(order.get('transaction_type')).upper() != 'SELL':
                    continue
                filled_qty = safe_int(order.get('filled_quantity') or order.get('quantity') or 0, 0)
                if filled_qty <= 0:
                    continue
                order_ts = extract_order_time(order)
                if entry_ts is not None and order_ts < entry_ts:
                    continue
                out['broker_exit_order_id'] = str(order.get('order_id') or '')
                out['broker_exit_avg_price'] = extract_avg_price(order)
                out['broker_exit_time'] = extract_order_time(order)
                break
    return out

def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    ha_close = (out["open"] + out["high"] + out["low"] + out["close"]) / 4.0

    ha_open = np.zeros(len(out))
    ha_open[0] = (out["open"].iloc[0] + out["close"].iloc[0]) / 2.0
    for i in range(1, len(out)):
        ha_open[i] = (ha_open[i - 1] + ha_close.iloc[i - 1]) / 2.0

    ha_high = np.vstack([out["high"].values, ha_open, ha_close.values]).max(axis=0)
    ha_low = np.vstack([out["low"].values, ha_open, ha_close.values]).min(axis=0)

    out["ha_open"] = ha_open
    out["ha_close"] = ha_close
    out["ha_high"] = ha_high
    out["ha_low"] = ha_low
    out["ha_green"] = out["ha_close"] > out["ha_open"]
    out["ha_red"] = out["ha_close"] < out["ha_open"]
    return out


def add_indicators(df: pd.DataFrame, ema_len: int, vol_lookback: int) -> pd.DataFrame:
    out = df.copy()
    out["ema"] = out["close"].ewm(span=ema_len, adjust=False).mean()
    out["ema_prev"] = out["ema"].shift(1)
    out["ema_slope_up"] = out["ema"] > out["ema_prev"]
    out["ema_slope_down"] = out["ema"] < out["ema_prev"]

    typical_price = (out["high"] + out["low"] + out["close"]) / 3.0
    tpv = typical_price * out["volume"]
    out["vwap"] = tpv.cumsum() / out["volume"].replace(0, np.nan).cumsum()
    out["vol_avg"] = out["volume"].rolling(vol_lookback).mean()
    out = heikin_ashi(out)
    return out


def fetch_history(kite: KiteConnect, token: int, trade_date: str, interval: str = "minute") -> pd.DataFrame:
    from_dt = f"{trade_date} 09:15:00"
    to_dt = f"{trade_date} 15:30:00"
    max_attempts = 4
    data = None
    for attempt in range(1, max_attempts + 1):
        try:
            data = kite.historical_data(
                instrument_token=token,
                from_date=from_dt,
                to_date=to_dt,
                interval=interval,
                continuous=False,
                oi=False,
            )
            break
        except DataException as exc:
            msg = str(exc).lower()
            is_transient = (
                "502" in msg
                or "503" in msg
                or "504" in msg
                or "bad gateway" in msg
                or "unknown content-type" in msg
                or "text/html" in msg
            )
            if is_transient and attempt < max_attempts:
                delay_sec = 1.5 * attempt
                print(
                    f"[WARN] historical_data transient error for token={token} "
                    f"attempt={attempt}/{max_attempts}; retrying in {delay_sec:.1f}s"
                )
                time.sleep(delay_sec)
                continue
            raise
        except Exception as exc:
            msg = str(exc).lower()
            is_transient = (
                "timeout" in msg
                or "tempor" in msg
                or "connection" in msg
                or "remote end closed" in msg
            )
            if is_transient and attempt < max_attempts:
                delay_sec = 1.5 * attempt
                print(
                    f"[WARN] historical_data network error for token={token} "
                    f"attempt={attempt}/{max_attempts}; retrying in {delay_sec:.1f}s"
                )
                time.sleep(delay_sec)
                continue
            raise

    df = pd.DataFrame(data)
    if df.empty:
        raise ValueError(f"No history returned for token {token}")
    df["date"] = pd.to_datetime(df["date"])
    if df["date"].dt.tz is None:
        df["date"] = df["date"].dt.tz_localize("UTC").dt.tz_convert(IST)
    else:
        df["date"] = df["date"].dt.tz_convert(IST)
    df = df.rename(columns={"date": "ts"})
    return df[["ts", "open", "high", "low", "close", "volume"]].copy()


def load_downloaded_option_history(symbol: str, trade_date: str) -> pd.DataFrame:
    """Load one option contract from the downloader's per-day CSV archive."""
    files = sorted(OPTION_DAILY_DIR.glob(f"nifty_weekly_*_{trade_date}.csv"))
    if not files:
        raise ValueError(
            f"No downloaded option file found for {trade_date} in {OPTION_DAILY_DIR}"
        )

    frames = []
    for path in files:
        frame = pd.read_csv(path, low_memory=False)
        if "tradingsymbol" not in frame.columns:
            continue
        matching = frame[frame["tradingsymbol"].astype(str) == str(symbol)].copy()
        if not matching.empty:
            frames.append(matching)

    if not frames:
        raise ValueError(
            f"No downloaded candles found for {symbol} on {trade_date} "
            f"in {OPTION_DAILY_DIR}"
        )

    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize(IST)
    else:
        df["ts"] = df["ts"].dt.tz_convert(IST)
    df = df.dropna(subset=["ts"]).sort_values("ts")
    df = df.drop_duplicates(subset=["ts"], keep="last")
    required = ["ts", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(
            f"Downloaded option file data for {symbol} is missing columns: {missing}"
        )
    return df[required].reset_index(drop=True)


def extract_option_strike(symbol: str) -> int:
    """Extract the strike from a NIFTY option tradingsymbol."""
    value = str(symbol or "").strip().upper()
    if not (value.endswith("CE") or value.endswith("PE")):
        return 0
    digits = ""
    for char in reversed(value[:-2]):
        if char.isdigit():
            digits = char + digits
        elif digits:
            break
    if not digits:
        return 0
    try:
        return int(digits) % 100000
    except Exception:
        return 0


def option_expiry_cutoff(trade_day):
    """Return the trade date as the expiry cutoff for the shared policy."""
    return trade_day


def select_spot_reference_from_history(spot: pd.DataFrame, trade_date: str, reference: str = "09:15"):
    """Resolve a deterministic spot reference from historical candles."""
    if spot is None or spot.empty or "close" not in spot.columns:
        raise ValueError("No spot history available to resolve the strike reference")
    ref = str(reference or "09:15").strip().lower()
    frame = spot.copy()
    if ref == "signal_close":
        valid = frame.dropna(subset=["close"])
        if "ts" in valid.columns:
            valid = valid.sort_values("ts", kind="stable")
        if valid.empty:
            raise ValueError("No valid spot close available for SIGNAL_CLOSE")
        latest = valid.iloc[-1]
        price = safe_float(latest.get("close"), 0.0)
        if price <= 0:
            raise ValueError("Invalid SIGNAL_CLOSE price")
        return price, latest.get("ts")
    if "ts" in frame.columns and len(ref) == 5 and ref.count(":") == 1:
        try:
            hh, mm = [int(x) for x in ref.split(":")]
            ts = pd.to_datetime(frame["ts"], errors="coerce")
            if getattr(ts.dt, "tz", None) is None:
                ts = ts.dt.tz_localize(IST)
            else:
                ts = ts.dt.tz_convert(IST)
            mask = (ts.dt.hour == hh) & (ts.dt.minute == mm)
            if mask.any():
                idx = mask[mask].index[0]
                price = safe_float(frame.at[idx, "close"], 0.0)
                if price > 0:
                    return price, ts.loc[idx]
        except Exception:
            pass
    # Deterministic fallback: first available market candle, never the latest/live quote.
    first = frame.dropna(subset=["close"]).iloc[0]
    price = safe_float(first.get("close"), 0.0)
    if price <= 0:
        raise ValueError("Invalid spot reference price")
    return price, first.get("ts")


def select_downloaded_option_symbols(spot: pd.DataFrame, trade_date: str, spot_price_override: Optional[float] = None, reference_ts_override=None):
    """Select a common CE/PE strike; optionally use a completed signal candle price."""
    files = sorted(OPTION_DAILY_DIR.glob(f"nifty_weekly_*_{trade_date}.csv"))
    if not files:
        raise ValueError(
            f"No downloaded option files found for {trade_date} in {OPTION_DAILY_DIR}"
        )

    trade_day = datetime.strptime(trade_date, "%Y-%m-%d").date()
    expiry_cutoff = option_expiry_cutoff(trade_day)
    expiry_by_file = {}
    for path in files:
        expiry_text = path.name[len("nifty_weekly_"):].split("_", 1)[0]
        try:
            expiry_by_file[path] = datetime.strptime(expiry_text, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Could not read option expiry from {path.name}") from exc

    eligible_expiries = sorted({e for e in expiry_by_file.values() if e > expiry_cutoff})
    if not eligible_expiries:
        raise ValueError(
            f"No downloaded option expiry after {expiry_cutoff} in {OPTION_DAILY_DIR}"
        )
    if AUTO_SELECT_EXPIRY:
        requested_expiry = datetime.strptime(AUTO_SELECT_EXPIRY, "%Y-%m-%d").date()
        if requested_expiry in eligible_expiries:
            selected_expiry = requested_expiry
        else:
            later_expiries = [e for e in eligible_expiries if e > requested_expiry]
            if not later_expiries:
                raise ValueError(f"No eligible downloaded expiry at/after {requested_expiry}")
            selected_expiry = later_expiries[0]
    else:
        selected_expiry = eligible_expiries[0]
    selected_files = [p for p in files if expiry_by_file[p] == selected_expiry]

    frames = []
    for path in selected_files:
        frame = pd.read_csv(path, low_memory=False)
        required = {"tradingsymbol", "strike", "option_type"}
        if required.issubset(frame.columns):
            if "expiry" in frame.columns:
                row_expiries = pd.to_datetime(frame["expiry"], errors="coerce").dt.date
                frame = frame.loc[row_expiries == selected_expiry]
            frames.append(frame[list(required)].drop_duplicates())
    if not frames:
        raise ValueError(f"No contract metadata found for expiry {selected_expiry}")

    contracts = pd.concat(frames, ignore_index=True).drop_duplicates()
    contracts["strike"] = pd.to_numeric(contracts["strike"], errors="coerce")
    contracts["option_type"] = contracts["option_type"].astype(str).str.upper()
    contracts = contracts.dropna(subset=["strike"])
    ce_strikes = set(contracts.loc[contracts["option_type"] == "CE", "strike"].tolist())
    pe_strikes = set(contracts.loc[contracts["option_type"] == "PE", "strike"].tolist())
    common_strikes = ce_strikes.intersection(pe_strikes)
    if not common_strikes:
        raise ValueError(f"No common CE/PE strikes found for expiry {selected_expiry}")

    if spot_price_override is not None:
        spot_price = safe_float(spot_price_override, 0.0)
        ref_ts = reference_ts_override
        if spot_price <= 0:
            raise ValueError("Invalid spot price supplied for downloaded strike selection")
    else:
        spot_price, ref_ts = select_spot_reference_from_history(
            spot, trade_date, AUTO_SELECT_SPOT_REF
        )
    selected_strike = min(common_strikes, key=lambda strike: (abs(float(strike) - spot_price), float(strike)))
    ce_rows = contracts[(contracts["option_type"] == "CE") & (contracts["strike"] == selected_strike)]
    pe_rows = contracts[(contracts["option_type"] == "PE") & (contracts["strike"] == selected_strike)]
    if ce_rows.empty or pe_rows.empty:
        raise ValueError(f"Could not resolve both option sides at common strike {selected_strike}")

    ce_symbol = str(ce_rows.iloc[0]["tradingsymbol"])
    pe_symbol = str(pe_rows.iloc[0]["tradingsymbol"])
    if extract_option_strike(ce_symbol) != extract_option_strike(pe_symbol):
        raise ValueError(f"Selected strike mismatch: CE={ce_symbol}, PE={pe_symbol}")
    print(
        f"[STRIKE AUDIT] mode={RUN_MODE} source=DOWNLOADED reference={AUTO_SELECT_SPOT_REF} "
        f"time={ref_ts} spot={spot_price:.2f} expiry={selected_expiry} "
        f"strike={selected_strike} CE={ce_symbol} PE={pe_symbol}"
    )
    return ce_symbol, pe_symbol


def fetch_prev_trading_day_close(kite: KiteConnect, token: int, trade_date: str) -> float:
    """Return the close price of the last 1-min candle of the previous trading day."""
    from datetime import date as _date, timedelta as _timedelta
    try:
        ref = _date.fromisoformat(trade_date)
    except Exception:
        return 0.0
    # Walk back up to 7 calendar days to find a trading day (skip weekends)
    prev = ref - _timedelta(days=1)
    for _ in range(7):
        if prev.weekday() < 5:  # Mon-Fri
            try:
                df = fetch_history(kite, token, prev.isoformat(), "minute")
                if df is not None and not df.empty:
                    return float(df.iloc[-1]["close"])
            except Exception:
                pass
        prev -= _timedelta(days=1)
    return 0.0


def fetch_prev_trading_day_ohlc(kite: KiteConnect, token: int, trade_date: str) -> Dict:
    """Return previous trading day OHLC for the underlying index."""
    from datetime import date as _date, timedelta as _timedelta
    try:
        ref = _date.fromisoformat(trade_date)
    except Exception:
        return {}

    prev = ref - _timedelta(days=1)
    for _ in range(7):
        if prev.weekday() < 5:
            try:
                df = fetch_history(kite, token, prev.isoformat(), "minute")
                if df is not None and not df.empty:
                    return {
                        "date": prev.isoformat(),
                        "open": safe_float(df.iloc[0].get("open"), 0.0),
                        "high": safe_float(df["high"].max(), 0.0),
                        "low": safe_float(df["low"].min(), 0.0),
                        "close": safe_float(df.iloc[-1].get("close"), 0.0),
                    }
            except Exception:
                pass
        prev -= _timedelta(days=1)
    return {}


def build_market_context(day: Optional[pd.DataFrame] = None, prev_high: float = 0.0, prev_low: float = 0.0, prev_close: float = 0.0) -> Dict:
    """Build index context used by logs: nifty OHLC summary, gap details, and CPR levels."""
    ctx: Dict[str, object] = {
        "nifty_open": "",
        "nifty_high": "",
        "nifty_low": "",
        "nifty_close": "",
        "nifty_prev_close": "",
        "nifty_change": "",
        "gap_type": "",
        "gap_points": "",
        "gap_pct": "",
        "is_gap_up": "",
        "cpr_pivot": "",
        "cpr_bc": "",
        "cpr_tc": "",
        "cpr_width": "",
        "cpr_width_pct": "",
        "cpr_type": "",
    }

    prev_high = safe_float(prev_high, 0.0)
    prev_low = safe_float(prev_low, 0.0)
    prev_close = safe_float(prev_close, 0.0)

    if day is not None and not day.empty:
        if "spot_open" in day.columns:
            ctx["nifty_open"] = int(round(float(day.iloc[0]["spot_open"])))
        if "spot_high" in day.columns:
            ctx["nifty_high"] = int(round(float(day["spot_high"].max())))
        if "spot_low" in day.columns:
            ctx["nifty_low"] = int(round(float(day["spot_low"].min())))
        if "spot_close" in day.columns:
            ctx["nifty_close"] = int(round(float(day.iloc[-1]["spot_close"])))

    if prev_close > 0:
        ctx["nifty_prev_close"] = int(round(prev_close))

    nifty_open_val = safe_float(ctx["nifty_open"], 0.0)
    nifty_close_val = safe_float(ctx["nifty_close"], 0.0)
    if prev_close > 0 and nifty_close_val > 0:
        chg_pts = nifty_close_val - prev_close
        chg_pct = (chg_pts / prev_close) * 100.0
        sign = "+" if chg_pts >= 0 else ""
        ctx["nifty_change"] = f"{sign}{chg_pts:.0f} pts ({sign}{chg_pct:.2f}%)"

    if prev_close > 0 and nifty_open_val > 0:
        gap_points = nifty_open_val - prev_close
        gap_pct = (gap_points / prev_close) * 100.0
        if gap_points > 0:
            gap_type = "GAP_UP"
        elif gap_points < 0:
            gap_type = "GAP_DOWN"
        else:
            gap_type = "FLAT"
        ctx["gap_type"] = gap_type
        ctx["gap_points"] = round(gap_points, 2)
        ctx["gap_pct"] = gap_pct
        ctx["is_gap_up"] = bool(gap_points > 0)

    if prev_high > 0 and prev_low > 0 and prev_close > 0:
        pivot = (prev_high + prev_low + prev_close) / 3.0
        bc = (prev_high + prev_low) / 2.0
        tc = (2.0 * pivot) - bc
        if bc > tc:
            bc, tc = tc, bc
        cpr_width = tc - bc
        cpr_width_pct = (cpr_width / pivot) * 100.0 if pivot else 0.0
        if cpr_width_pct < 0.30:
            cpr_type = "NARROW"
        elif cpr_width_pct < 0.70:
            cpr_type = "NORMAL"
        else:
            cpr_type = "WIDE"

        ctx["cpr_pivot"] = round(pivot, 2)
        ctx["cpr_bc"] = round(bc, 2)
        ctx["cpr_tc"] = round(tc, 2)
        ctx["cpr_width"] = round(cpr_width, 2)
        ctx["cpr_width_pct"] = round(cpr_width_pct, 2)
        ctx["cpr_type"] = cpr_type

    return ctx


def calculate_gap_pct(today_open: float, previous_close: float) -> Optional[float]:
    previous_close = safe_float(previous_close, 0.0)
    today_open = safe_float(today_open, 0.0)
    if previous_close <= 0 or today_open <= 0:
        return None
    return ((today_open - previous_close) / previous_close) * 100.0


def set_mtf_interval_from_gap(gap_pct: Optional[float], source: str = "") -> str:
    global MTF_INTERVAL, _LAST_AUTO_MTF_LOG_KEY
    if MTF_MODE == "MANUAL":
        return MTF_INTERVAL
    if gap_pct is None:
        return MTF_INTERVAL
    try:
        gap_pct = float(gap_pct)
    except (TypeError, ValueError):
        return MTF_INTERVAL
    if not np.isfinite(gap_pct):
        return MTF_INTERVAL
    previous_interval = MTF_INTERVAL
    MTF_INTERVAL = "1min" if abs(gap_pct) < GAP_CONFIRMATION_THRESHOLD_PCT else "5min"
    suffix = f" source={source}" if source else ""
    log_key = (str(source), round(float(gap_pct), 4), MTF_INTERVAL)
    if log_key != _LAST_AUTO_MTF_LOG_KEY and (source != "live" or previous_interval != MTF_INTERVAL or not _LAST_AUTO_MTF_LOG_KEY):
        print(f"[AUTO MTF] gap_pct={gap_pct:.2f}% -> interval={MTF_INTERVAL}{suffix}")
        _LAST_AUTO_MTF_LOG_KEY = log_key
    return MTF_INTERVAL


def apply_gap_entry_policy(
    gap_pct: Optional[float],
    base_tracking_start: pd.Timestamp,
    session_trade_date: str,
    source: str = "",
) -> pd.Timestamp:
    """Apply gap confirmation and delayed-entry rules consistently.

    Small gaps use the normal entry flow. Moderate gaps use the 5-minute
    confirmation interval. Large gaps additionally block entries until 09:30.
    MANUAL MTF mode continues to honor its configured interval.
    """
    if gap_pct in (None, ""):
        return base_tracking_start
    try:
        gap_abs = abs(float(gap_pct))
    except (TypeError, ValueError):
        return base_tracking_start
    if not np.isfinite(gap_abs):
        return base_tracking_start

    if MTF_MODE != "MANUAL" and source != "backtest":
        set_mtf_interval_from_gap(gap_abs, source=source)

    if gap_abs >= GAP_DELAY_THRESHOLD_PCT:
        delay_start = pd.Timestamp(
            f"{session_trade_date} {GAP_DELAY_ENTRY_TIME}",
            tz=IST,
        )
        # The candle timestamp is the candle's close/entry timestamp.  A
        # delayed entry configured for 09:30 must therefore start on the
        # following minute, not on the 09:30 candle itself.
        return max(base_tracking_start, delay_start + pd.Timedelta(minutes=1))
    return base_tracking_start


def resolve_token_from_instruments(kite: KiteConnect, tradingsymbol: str, exchange: str = "NFO") -> int:
    instruments = kite.instruments(exchange)
    for inst in instruments:
        if inst.get("tradingsymbol") == tradingsymbol:
            return int(inst["instrument_token"])
    raise ValueError(f"Could not resolve token for {tradingsymbol} on {exchange}")


_instruments_cache: Dict[str, pd.DataFrame] = {}
_last_spot_ltp: Dict[str, tuple[float, float]] = {}
SPOT_LTP_RETRY_COUNT = 1
SPOT_LTP_CACHE_SECONDS = 15.0

def get_instruments_df(kite: KiteConnect, exchange: str = "NFO") -> pd.DataFrame:
    global _instruments_cache
    if exchange not in _instruments_cache:
        instruments = pd.DataFrame(kite.instruments(exchange))
        if "expiry" in instruments.columns:
            instruments["expiry"] = pd.to_datetime(instruments["expiry"], errors="coerce")
        _instruments_cache[exchange] = instruments
    return _instruments_cache[exchange].copy()


def round_to_step(value: float, step: int) -> int:
    step_value = float(step)
    if step_value <= 0:
        raise ValueError(f"Strike step must be positive, got {step}")
    return int(round(float(value) / step_value) * step_value)


def get_spot_ltp(kite: KiteConnect, underlying_name: str) -> float:
    # Kite LTP/quote expects exchange:tradingsymbol keys for index lookup.
    name = str(underlying_name or "").strip().upper()
    if name == "NIFTY":
        candidate_keys = ["NSE:NIFTY 50", "NSE:NIFTY50", "NSE:NIFTY"]
    elif name == "BANKNIFTY":
        candidate_keys = ["NSE:NIFTY BANK", "NSE:BANKNIFTY"]
    elif name == "FINNIFTY":
        candidate_keys = ["NSE:NIFTY FIN SERVICE", "NSE:FINNIFTY"]
    else:
        candidate_keys = [f"NSE:{name}", f"NSE:{name} 50"]

    def remember(price: float) -> float:
        _last_spot_ltp[name] = (price, time.monotonic())
        return price

    # 1) Try LTP using exchange:tradingsymbol keys only. A transient timeout
    # should not immediately force the strategy to abandon a recent price.
    for key_index, key in enumerate(candidate_keys):
        for attempt in range(SPOT_LTP_RETRY_COUNT + 1):
            try:
                ltp_response = kite.ltp(key)
                if isinstance(ltp_response, dict) and ltp_response:
                    row = ltp_response.get(key) or next(iter(ltp_response.values()), None)
                    if isinstance(row, dict) and row.get("last_price") not in (None, ""):
                        price = float(row["last_price"])
                        if price > 0:
                            return remember(price)
            except Exception as exc:
                print(f"[get_spot_ltp] ltp failed for {key} (attempt {attempt + 1}): {exc}")
                if attempt < SPOT_LTP_RETRY_COUNT:
                    time.sleep(0.25)
        if key_index == 0:
            print(f"[get_spot_ltp] primary LTP key failed; trying fallback key for {name}")

    # 2) Try quote() with the same keys.
    for key in candidate_keys:
        try:
            quote_response = kite.quote(key)
            if isinstance(quote_response, dict) and quote_response:
                row = quote_response.get(key) or next(iter(quote_response.values()), None)
                if isinstance(row, dict) and row.get("last_price") not in (None, ""):
                    price = float(row["last_price"])
                    if price > 0:
                        return remember(price)
        except Exception as exc:
            print(f"[get_spot_ltp] quote failed for {key}: {exc}")

    cached = _last_spot_ltp.get(name)
    if cached and (time.monotonic() - cached[1]) <= SPOT_LTP_CACHE_SECONDS:
        print(f"[get_spot_ltp] using cached price for {name}: {cached[0]:.2f}")
        return cached[0]

    raise ValueError(
        f"Could not fetch spot LTP for {underlying_name}. "
        f"Tried only exchange:tradingsymbol keys: {candidate_keys}"
    )


def get_spot_open_for_today(kite: KiteConnect, underlying_name: str) -> float:
    """Return today's index open using quote fallback keys; 0.0 if unavailable."""
    name = str(underlying_name or "").strip().upper()
    if name == "NIFTY":
        candidate_keys = ["NSE:NIFTY 50", "NSE:NIFTY50", "NSE:NIFTY"]
    elif name == "BANKNIFTY":
        candidate_keys = ["NSE:NIFTY BANK", "NSE:BANKNIFTY"]
    elif name == "FINNIFTY":
        candidate_keys = ["NSE:NIFTY FIN SERVICE", "NSE:FINNIFTY"]
    else:
        candidate_keys = [f"NSE:{name}", f"NSE:{name} 50"]

    for key in candidate_keys:
        try:
            quote_response = kite.quote(key)
            if not isinstance(quote_response, dict) or not quote_response:
                continue
            row = quote_response.get(key) or next(iter(quote_response.values()), None)
            if not isinstance(row, dict):
                continue
            ohlc = row.get("ohlc", {})
            open_price = ohlc.get("open") if isinstance(ohlc, dict) else None
            if open_price not in (None, ""):
                value = float(open_price)
                if value > 0:
                    return value
        except Exception:
            continue
    return 0.0


_gift_nifty_cache: Dict[str, object] = {"snapshot": None, "fetched_at": 0.0, "last_error_log_at": 0.0}


def get_gift_nifty_snapshot(kite: KiteConnect, spot_price: float = 0.0) -> Dict[str, float]:
    """Return GIFT Nifty LTP/change context for logging. Never raises.

    Keys: gift_nifty_ltp, gift_nifty_change_pts, gift_nifty_change_pct,
    gift_nifty_premium (GIFT LTP minus the given NIFTY spot). Values are NaN
    when unavailable, so callers can always pass them straight to the event row.
    """
    empty = {
        "gift_nifty_ltp": np.nan,
        "gift_nifty_change_pts": np.nan,
        "gift_nifty_change_pct": np.nan,
        "gift_nifty_premium": np.nan,
    }
    if not ENABLE_GIFT_NIFTY_LOG or kite is None:
        return empty

    now_mono = time.monotonic()
    snapshot = _gift_nifty_cache.get("snapshot")
    if not snapshot or (now_mono - safe_float(_gift_nifty_cache.get("fetched_at"), 0.0)) > GIFT_NIFTY_CACHE_SECONDS:
        snapshot = None
        try:
            quotes = kite.quote(GIFT_NIFTY_QUOTE_KEYS) or {}
            row = next((quotes.get(key) for key in GIFT_NIFTY_QUOTE_KEYS if quotes.get(key)), None) or {}
            ltp = safe_float(row.get("last_price"), 0.0)
            if ltp <= 0:
                raise ValueError("empty GIFT Nifty quote")
            prev_close = safe_float((row.get("ohlc") or {}).get("close"), 0.0)
            change_pts = ltp - prev_close if prev_close > 0 else np.nan
            change_pct = (change_pts / prev_close * 100.0) if prev_close > 0 else np.nan
            snapshot = {"ltp": ltp, "change_pts": change_pts, "change_pct": change_pct}
            _gift_nifty_cache["snapshot"] = snapshot
            _gift_nifty_cache["fetched_at"] = now_mono
        except Exception as exc:
            if (now_mono - safe_float(_gift_nifty_cache.get("last_error_log_at"), 0.0)) > GIFT_NIFTY_ERROR_LOG_THROTTLE_SECONDS:
                print(f"[GIFT NIFTY] quote unavailable, entry continues without it: {exc}")
                _gift_nifty_cache["last_error_log_at"] = now_mono
            return empty

    spot_price = safe_float(spot_price, 0.0)
    return {
        "gift_nifty_ltp": round(float(snapshot["ltp"]), 2),
        "gift_nifty_change_pts": round(float(snapshot["change_pts"]), 2) if np.isfinite(snapshot["change_pts"]) else np.nan,
        "gift_nifty_change_pct": round(float(snapshot["change_pct"]), 3) if np.isfinite(snapshot["change_pct"]) else np.nan,
        "gift_nifty_premium": round(float(snapshot["ltp"]) - spot_price, 2) if spot_price > 0 else np.nan,
    }


def auto_select_ce_pe_symbols(kite: KiteConnect, spot_ltp: float, exchange: str = "NFO", selection_time=None) -> tuple[str, str, int, int]:
    global _LAST_AUTOSELECT_EXPIRY_LOG_KEY, _LAST_AUTOSELECT_SYMBOLS_LOG_KEY
    instruments = get_instruments_df(kite, exchange)
    opts = instruments[(instruments["name"] == UNDERLYING_NAME) & (instruments["segment"].str.contains("NFO-OPT", na=False))].copy()
    if opts.empty:
        raise ValueError(f"No option instruments found for {UNDERLYING_NAME}")

    opts = opts.dropna(subset=["expiry", "strike", "tradingsymbol", "instrument_token"]).copy()
    opts["strike"] = pd.to_numeric(opts["strike"], errors="coerce")
    opts = opts.dropna(subset=["strike"])

    # Use the same expiry policy in all modes. Historical runs use TRADE_DATE;
    # live modes use today's IST date unless an explicit --date was supplied.
    if RUN_MODE in {"PAPERTRADE_LIVE", "LIVE_ORDER"}:
        try:
            reference_date = pd.to_datetime(_cli_args.date).date() if _cli_args.date else datetime.now(IST).date()
        except Exception:
            reference_date = datetime.now(IST).date()
    else:
        try:
            reference_date = pd.to_datetime(TRADE_DATE).date()
        except Exception:
            reference_date = datetime.now(IST).date()
    expiry_cutoff = option_expiry_cutoff(reference_date)
    opts["expiry_date"] = opts["expiry"].dt.date
    opts = opts[opts["expiry_date"] > expiry_cutoff].copy()
    if opts.empty:
        raise ValueError(f"No option expiry available after {expiry_cutoff}")

    expiry_shifted_to_next = False
    if AUTO_SELECT_EXPIRY:
        requested_expiry = pd.to_datetime(AUTO_SELECT_EXPIRY).date()
        available = sorted(set(opts["expiry_date"].tolist()))
        if requested_expiry in available:
            selected_expiry = requested_expiry
        else:
            later = [d for d in available if d > requested_expiry]
            if not later:
                raise ValueError(f"No eligible option expiry at/after {requested_expiry}")
            selected_expiry = later[0]
            expiry_shifted_to_next = True
    else:
        selected_expiry = min(opts["expiry_date"].tolist())
    opts = opts[opts["expiry_date"] == selected_expiry].copy()

    requested_expiry = str(AUTO_SELECT_EXPIRY).strip() if str(AUTO_SELECT_EXPIRY).strip() else "nearest"
    expiry_log_key = (str(RUN_MODE), str(reference_date), str(requested_expiry), str(selected_expiry), bool(expiry_shifted_to_next))
    if expiry_log_key != _LAST_AUTOSELECT_EXPIRY_LOG_KEY:
        print(
            f"[AUTO-SELECT EXPIRY] mode={RUN_MODE} reference_date={reference_date} "
            f"requested={requested_expiry} selected={selected_expiry} shifted_to_next={expiry_shifted_to_next}"
        )
        _LAST_AUTOSELECT_EXPIRY_LOG_KEY = expiry_log_key

    # Choose from common CE/PE strikes so the selected pair can never diverge.
    ce_strikes = set(opts.loc[opts["instrument_type"] == "CE", "strike"].tolist())
    pe_strikes = set(opts.loc[opts["instrument_type"] == "PE", "strike"].tolist())
    common_strikes = ce_strikes.intersection(pe_strikes)
    if not common_strikes:
        raise ValueError(f"No common CE/PE strikes available for expiry {selected_expiry}")
    nearest_strike = min(common_strikes, key=lambda strike: (abs(float(strike) - float(spot_ltp)), float(strike)))
    strike_opts = opts[opts["strike"] == nearest_strike].copy()

    ce_row = strike_opts[strike_opts["instrument_type"] == "CE"].sort_values("tradingsymbol").head(1)
    pe_row = strike_opts[strike_opts["instrument_type"] == "PE"].sort_values("tradingsymbol").head(1)
    if ce_row.empty or pe_row.empty:
        raise ValueError(f"Could not resolve CE/PE for strike {nearest_strike}")

    ce_symbol = ce_row.iloc[0]["tradingsymbol"]
    pe_symbol = pe_row.iloc[0]["tradingsymbol"]
    ce_token = int(ce_row.iloc[0]["instrument_token"])
    pe_token = int(pe_row.iloc[0]["instrument_token"])
    ce_expiry = pd.to_datetime(ce_row.iloc[0]["expiry"]).date()
    pe_expiry = pd.to_datetime(pe_row.iloc[0]["expiry"]).date()
    if ce_expiry != pe_expiry or extract_option_strike(ce_symbol) != extract_option_strike(pe_symbol):
        raise ValueError(f"Selected CE/PE pair mismatch: CE={ce_symbol} ({ce_expiry}), PE={pe_symbol} ({pe_expiry})")
    symbols_log_key = (str(ce_symbol), int(ce_token), str(ce_expiry), str(pe_symbol), int(pe_token), str(pe_expiry))
    if symbols_log_key != _LAST_AUTOSELECT_SYMBOLS_LOG_KEY:
        _LAST_AUTOSELECT_SYMBOLS_LOG_KEY = symbols_log_key
        print(
            f"[STRIKE AUDIT] mode={RUN_MODE} source=KITE reference=COMPLETED_SPOT_CANDLE_CLOSE spot={float(spot_ltp):.2f} "
            f"selection_time={selection_time} expiry={ce_expiry} strike={extract_option_strike(ce_symbol)} "
            f"CE={ce_symbol} PE={pe_symbol}"
        )
    return ce_symbol, pe_symbol, ce_token, pe_token


def place_kite_order_with_market_protection(kite: KiteConnect, order_params: Dict) -> str:
    """Place an order with market protection when applicable.

    - Adds `market_protection` for MARKET and SL-M orders when enabled.
    - Works with newer Kite SDKs that accept the keyword directly.
    - Falls back to the raw HTTP API path for older SDKs where `place_order()`
      does not yet expose the keyword but the backend supports it.
    """
    params = dict(order_params)
    order_type = str(params.get("order_type") or "").upper()
    protection_value = safe_int(LIVE_ORDER_MARKET_PROTECTION, -1)
    protection_applicable = ENABLE_MARKET_PROTECTION and order_type in {"MARKET", "SL-M"}

    if protection_applicable:
        params["market_protection"] = protection_value

    try:
        return kite.place_order(**params)
    except TypeError as exc:
        needs_raw_fallback = protection_applicable and "market_protection" in str(exc)
        if not needs_raw_fallback:
            raise

        if not hasattr(kite, "_post"):
            raise RuntimeError(
                "Installed Kite SDK does not accept market_protection in place_order(), "
                "and raw HTTP fallback is unavailable. Update kiteconnect or switch to a version that supports market protection."
            ) from exc

        raw_params = {k: v for k, v in params.items() if v is not None}
        variety = str(raw_params.pop("variety", LIVE_ORDER_VARIETY) or LIVE_ORDER_VARIETY)

        try:
            return kite._post(f"order.place.{variety}", params=raw_params, is_json=False)
        except TypeError:
            return kite._post(f"order.place.{variety}", raw_params)


def place_live_entry_order(kite: KiteConnect, side: str, ce_symbol: str, pe_symbol: str, qty: int, entry_price_hint: float = 0.0, entry_number: int = 1) -> Dict:
    if not ENABLE_LIVE_ORDERS:
        raise RuntimeError("ENABLE_LIVE_ORDERS must be True for LIVE_ORDER mode")
    tradingsymbol = ce_symbol if side == "CE" else pe_symbol

    if ENABLE_COVER_ORDER_ENTRY:
        if str(LIVE_ORDER_PRODUCT).upper() != "MIS":
            raise RuntimeError("Cover Order entry requires LIVE_ORDER_PRODUCT = 'MIS'")
        ref_entry_price = safe_float(entry_price_hint, 0.0)
        if ref_entry_price <= 0:
            raise RuntimeError("Cover Order entry requires a positive entry_price_hint")

        sl_points = get_dynamic_emergency_sl_points(ref_entry_price, entry_number)
        trigger_price = round(max(0.05, ref_entry_price - sl_points), 1)
        order_id = place_kite_order_with_market_protection(kite, {
            "variety": "co",
            "exchange": OPTION_EXCHANGE,
            "tradingsymbol": tradingsymbol,
            "transaction_type": kite.TRANSACTION_TYPE_BUY,
            "quantity": int(qty),
            "product": "MIS",
            "order_type": "MARKET",
            "trigger_price": trigger_price,
            "validity": "DAY",
            "tag": LIVE_ORDER_TAG,
        })
        return {
            "entry_order_id": order_id,
            "entry_order_symbol": tradingsymbol,
            "entry_transaction_type": "BUY",
            "cover_order_used": True,
            "cover_order_trigger_price": trigger_price,
            "emergency_sl_pct_used": sl_points,
        }

    order_params = {
        "variety": LIVE_ORDER_VARIETY,
        "exchange": OPTION_EXCHANGE,
        "tradingsymbol": tradingsymbol,
        "transaction_type": kite.TRANSACTION_TYPE_BUY,
        "quantity": int(qty),
        "product": LIVE_ORDER_PRODUCT,
        "order_type": LIVE_ORDER_ORDER_TYPE,
        "validity": "DAY",
        "tag": LIVE_ORDER_TAG,
    }
    try:
        order_id = place_kite_order_with_market_protection(kite, order_params)
        order_type_used = str(order_params.get("order_type") or "")
        fallback_limit_price = np.nan
    except InputException as exc:
        msg = str(exc)
        is_market_protection_reject = "market protection" in msg.lower()
        market_order_requested = str(order_params.get("order_type") or "").upper() == "MARKET"
        if ENABLE_MARKET_TO_LIMIT_FALLBACK and market_order_requested and is_market_protection_reject:
            fallback_price = build_fallback_limit_price(kite, tradingsymbol, str(kite.TRANSACTION_TYPE_BUY))
            order_params["order_type"] = "LIMIT"
            order_params["price"] = fallback_price
            order_id = place_kite_order_with_market_protection(kite, order_params)
            order_type_used = "LIMIT_FALLBACK"
            fallback_limit_price = fallback_price
            print(
                f"[ORDER FALLBACK] BUY {tradingsymbol}: MARKET rejected by market-protection policy; "
                f"placed LIMIT @ {fallback_price:.2f}"
            )
        else:
            raise

    return {
        "entry_order_id": order_id,
        "entry_order_symbol": tradingsymbol,
        "entry_transaction_type": "BUY",
        "cover_order_used": False,
        "entry_order_type_used": order_type_used,
        "entry_limit_price_used": fallback_limit_price,
    }


def apply_paper_entry_slippage(signal_price: float, side: str = "BUY") -> float:
    px = safe_float(signal_price, 0.0)
    if px <= 0:
        return px
    mode = str(PAPER_ENTRY_SLIPPAGE_MODE).upper()
    if mode == "NONE":
        slipped = px
    elif mode == "PCT":
        slipped = px * (1.0 + safe_float(PAPER_ENTRY_SLIPPAGE_PCT, 0.0))
    else:
        slipped = px + safe_float(PAPER_ENTRY_SLIPPAGE_POINTS, 0.0)
    return round_to_tick(slipped, OPTION_TICK_SIZE)


def apply_paper_exit_slippage(signal_price: float, side: str = "SELL") -> float:
    px = safe_float(signal_price, 0.0)
    if px <= 0:
        return px
    mode = str(PAPER_EXIT_SLIPPAGE_MODE).upper()
    if mode == "NONE":
        slipped = px
    elif mode == "PCT":
        slipped = px * (1.0 - safe_float(PAPER_EXIT_SLIPPAGE_PCT, 0.0))
    else:
        slipped = px - safe_float(PAPER_EXIT_SLIPPAGE_POINTS, 0.0)
    return round_to_tick(max(OPTION_TICK_SIZE, slipped), OPTION_TICK_SIZE)


def is_signal_too_old(signal_ts, now_ts) -> bool:
    if signal_ts is None or now_ts is None:
        return False
    sig = pd.Timestamp(signal_ts)
    nowv = pd.Timestamp(now_ts)
    if hasattr(sig, "to_pydatetime"):
        sig = sig.to_pydatetime()
    if hasattr(nowv, "to_pydatetime"):
        nowv = nowv.to_pydatetime()
    if sig.tzinfo is None:
        sig = IST.localize(sig)
    if nowv.tzinfo is None:
        nowv = IST.localize(nowv)
    return (nowv - sig).total_seconds() > safe_int(MAX_ENTRY_SIGNAL_AGE_SECONDS, 20)


def should_skip_entry_for_slippage(signal_price: float, actual_price: float) -> bool:
    if not SKIP_ENTRY_IF_TOO_MUCH_SLIPPAGE:
        return False
    return (safe_float(actual_price) - safe_float(signal_price)) > safe_float(MAX_ENTRY_SLIPPAGE_POINTS_ALLOWED, 99999.0)


def register_symbol_exit_cooldown(live_state: Dict, option_symbol: str, exit_time, exit_price: float = None) -> None:
    symbol = str(option_symbol or "").strip().upper()
    if not symbol or exit_time is None:
        return
    ts = pd.Timestamp(exit_time)
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = IST.localize(ts)
    live_state.setdefault("symbol_last_exit_time", {})[symbol] = ts
    if exit_price is not None and safe_float(exit_price, 0.0) > 0:
        live_state.setdefault("symbol_last_exit_price", {})[symbol] = safe_float(exit_price, 0.0)


def is_same_symbol_reentry_blocked(live_state: Dict, option_symbol: str, now_ts) -> bool:
    cooldown_mins = safe_int(BLOCK_SAME_SYMBOL_REENTRY_WITHIN_MINUTES, 0)
    symbol = str(option_symbol or "").strip().upper()
    if cooldown_mins <= 0 or not symbol:
        return False
    last_exit = (live_state.get("symbol_last_exit_time") or {}).get(symbol)
    if last_exit is None:
        return False
    ts = pd.Timestamp(now_ts)
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = IST.localize(ts)
    return ts < (last_exit + timedelta(minutes=cooldown_mins))


def is_same_symbol_reentry_price_blocked(live_state: Dict, option_symbol: str, entry_price: float) -> bool:
    min_drop = safe_float(SAME_SYMBOL_MIN_REENTRY_DROP_POINTS, 0.0)
    symbol = str(option_symbol or "").strip().upper()
    previous_exit_price = safe_float((live_state.get("symbol_last_exit_price") or {}).get(symbol), 0.0)
    current_price = safe_float(entry_price, 0.0)
    if min_drop <= 0 or not symbol or previous_exit_price <= 0 or current_price <= 0:
        return False
    return current_price >= previous_exit_price - min_drop


def get_option_ltp_from_live_state(live_state: Dict, side: str, instrument_token: int = 0) -> float:
    try:
        side_up = str(side or "").upper()
        token = safe_int(instrument_token, 0)
        if token <= 0:
            token = safe_int(live_state.get("current_ce_token"), 0) if side_up == "CE" else safe_int(live_state.get("current_pe_token"), 0)
        tick = (live_state.get("current") or {}).get(token) or {}
        for key in ("last_price", "close"):
            if tick.get(key) not in (None, ""):
                return safe_float(tick.get(key), 0.0)
    except Exception:
        pass
    return 0.0


def get_spot_ltp_from_live_state(live_state: Dict) -> float:
    try:
        tick = (live_state.get("current") or {}).get(UNDERLYING_TOKEN) or {}
        for key in ("last_price", "close"):
            if tick.get(key) not in (None, ""):
                return safe_float(tick.get(key), 0.0)
    except Exception:
        pass
    return 0.0


def should_block_entry_reentry(live_state: Dict, pending_buy: Dict) -> tuple[bool, str, float]:
    """Apply only the configured same-symbol re-entry safeguards."""
    signal_price = safe_float(pending_buy.get("entry_price"), 0.0)
    option_symbol = str(pending_buy.get("option_symbol") or "")
    now_ts = datetime.now(IST)

    if is_same_symbol_reentry_blocked(live_state, option_symbol, now_ts):
        return True, f"same symbol cooldown active for {option_symbol}", signal_price
    if is_same_symbol_reentry_price_blocked(live_state, option_symbol, signal_price):
        previous_exit_price = safe_float((live_state.get("symbol_last_exit_price") or {}).get(option_symbol.upper()), 0.0)
        return True, f"same symbol price has not dropped {safe_float(SAME_SYMBOL_MIN_REENTRY_DROP_POINTS, 0.0):.2f} points below exit ({previous_exit_price:.2f})", signal_price

    return False, "", signal_price


def safe_place_live_exit_order(kite: KiteConnect, side: str, ce_symbol: str, pe_symbol: str, qty: int, broker_entry: Optional[Dict] = None) -> Dict:
    broker_entry = broker_entry or {}
    # (Previously contained an available-margin pre-check; removed per configuration)
    existing_exit_order_id = str(broker_entry.get("exit_order_id") or broker_entry.get("broker_exit_order_id") or "").strip()
    if existing_exit_order_id:
        # Replace a stale exit (e.g. an unfilled LIMIT fallback) instead of
        # leaving the position with no working exit order.
        cancel_order_and_wait(kite, existing_exit_order_id)
        snap = get_order_snapshot(kite, existing_exit_order_id)
        status = normalize_order_status(snap.get("status"))
        if status in {"OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED", "VALIDATION PENDING"}:
            broker_entry["exit_in_progress"] = True
            return {"skipped": True, "skip_reason": f"EXIT_ALREADY_PENDING:{status}", "exit_order_id": existing_exit_order_id}
        if status == "COMPLETE":
            cancel_order_and_wait(kite, broker_entry.get("emergency_sl_order_id"))
            broker_entry.update({
                "manual_or_broker_exit_detected": True,
                "broker_exit_order_id": existing_exit_order_id,
                "broker_exit_avg_price": extract_avg_price(snap),
                "broker_exit_time": extract_order_time(snap),
                "broker_exit_reason": str(broker_entry.get("broker_exit_reason") or "EXIT_ALREADY_COMPLETED"),
                "broker_open_qty": 0,
                "exit_in_progress": False,
            })
            return {"skipped": True, "skip_reason": "EXIT_ALREADY_COMPLETED", "exit_order_id": existing_exit_order_id}
    elif broker_entry.get("exit_in_progress"):
        return {"skipped": True, "skip_reason": "EXIT_ALREADY_IN_PROGRESS"}

    tradingsymbol = str(broker_entry.get("option_symbol") or (ce_symbol if side == "CE" else pe_symbol))
    # Cancel the broker-side SL only immediately before the exit, and read the
    # position afterwards so an SL that filled first is reflected in net_qty.
    try:
        cancel_order_and_wait(kite, broker_entry.get("emergency_sl_order_id"))
    except Exception:
        print(f"[EXIT PREP WARNING] Failed to cancel emergency SL for {tradingsymbol}")
    broker_entry["exit_in_progress"] = True
    try:
        net_qty = get_net_position_qty(kite, tradingsymbol)
        broker_entry["broker_open_qty"] = net_qty
        if net_qty <= 0:
            broker_entry.update({
                "manual_or_broker_exit_detected": True,
                "broker_exit_reason": str(broker_entry.get("broker_exit_reason") or "NO_POSITION_TO_EXIT"),
                "broker_open_qty": 0,
                "exit_in_progress": False,
            })
            return {"skipped": True, "skip_reason": "NO_POSITION_TO_EXIT", "exit_order_symbol": tradingsymbol}
        order_meta = place_live_exit_order(kite, side, ce_symbol, pe_symbol, min(int(qty), abs(int(net_qty))), broker_entry=broker_entry)
        broker_entry.update(order_meta)
        return order_meta
    except Exception:
        broker_entry["exit_in_progress"] = False
        restore_emergency_sl_order(kite, broker_entry, tradingsymbol)
        raise


def place_live_exit_order(kite: KiteConnect, side: str, ce_symbol: str, pe_symbol: str, qty: int, broker_entry: Optional[Dict] = None) -> Dict:
    if not ENABLE_LIVE_ORDERS:
        raise RuntimeError("ENABLE_LIVE_ORDERS must be True for LIVE_ORDER mode")
    broker_entry = broker_entry or {}
    tradingsymbol = str(broker_entry.get("option_symbol") or (ce_symbol if side == "CE" else pe_symbol))
    order_params = {
        "variety": LIVE_ORDER_VARIETY,
        "exchange": OPTION_EXCHANGE,
        "tradingsymbol": tradingsymbol,
        "transaction_type": kite.TRANSACTION_TYPE_SELL,
        "quantity": int(qty),
        "product": LIVE_ORDER_PRODUCT,
        "order_type": LIVE_ORDER_ORDER_TYPE,
        "validity": "DAY",
        "tag": LIVE_ORDER_TAG,
    }
    try:
        order_id = place_kite_order_with_market_protection(kite, order_params)
        order_type_used = str(order_params.get("order_type") or "")
        fallback_limit_price = np.nan
    except InputException as exc:
        msg = str(exc)
        is_market_protection_reject = "market protection" in msg.lower()
        market_order_requested = str(order_params.get("order_type") or "").upper() == "MARKET"
        if ENABLE_MARKET_TO_LIMIT_FALLBACK and market_order_requested and is_market_protection_reject:
            fallback_price = build_fallback_limit_price(kite, tradingsymbol, str(kite.TRANSACTION_TYPE_SELL))
            order_params["order_type"] = "LIMIT"
            order_params["price"] = fallback_price
            order_id = place_kite_order_with_market_protection(kite, order_params)
            order_type_used = "LIMIT_FALLBACK"
            fallback_limit_price = fallback_price
            print(
                f"[ORDER FALLBACK] SELL {tradingsymbol}: MARKET rejected by market-protection policy; "
                f"placed LIMIT @ {fallback_price:.2f}"
            )
        else:
            raise

    return {
        "exit_order_id": order_id,
        "exit_order_symbol": tradingsymbol,
        "exit_transaction_type": "SELL",
        "exit_order_type_used": order_type_used,
        "exit_limit_price_used": fallback_limit_price,
    }


def place_live_squareoff_order(kite: KiteConnect, tradingsymbol: str, net_qty: int) -> Dict:
    if not ENABLE_LIVE_ORDERS:
        raise RuntimeError("ENABLE_LIVE_ORDERS must be True for LIVE_ORDER mode")
    qty = abs(safe_int(net_qty, 0))
    if qty <= 0:
        raise ValueError(f"Cannot square off zero quantity for {tradingsymbol}")

    tx = kite.TRANSACTION_TYPE_SELL if safe_int(net_qty, 0) > 0 else kite.TRANSACTION_TYPE_BUY
    order_params = {
        "variety": LIVE_ORDER_VARIETY,
        "exchange": OPTION_EXCHANGE,
        "tradingsymbol": str(tradingsymbol),
        "transaction_type": tx,
        "quantity": int(qty),
        "product": LIVE_ORDER_PRODUCT,
        "order_type": LIVE_ORDER_ORDER_TYPE,
        "validity": "DAY",
        "tag": LIVE_ORDER_TAG,
    }
    try:
        order_id = place_kite_order_with_market_protection(kite, order_params)
        order_type_used = str(order_params.get("order_type") or "")
        fallback_limit_price = np.nan
    except InputException as exc:
        msg = str(exc)
        is_market_protection_reject = "market protection" in msg.lower()
        market_order_requested = str(order_params.get("order_type") or "").upper() == "MARKET"
        if ENABLE_MARKET_TO_LIMIT_FALLBACK and market_order_requested and is_market_protection_reject:
            fallback_price = build_fallback_limit_price(kite, str(tradingsymbol), str(tx))
            order_params["order_type"] = "LIMIT"
            order_params["price"] = fallback_price
            order_id = place_kite_order_with_market_protection(kite, order_params)
            order_type_used = "LIMIT_FALLBACK"
            fallback_limit_price = fallback_price
            print(
                f"[ORDER FALLBACK] {str(tx).upper()} {tradingsymbol}: MARKET rejected by market-protection policy; "
                f"placed LIMIT @ {fallback_price:.2f}"
            )
        else:
            raise

    return {
        "squareoff_order_id": order_id,
        "squareoff_symbol": str(tradingsymbol),
        "squareoff_transaction_type": str(tx).upper(),
        "squareoff_qty": int(qty),
        "squareoff_order_type_used": order_type_used,
        "squareoff_limit_price_used": fallback_limit_price,
    }


def is_market_hours(ts: datetime) -> bool:
    local_ts = ts.astimezone(IST) if ts.tzinfo else IST.localize(ts)
    if local_ts.weekday() >= 5:
        return False
    current_t = local_ts.time()
    return dtime(9, 15) <= current_t <= dtime(15, 30)


def wait_until_market_open(check_interval_seconds: int = 30) -> datetime:
    interval = max(1, int(check_interval_seconds))
    last_eta_min = None

    try:
        while True:
            now_ist = datetime.now(IST)
            market_open_today = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)

            if now_ist >= market_open_today:
                return now_ist

            remaining_seconds = max(1, int((market_open_today - now_ist).total_seconds()))
            eta_minutes = remaining_seconds // 60

            if last_eta_min is None or eta_minutes != last_eta_min:
                print(
                    f"Waiting for market open at 09:15 IST... "
                    f"current={now_ist.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"remaining~{eta_minutes}m"
                )
                last_eta_min = eta_minutes

            time.sleep(min(interval, remaining_seconds))
    except KeyboardInterrupt:
        print("\nInterrupted waiting for market open. Exiting.")
        sys.exit(0)


def floor_to_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def is_live_reentry_cooldown_active(last_exit_time, now_ts: datetime) -> bool:
    cooldown_minutes = max(0, safe_int(REENTRY_COOLDOWN_MINUTES, 0))
    if cooldown_minutes <= 0 or last_exit_time is None:
        return False
    exit_ts = pd.Timestamp(last_exit_time)
    if hasattr(exit_ts, "to_pydatetime"):
        exit_ts = exit_ts.to_pydatetime()
    if exit_ts.tzinfo is None:
        exit_ts = IST.localize(exit_ts)
    now_local = now_ts.astimezone(IST) if now_ts.tzinfo else IST.localize(now_ts)
    cooldown_until = exit_ts + timedelta(minutes=cooldown_minutes)
    return now_local < cooldown_until


def ensure_parent_dir(file_path: str) -> None:
    parent = os.path.dirname(file_path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def sanitize_filename_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value))


def strip_option_side_suffix(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if s.endswith("CE") or s.endswith("PE"):
        return s[:-2]
    return s


def get_timestamp_str() -> str:
    return datetime.now(IST).strftime("%d%m%Y_%H%M%S")


def build_backtest_excel_file_path(trade_date: str, ce_symbol: str, pe_symbol: str) -> str:
    try:
        date_part = datetime.strptime(trade_date, "%Y-%m-%d").strftime("%d%m%Y") if trade_date else get_timestamp_str()
    except Exception:
        date_part = sanitize_filename_part(trade_date) if trade_date else get_timestamp_str()
    version_suffix = f"_{sanitize_filename_part(LOG_IDENTIFIER)}" if LOG_IDENTIFIER else ""
    config_suffix = (
        f"_mtf{sanitize_filename_part(MTF_INTERVAL)}"
        f"{'_mfe' if ENABLE_MFE_EXIT else ''}"
        f"_max{sanitize_filename_part(MAX_TRADES)}"
        f"_sl{sanitize_filename_part(FIXED_SL_POINTS)}"
    )
    data_source_suffix = "_kite" if BACKTEST_OPTION_DATA_SOURCE == "KITE" else "_csv"
    file_name = f"backtest_{date_part}{config_suffix}{version_suffix}{data_source_suffix}.xlsx"
    return os.path.join(STRATEGY_LOG_DIR, file_name)


def build_papertrade_excel_file_path(trade_date: str, ce_symbol: str, pe_symbol: str) -> str:
    timestamp = get_timestamp_str()
    version_suffix = f"_{sanitize_filename_part(LOG_IDENTIFIER)}" if LOG_IDENTIFIER else ""
    config_suffix = (
        f"_mtf{sanitize_filename_part(MTF_INTERVAL)}"
        f"{'_mfe' if ENABLE_MFE_EXIT else ''}"
        f"_max{sanitize_filename_part(MAX_TRADES)}"
        f"_sl{sanitize_filename_part(FIXED_SL_POINTS)}"
    )
    file_name = f"papertrade_{timestamp}{config_suffix}{version_suffix}.xlsx"
    return os.path.join(STRATEGY_LOG_DIR, file_name)


def build_livetrade_excel_file_path(trade_date: str, ce_symbol: str, pe_symbol: str) -> str:
    timestamp = get_timestamp_str()
    version_suffix = f"_{sanitize_filename_part(LOG_IDENTIFIER)}" if LOG_IDENTIFIER else ""
    config_suffix = (
        f"_mtf{sanitize_filename_part(MTF_INTERVAL)}"
        f"{'_mfe' if ENABLE_MFE_EXIT else ''}"
        f"_max{sanitize_filename_part(MAX_TRADES)}"
        f"_sl{sanitize_filename_part(FIXED_SL_POINTS)}"
    )
    file_name = f"livetrade_{timestamp}{config_suffix}{version_suffix}.xlsx"
    return os.path.join(STRATEGY_LOG_DIR, file_name)


def to_excel_safe_df(rows: List[Dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for col in df.columns:
        # Try to convert object columns containing datetime-like values
        if df[col].dtype == object:
            try:
                converted = pd.to_datetime(df[col], errors="coerce")
                if converted.notna().any():
                    # If timestamps carry timezone info, convert to IST and
                    # then drop tz info so Excel shows local IST times.
                    try:
                        if getattr(converted.dt, "tz", None) is not None:
                            converted = converted.dt.tz_convert(IST).dt.tz_localize(None)
                    except Exception:
                        pass
                    df[col] = converted
            except Exception:
                pass
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            try:
                if getattr(df[col].dt, "tz", None) is not None:
                    df[col] = df[col].dt.tz_convert(IST).dt.tz_localize(None)
            except Exception:
                try:
                    df[col] = df[col].dt.tz_localize(None)
                except Exception:
                    pass
    return df


def build_papertrade_summary_df(
    live_trade_events: List[Dict],
    guard_stop_reason: str = "",
    run_mode: str = "PAPERTRADE_LIVE",
    market_context: Optional[Dict] = None,
) -> pd.DataFrame:
    trades_df = to_excel_safe_df(live_trade_events)

    total_trades = 0
    winning_trades = 0
    losing_trades = 0
    total_points = 0.0
    total_rs = 0.0
    notes = ""

    if trades_df.empty:
        notes = "No trades found for the day with current filters."
    else:
        total_trades = int(len(trades_df))
        if "pnl_rs" in trades_df.columns:
            winning_trades = int((trades_df["pnl_rs"] > 0).sum())
            losing_trades = int((trades_df["pnl_rs"] <= 0).sum())
            total_rs = float(trades_df["pnl_rs"].sum())
        if "pnl_points" in trades_df.columns:
            total_points = float(trades_df["pnl_points"].sum())

    latest_context = {}
    if live_trade_events:
        latest_context = dict(live_trade_events[-1])

    fallback_context = market_context or {}

    def context_value(key: str):
        val = latest_context.get(key, "")
        try:
            if val == "" or pd.isna(val):
                return fallback_context.get(key, "")
        except Exception:
            if val == "":
                return fallback_context.get(key, "")
        return val

    summary_rows = [
        {"metric": "run_mode", "value": str(run_mode)},
        {"metric": "script_name", "value": get_running_script_name()},
        {"metric": "gap_type", "value": context_value("gap_type")},
        {"metric": "gap_points", "value": context_value("gap_points")},
        {"metric": "gap_pct", "value": context_value("gap_pct")},
        {"metric": "cpr_pivot", "value": context_value("cpr_pivot")},
        {"metric": "cpr_bc", "value": context_value("cpr_bc")},
        {"metric": "cpr_tc", "value": context_value("cpr_tc")},
        {"metric": "cpr_width", "value": context_value("cpr_width")},
        {"metric": "cpr_width_pct", "value": context_value("cpr_width_pct")},
        {"metric": "cpr_type", "value": context_value("cpr_type")},
        {"metric": "papertrade_fresh_count_on_restart", "value": str(PAPERTRADE_FRESH_COUNT_ON_RESTART)},
        {"metric": "mtm_guard_enabled", "value": str(ENABLE_MTM_GUARD)},
        {"metric": "trade_stop_basis", "value": "MTM_ONLY" if ENABLE_MTM_GUARD else "MTM_AND_MAX_TRADES"},
        {"metric": "tracking_start", "value": TRACKING_START},
        {"metric": "no_new_entry_after", "value": NO_NEW_ENTRY_AFTER},
        {"metric": "trailing_profit_stage1", "value": str(TRAILING_PROFIT_STAGE1)},
        {"metric": "trailing_profit_stage2", "value": str(TRAILING_PROFIT_STAGE2)},
        {"metric": "trailing_profit_stage2_time", "value": TRAILING_PROFIT_STAGE2_TIME},
        {"metric": "breakeven_trigger", "value": float(BREAKEVEN_TRIGGER)},
        {"metric": "trail_trigger", "value": float(TRAIL_TRIGGER)},
        {"metric": "trail_pct", "value": float(TRAIL_PCT)},
        {"metric": "daily_profit_target_rs", "value": float(DAILY_PROFIT_TARGET_RS)},
        {"metric": "daily_loss_limit_rs", "value": float(DAILY_LOSS_LIMIT_RS)},
        {"metric": "max_trades", "value": int(MAX_TRADES) if not ENABLE_MTM_GUARD and MAX_TRADES is not None else ""},
        {"metric": "max_concurrent_trades", "value": int(MAX_CONCURRENT_TRADES)},
        {"metric": "max_concurrent_trades_ce", "value": int(MAX_CONCURRENT_TRADES_CE)},
        {"metric": "max_concurrent_trades_pe", "value": int(MAX_CONCURRENT_TRADES_PE)},
        {"metric": "lots", "value": int(LOTS)},
        {"metric": "lot_size", "value": int(LOT_SIZE)},
        {"metric": "qty", "value": int(QTY)},
        {"metric": "total_trades", "value": total_trades},
        {"metric": "winning_trades", "value": winning_trades},
        {"metric": "losing_or_flat_trades", "value": losing_trades},
        {"metric": "total_pnl_points", "value": round(total_points, 2)},
        {"metric": "total_pnl_rs", "value": round(total_rs, 2)},
        {"metric": "guard_stop_reason", "value": guard_stop_reason or ""},
        {"metric": "auto_select_spot_ref", "value": str(AUTO_SELECT_SPOT_REF)},
        {"metric": "notes", "value": notes},
    ]
    return pd.DataFrame(summary_rows)


def build_backtest_summary_df(trades: pd.DataFrame, run_mode: str, day: pd.DataFrame = None) -> pd.DataFrame:
    total_trades = 0
    winning_trades = 0
    losing_trades = 0
    total_points = 0.0
    total_rs = 0.0
    notes = ""
    guard_stop_reason = trades.attrs.get("guard_stop_reason") if hasattr(trades, "attrs") else ""

    if trades is None or trades.empty:
        notes = "No trades found for the day with current filters."
    else:
        total_trades = int(len(trades))
        if "pnl_rs" in trades.columns:
            winning_trades = int((trades["pnl_rs"] > 0).sum())
            losing_trades = int((trades["pnl_rs"] <= 0).sum())
            total_rs = float(trades["pnl_rs"].sum())
        if "pnl_points" in trades.columns:
            total_points = float(trades["pnl_points"].sum())

    market_ctx = trades.attrs.get("market_context", {}) if hasattr(trades, "attrs") else {}
    if not market_ctx:
        market_ctx = build_market_context(
            day,
            prev_high=safe_float((day.attrs.get("nifty_prev_high", 0.0) if day is not None and hasattr(day, "attrs") else 0.0), 0.0),
            prev_low=safe_float((day.attrs.get("nifty_prev_low", 0.0) if day is not None and hasattr(day, "attrs") else 0.0), 0.0),
            prev_close=safe_float((day.attrs.get("nifty_prev_close", 0.0) if day is not None and hasattr(day, "attrs") else 0.0), 0.0),
        )

    summary_rows = [
        {"metric": "run_mode", "value": str(run_mode)},
        {"metric": "script_name", "value": get_running_script_name()},
        {"metric": "trade_date", "value": str(TRADE_DATE)},
        {"metric": "nifty_prev_close", "value": market_ctx.get("nifty_prev_close", "")},
        {"metric": "nifty_open", "value": market_ctx.get("nifty_open", "")},
        {"metric": "nifty_high", "value": market_ctx.get("nifty_high", "")},
        {"metric": "nifty_low", "value": market_ctx.get("nifty_low", "")},
        {"metric": "nifty_close", "value": market_ctx.get("nifty_close", "")},
        {"metric": "nifty_change", "value": market_ctx.get("nifty_change", "")},
        {"metric": "gap_type", "value": market_ctx.get("gap_type", "")},
        {"metric": "gap_points", "value": market_ctx.get("gap_points", "")},
        {"metric": "gap_pct", "value": market_ctx.get("gap_pct", "")},
        {"metric": "cpr_pivot", "value": market_ctx.get("cpr_pivot", "")},
        {"metric": "cpr_bc", "value": market_ctx.get("cpr_bc", "")},
        {"metric": "cpr_tc", "value": market_ctx.get("cpr_tc", "")},
        {"metric": "cpr_width", "value": market_ctx.get("cpr_width", "")},
        {"metric": "cpr_width_pct", "value": market_ctx.get("cpr_width_pct", "")},
        {"metric": "cpr_type", "value": market_ctx.get("cpr_type", "")},
        {"metric": "mtm_guard_enabled", "value": str(ENABLE_MTM_GUARD)},
        {"metric": "trade_stop_basis", "value": "MTM_ONLY" if ENABLE_MTM_GUARD else "MTM_AND_MAX_TRADES"},
        {"metric": "tracking_start", "value": TRACKING_START},
        {"metric": "no_new_entry_after", "value": NO_NEW_ENTRY_AFTER},
        {"metric": "trailing_profit_stage1", "value": str(TRAILING_PROFIT_STAGE1)},
        {"metric": "trailing_profit_stage2", "value": str(TRAILING_PROFIT_STAGE2)},
        {"metric": "trailing_profit_stage2_time", "value": TRAILING_PROFIT_STAGE2_TIME},
        {"metric": "breakeven_trigger", "value": float(BREAKEVEN_TRIGGER)},
        {"metric": "trail_trigger", "value": float(TRAIL_TRIGGER)},
        {"metric": "trail_pct", "value": float(TRAIL_PCT)},
        {"metric": "daily_profit_target_rs", "value": float(DAILY_PROFIT_TARGET_RS)},
        {"metric": "daily_loss_limit_rs", "value": float(DAILY_LOSS_LIMIT_RS)},
        {"metric": "max_trades", "value": int(MAX_TRADES) if not ENABLE_MTM_GUARD and MAX_TRADES is not None else ""},
        {"metric": "max_concurrent_trades", "value": int(MAX_CONCURRENT_TRADES)},
        {"metric": "max_concurrent_trades_ce", "value": int(MAX_CONCURRENT_TRADES_CE)},
        {"metric": "max_concurrent_trades_pe", "value": int(MAX_CONCURRENT_TRADES_PE)},
        {"metric": "lots", "value": int(LOTS)},
        {"metric": "lot_size", "value": int(LOT_SIZE)},
        {"metric": "qty", "value": int(QTY)},
        {"metric": "total_trades", "value": total_trades},
        {"metric": "winning_trades", "value": winning_trades},
        {"metric": "losing_or_flat_trades", "value": losing_trades},
        {"metric": "total_pnl_points", "value": round(total_points, 2)},
        {"metric": "total_pnl_rs", "value": round(total_rs, 2)},
        {"metric": "guard_stop_reason", "value": guard_stop_reason or ""},
        {"metric": "auto_select_spot_ref", "value": str(AUTO_SELECT_SPOT_REF)},
        {"metric": "notes", "value": notes},
    ]
    return pd.DataFrame(summary_rows)


def export_papertrade_events_to_excel(trade_date: str, ce_symbol: str, pe_symbol: str, live_events: List[Dict], live_trade_events: List[Dict], guard_stop_reason: str = "", execution_mode: str = "PAPERTRADE_LIVE", reuse_path: str = None, debug_day: Optional[pd.DataFrame] = None) -> str:
    def normalize_export_timestamp(value):
        try:
            timestamp = pd.Timestamp(value)
            if timestamp.tzinfo is not None:
                timestamp = timestamp.tz_convert(IST).tz_localize(None)
            return timestamp.to_pydatetime().replace(microsecond=0)
        except Exception:
            return value

    if reuse_path:
        excel_file = reuse_path
    else:
        excel_file = build_livetrade_excel_file_path(trade_date, ce_symbol, pe_symbol) if str(execution_mode).upper() == "LIVE_ORDER" else build_papertrade_excel_file_path(trade_date, ce_symbol, pe_symbol)
    ensure_parent_dir(excel_file)

    events_df = to_excel_safe_df(live_events)
    if not events_df.empty and "event_time" in events_df.columns:
        events_df = events_df.sort_values("event_time").reset_index(drop=True)

    trades_df = to_excel_safe_df(live_trade_events)
    if trades_df.empty:
        trades_df = pd.DataFrame(
            columns=[
                "entry_time",
                "exit_time",
                "side",
                "option_symbol",
                "entry_price",
                "exit_price",
                "pnl_points",
                "pnl_rs",
            ]
        )
    if not trades_df.empty and "entry_time" in trades_df.columns:
        trades_df = trades_df.sort_values("entry_time").reset_index(drop=True)

    for frame in (events_df, trades_df):
        for timestamp_column in ("event_time", "entry_time", "exit_time", "signal_time"):
            if timestamp_column in frame.columns:
                frame[timestamp_column] = frame[timestamp_column].map(normalize_export_timestamp)

    # Align trade `entry_time`/`signal_time` with actual BUY event times when available.
    try:
        if not events_df.empty and not trades_df.empty and "option_symbol" in events_df.columns and "option_symbol" in trades_df.columns:
            buy_events = events_df[events_df.get("event_type", "").astype(str).str.upper() == "BUY"][["option_symbol", "event_time"]].copy()
            # Build a queue of buy times per option symbol (preserve chronological order)
            buy_queues = {}
            for _, r in buy_events.iterrows():
                sym = str(r.get("option_symbol") or "")
                buy_queues.setdefault(sym, []).append(r.get("event_time"))

            # Replace trades_df.entry_time with the first unused buy event time for the same symbol.
            # Keep signal_time in sync so exported rows do not show stale pre-fill timestamps.
            for idx, row in trades_df.iterrows():
                sym = str(row.get("option_symbol") or "")
                if sym in buy_queues and buy_queues[sym]:
                    candidate = buy_queues[sym].pop(0)
                    trades_df.at[idx, "entry_time"] = candidate
                    if "signal_time" in trades_df.columns:
                        trades_df.at[idx, "signal_time"] = candidate
    except Exception:
        pass

    debug_df = pd.DataFrame() if debug_day is None else debug_day.copy()
    if not debug_df.empty:
        for col in debug_df.columns:
            if pd.api.types.is_datetime64_any_dtype(debug_df[col]):
                try:
                    if getattr(debug_df[col].dt, "tz", None) is not None:
                        debug_df[col] = debug_df[col].dt.tz_convert(IST).dt.tz_localize(None)
                except Exception:
                    try:
                        debug_df[col] = debug_df[col].dt.tz_localize(None)
                    except Exception:
                        pass

    summary_market_context: Dict[str, object] = {}
    try:
        if debug_day is not None and hasattr(debug_day, "attrs"):
            for key in ["gap_type", "gap_points", "gap_pct", "cpr_pivot", "cpr_bc", "cpr_tc", "cpr_width", "cpr_width_pct", "cpr_type"]:
                if key in debug_day.attrs:
                    summary_market_context[key] = debug_day.attrs.get(key, "")
    except Exception:
        summary_market_context = {}

    summary_df = build_papertrade_summary_df(
        live_trade_events,
        guard_stop_reason,
        execution_mode,
        market_context=summary_market_context,
    )

    with pd.ExcelWriter(excel_file, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="summary", index=False)
        events_df.to_excel(writer, sheet_name="live_events", index=False)
        trades_df.to_excel(writer, sheet_name="live_trade_events", index=False)
        debug_df.to_excel(writer, sheet_name="debug_merged", index=False)

    try:
        from openpyxl import load_workbook
        from openpyxl.chart import BarChart, Reference, LineChart
        wb = load_workbook(excel_file)


        wb.save(excel_file)
    except Exception as chart_exc:
        print(f"[Excel chart warning] {chart_exc}")

    return excel_file

def format_live_event_row(
    event_type: str,
    run_mode: str,
    trade_date: str,
    underlying: str,
    side: str,
    option_symbol: str,
    event_time,
    event_reason: str,
    spot_price: float,
    option_price: float,
    qty: int,
    realized_mtm: float,
    mtf_label: str,
    gap_type: str = "",
    gap_points: float = np.nan,
    gap_pct: float = np.nan,
    cpr_type: str = "",
    cpr_width_pct: float = np.nan,
    emergency_sl_pct_used: float = np.nan,
    emergency_sl_price: float = np.nan,
    emergency_sl_trigger_price: float = np.nan,
    target_profit_points: float = np.nan,
    trailing_status: str = "",
    gift_nifty: Optional[Dict] = None,
) -> Dict:
    gift_nifty = gift_nifty or {}
    return {
        "event_type": event_type,
        "run_mode": run_mode,
        "trade_date": trade_date,
        "underlying": underlying,
        "side": side,
        "option_symbol": option_symbol,
        "event_time": event_time,
        "event_reason": event_reason,
        "spot_price": spot_price,
        "option_price": option_price,
        "qty": qty,
        "realized_mtm": realized_mtm,
        "mtf_label": mtf_label,
        "gap_type": gap_type,
        "gap_points": gap_points,
        "gap_pct": gap_pct,
        "cpr_type": cpr_type,
        "cpr_width_pct": cpr_width_pct,
        "emergency_sl_pct_used": emergency_sl_pct_used,
        "emergency_sl_price": emergency_sl_price,
        "emergency_sl_trigger_price": emergency_sl_trigger_price,
        "target_profit_points": target_profit_points,
        "trailing_status": trailing_status,
        "gift_nifty_ltp": safe_float(gift_nifty.get("gift_nifty_ltp"), np.nan),
        "gift_nifty_change_pts": safe_float(gift_nifty.get("gift_nifty_change_pts"), np.nan),
        "gift_nifty_change_pct": safe_float(gift_nifty.get("gift_nifty_change_pct"), np.nan),
        "gift_nifty_premium": safe_float(gift_nifty.get("gift_nifty_premium"), np.nan),
    }


def candle_rows_from_history(df: pd.DataFrame) -> List[Dict]:
    rows = []
    if df is None or df.empty:
        return rows
    w = df.copy()
    w["ts"] = pd.to_datetime(w["ts"])
    for _, r in w.sort_values("ts").iterrows():
        rows.append({
            "ts": r["ts"],
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r.get("volume", 0) or 0),
        })
    return rows


def rows_to_df(rows: List[Dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    out = pd.DataFrame(rows).sort_values("ts").drop_duplicates(subset=["ts"], keep="last").reset_index(drop=True)
    out["ts"] = pd.to_datetime(out["ts"])
    return out


def run_after_hours_paper_replay(kite: KiteConnect, ce_symbol: str, pe_symbol: str, ce_token: int, pe_token: int, trade_date: str, execution_mode: str = "PAPERTRADE_LIVE") -> pd.DataFrame:
    print(
        f"[AFTER-HOURS REPLAY] Reconstructing {execution_mode} session for {trade_date} "
        f"from minute history (no live websocket)."
    )

    def fetch_history_safe(token: int, label: str) -> pd.DataFrame:
        try:
            return fetch_history(kite, token, trade_date, BASE_INTERVAL)
        except ValueError as exc:
            if "No history returned for token" in str(exc):
                print(f"[AFTER-HOURS REPLAY] No history for {label} token={token}")
                return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
            raise

    spot_hist = fetch_history_safe(UNDERLYING_TOKEN, UNDERLYING_NAME)

    replay_ce_symbol = ce_symbol
    replay_pe_symbol = pe_symbol
    replay_ce_token = ce_token
    replay_pe_token = pe_token

    if AUTO_SELECT_OPTION_SYMBOLS and spot_hist is not None and not spot_hist.empty:
        try:
            ref = str(AUTO_SELECT_SPOT_REF or "open").strip().lower()
            hist_spot_ts = None
            if ref in {"close", "signal_close"}:
                valid_spot = spot_hist.dropna(subset=["close"]).sort_values("ts")
                if valid_spot.empty:
                    raise ValueError(f"No valid spot close available for reference {AUTO_SELECT_SPOT_REF}")
                latest_spot = valid_spot.iloc[-1]
                hist_spot_ltp = safe_float(latest_spot.get("close"), 0.0)
                hist_spot_ts = latest_spot.get("ts")
            elif ref == "last":
                hist_spot_ltp = safe_float(spot_hist.iloc[-1].get("close"), 0.0)
                hist_spot_ts = spot_hist.iloc[-1].get("ts")
            else:
                hist_spot_ltp = safe_float(spot_hist.iloc[0].get("open"), 0.0)
                hist_spot_ts = spot_hist.iloc[0].get("ts")

            if hist_spot_ltp > 0:
                replay_ce_symbol, replay_pe_symbol, replay_ce_token, replay_pe_token = auto_select_ce_pe_symbols(
                    kite,
                    hist_spot_ltp,
                    OPTION_EXCHANGE,
                    selection_time=hist_spot_ts,
                )
                print(
                    f"[AFTER-HOURS REPLAY] Historical spot ref ({AUTO_SELECT_SPOT_REF})={hist_spot_ltp:.2f} -> "
                    f"CE={replay_ce_symbol} ({replay_ce_token}) PE={replay_pe_symbol} ({replay_pe_token})"
                )
        except Exception as exc:
            print(f"[AFTER-HOURS REPLAY] Historical symbol auto-select failed; using incoming symbols. reason={exc}")

    ce_hist = fetch_history_safe(replay_ce_token, replay_ce_symbol)
    pe_hist = fetch_history_safe(replay_pe_token, replay_pe_symbol)

    if spot_hist.empty or ce_hist.empty or pe_hist.empty:
        print("[AFTER-HOURS REPLAY] Incomplete history; replay aborted.")
        final_trades = pd.DataFrame()
        excel_file = export_papertrade_events_to_excel(
            trade_date=trade_date,
            ce_symbol=replay_ce_symbol,
            pe_symbol=replay_pe_symbol,
            live_events=[],
            live_trade_events=[],
            guard_stop_reason="",
            execution_mode=execution_mode,
            reuse_path=None,
        )
        final_trades.attrs["papertrade_excel_file"] = excel_file
        final_trades.attrs["live_excel_file"] = excel_file
        final_trades.attrs["guard_stop_reason"] = ""
        final_trades.attrs["papertrade_fresh_count_on_restart"] = PAPERTRADE_FRESH_COUNT_ON_RESTART
        final_trades.attrs["papertrade_restart_tracking_from"] = None
        final_trades.attrs["realized_mtm_rs"] = 0.0
        return final_trades

    day = prepare_merged_day(spot_hist, ce_hist, pe_hist)

    try:
        prev_ohlc = fetch_prev_trading_day_ohlc(kite, UNDERLYING_TOKEN, trade_date)
    except Exception:
        prev_ohlc = {}
    day.attrs.update(
        build_market_context(
            day,
            prev_high=safe_float(prev_ohlc.get("high"), 0.0),
            prev_low=safe_float(prev_ohlc.get("low"), 0.0),
            prev_close=safe_float(prev_ohlc.get("close"), 0.0),
        )
    )

    # Use BACKTEST mode logic for replay so historical symbol refresh and
    # signal construction match backtest behavior.
    replay_mode = "BACKTEST"
    if AUTO_SELECT_OPTION_SYMBOLS:
        trades = run_single_day_test(day, replay_mode, tracking_start_override=None, ce_symbol_override=None, pe_symbol_override=None)
    else:
        trades = run_single_day_test(day, replay_mode, tracking_start_override=None, ce_symbol_override=replay_ce_symbol, pe_symbol_override=replay_pe_symbol)

    live_events: List[Dict] = []
    live_trade_events: List[Dict] = []
    realized_mtm = 0.0
    if trades is not None and not trades.empty:
        for _, trade in trades.iterrows():
            trade_dict = trade.to_dict()
            trade_dict["run_mode"] = execution_mode
            buy_event_row = format_live_event_row(
                event_type="BUY",
                run_mode=str(trade_dict.get("run_mode", execution_mode)),
                trade_date=str(trade_dict.get("trade_date", trade_date)),
                underlying=str(trade_dict.get("underlying", UNDERLYING_NAME)),
                side=str(trade_dict.get("side", "")),
                option_symbol=str(trade_dict.get("option_symbol", "")),
                event_time=trade_dict.get("entry_time"),
                event_reason=str(trade_dict.get("entry_reason", "")),
                spot_price=safe_float(trade_dict.get("entry_spot"), 0.0),
                option_price=safe_float(trade_dict.get("entry_price"), 0.0),
                qty=safe_int(trade_dict.get("qty"), QTY),
                realized_mtm=realized_mtm,
                mtf_label=str(trade_dict.get("mtf_entry_label", "")),
                emergency_sl_pct_used=safe_float(trade_dict.get("emergency_sl_pct_used"), 0.0),
                emergency_sl_price=safe_float(trade_dict.get("emergency_sl_price"), np.nan),
            )
            live_events.append(buy_event_row)

            realized_mtm += safe_float(trade_dict.get("pnl_rs"), 0.0)
            sell_event_row = format_live_event_row(
                event_type="SELL",
                run_mode=str(trade_dict.get("run_mode", execution_mode)),
                trade_date=str(trade_dict.get("trade_date", trade_date)),
                underlying=str(trade_dict.get("underlying", UNDERLYING_NAME)),
                side=str(trade_dict.get("side", "")),
                option_symbol=str(trade_dict.get("option_symbol", "")),
                event_time=trade_dict.get("exit_time"),
                event_reason=str(trade_dict.get("exit_reason", "")),
                spot_price=safe_float(trade_dict.get("exit_spot"), 0.0),
                option_price=safe_float(trade_dict.get("exit_price"), 0.0),
                qty=safe_int(trade_dict.get("qty"), QTY),
                realized_mtm=realized_mtm,
                mtf_label=str(trade_dict.get("mtf_exit_label", "")),
                emergency_sl_pct_used=safe_float(trade_dict.get("emergency_sl_pct_used"), 0.0),
                emergency_sl_price=safe_float(trade_dict.get("emergency_sl_price"), np.nan),
            )
            live_events.append(sell_event_row)
            live_trade_events.append(trade_dict)

    guard_stop_reason = str(trades.attrs.get("guard_stop_reason") or "") if hasattr(trades, "attrs") else ""
    excel_file = export_papertrade_events_to_excel(
        trade_date=trade_date,
        ce_symbol=replay_ce_symbol,
        pe_symbol=replay_pe_symbol,
        live_events=live_events,
        live_trade_events=live_trade_events,
        guard_stop_reason=guard_stop_reason,
        execution_mode=execution_mode,
        reuse_path=None,
            debug_day=day,
    )

    if live_trade_events:
        final_trades = pd.DataFrame(live_trade_events).sort_values("entry_time").reset_index(drop=True)
    else:
        final_trades = trades.copy()

    final_trades.attrs["papertrade_excel_file"] = excel_file
    final_trades.attrs["live_excel_file"] = excel_file
    final_trades.attrs["guard_stop_reason"] = guard_stop_reason
    final_trades.attrs["papertrade_fresh_count_on_restart"] = PAPERTRADE_FRESH_COUNT_ON_RESTART
    final_trades.attrs["papertrade_restart_tracking_from"] = None
    if not final_trades.empty and "pnl_rs" in final_trades.columns:
        final_trades.attrs["realized_mtm_rs"] = float(final_trades["pnl_rs"].sum())
    else:
        final_trades.attrs["realized_mtm_rs"] = 0.0
    return final_trades


def run_live_session(kite: KiteConnect, ce_symbol: str, pe_symbol: str, ce_token: int, pe_token: int, execution_mode: str = "PAPERTRADE_LIVE") -> pd.DataFrame:
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:
        print(f"{execution_mode} runs only on weekdays. Current IST time: {now_ist.strftime('%Y-%m-%d %H:%M:%S')}")
        return pd.DataFrame()

    market_open_today = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close_today = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)

    if now_ist < market_open_today:
        print(f"{execution_mode} started before market open. Keeping app alive until 09:15 IST...")
        now_ist = wait_until_market_open(check_interval_seconds=30)
        print(f"Market is open. Starting live processing at {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST")
    elif now_ist > market_close_today:
        if execution_mode == "PAPERTRADE_LIVE" and AFTER_HOURS_PAPER_REPLAY:
            print(
                f"{execution_mode} after market close at {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST. "
                "Running after-hours replay from day history because --after-hours-replay is enabled."
            )
            return run_after_hours_paper_replay(
                kite,
                ce_symbol,
                pe_symbol,
                ce_token,
                pe_token,
                now_ist.strftime("%Y-%m-%d"),
                execution_mode=execution_mode,
            )
        print(f"{execution_mode} runs only during market hours. Current IST time: {now_ist.strftime('%Y-%m-%d %H:%M:%S')}")
        return pd.DataFrame()

    trade_date = now_ist.strftime("%Y-%m-%d")
    no_new_entry_after_live_ts = pd.Timestamp(f"{trade_date} {NO_NEW_ENTRY_AFTER}", tz=IST)
    force_exit_ts = pd.Timestamp(f"{trade_date} {FORCE_EXIT}", tz=IST)
    session_start_minute = floor_to_minute(now_ist)
    live_tracking_start = session_start_minute if (PAPERTRADE_FRESH_COUNT_ON_RESTART and ALLOW_ENTRY_ONLY_AFTER_APP_START) else None

    preload_day_history = READ_HISTORY_FROM_DAY_START or (not PAPERTRADE_FRESH_COUNT_ON_RESTART)

    if preload_day_history:
        print(f"Bootstrapping live session from historical minute candles for {trade_date}...")
        if PAPERTRADE_FRESH_COUNT_ON_RESTART and ALLOW_ENTRY_ONLY_AFTER_APP_START:
            print(
                f"{execution_mode} mid-session restart: using full-day history for indicators, "
                f"but allowing fresh entries only from {session_start_minute.strftime('%H:%M')} IST"
            )
        else:
            print(f"{execution_mode} restart mode: resuming count from market-open reconstructed history")

        def fetch_history_safe(token: int, label: str) -> pd.DataFrame:
            try:
                return fetch_history(kite, token, trade_date, BASE_INTERVAL)
            except ValueError as exc:
                if "No history returned for token" in str(exc):
                    print(
                        f"[Bootstrap pending] No historical candles yet for {label} token={token}. "
                        "Continuing with websocket-only warmup."
                    )
                    return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
                raise

        spot_hist = fetch_history_safe(UNDERLYING_TOKEN, UNDERLYING_NAME)
        ce_hist = fetch_history_safe(ce_token, ce_symbol)
        pe_hist = fetch_history_safe(pe_token, pe_symbol)

        current_minute = session_start_minute
        spot_rows = candle_rows_from_history(spot_hist[spot_hist["ts"] < current_minute])
        ce_rows = candle_rows_from_history(ce_hist[ce_hist["ts"] < current_minute])
        pe_rows = candle_rows_from_history(pe_hist[pe_hist["ts"] < current_minute])
    else:
        print(f"Starting fresh {execution_mode} session from {session_start_minute.strftime('%Y-%m-%d %H:%M:%S %Z')} with no preloaded history...")
        spot_rows = []
        ce_rows = []
        pe_rows = []

    live_state = {
        "spot_rows": spot_rows,
        "ce_rows": ce_rows,
        "pe_rows": pe_rows,
        "current": {},
        "stop": False,
        "final_trades": pd.DataFrame(),
        "paper_open_position": None,
        "paper_exit_pending": None,
        "last_exception": None,
        "active_order_event_id": None,
        "live_entry_in_progress_event_id": None,
        # Persistent session-level count of successfully emitted BUY entries.
        # MAX_TRADES limits total entries and survives incremental rebuilds.
        "execution_entry_count": 0,
        "printed_buy_event_ids": set(),
        "insufficient_funds_event_ids": set(),
        "stale_live_signal_event_ids": set(),
        "printed_sell_event_ids": set(),
        "printed_trade_ids": set(),
        "paper_entry_prices": {},
        "live_events": [],
        "live_trade_events": [],
        "broker_positions": {},
        "realized_live_pnl_rs": 0.0,
        "last_broker_sync_ts": 0.0,
        "excel_file_path": None,
        "max_trades_block_logged": False,
        "shutdown_cleanup_done": False,
        "shutdown_summary_printed": False,
        "last_live_exit_time": None,
        "consecutive_losses":0,
        "cooldown_block_logged_for": None,
        "symbol_last_exit_time": {},
        "symbol_last_exit_price": {},
        "force_exit_triggered": False,
        "current_ce_symbol": ce_symbol,
        "current_pe_symbol": pe_symbol,
        "current_ce_token": ce_token,
        "current_pe_token": pe_token,
        "ticker_ws": None,
        "ws_connected": False,
        "last_tick_ts": None,
        "last_tick_exchange_ts": None,
        "last_idle_status_ts": None,
        "last_idle_status_reason": None,
        "last_tick_stall_logged_ts": None,
        "last_tick_resubscribe_attempt_ts": None,
        "last_tick_reconnect_attempt_ts": None,
        "ws_reconnect_requested": False,
        "last_token_tick_ts": {},
        "last_open_position_ltp_refresh_ts": None,
        "last_symbol_refresh_ts": None,
        "last_history_backfill_attempt_ts": None,
        "history_backfill_received": set(),
        "history_backfill_complete": not preload_day_history,
        "last_symbol_switch_ts": None,
        "last_entry_block_reason": "",
        "locked_trade_strike_active": False,
        "locked_ce_symbol": "",
        "locked_pe_symbol": "",
        "locked_ce_token": 0,
        "locked_pe_token": 0,
        "last_symbol_lock_skip_reason": "",
        "last_open_pnl_log_ts": None,
        "last_open_pnl_event_id": None,
        "open_pnl_log_lock": threading.Lock(),
        "market_context": {},
        "last_day_debug_df": pd.DataFrame(),
        "last_entry_diag_key": None,
        "entry_diag_file_path": os.path.join(
            STRATEGY_LOG_DIR,
            f"entry_diag_{trade_date.replace('-', '')}_{sanitize_filename_part(LOG_IDENTIFIER or 'default')}.txt",
        ),
        "watchdog_stop_event": threading.Event(),
        "watchdog_thread": None,
    }

    if ENTRY_DIAGNOSTICS:
        try:
            ensure_parent_dir(live_state["entry_diag_file_path"])
            with open(live_state["entry_diag_file_path"], "a", encoding="utf-8") as _diag_fp:
                _diag_fp.write(
                    f"\n=== ENTRY DIAG SESSION START {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S %Z')} mode={execution_mode} ===\n"
                )
            print(f"[ENTRY DIAG FILE] {live_state['entry_diag_file_path']}")
        except Exception as _diag_file_exc:
            print(f"[ENTRY DIAG FILE WARNING] {_diag_file_exc}")

    try:
        prev_ohlc = fetch_prev_trading_day_ohlc(kite, UNDERLYING_TOKEN, trade_date)
    except Exception:
        prev_ohlc = {}
    live_ctx_day = None
    try:
        today_ist = datetime.now(IST).date()
        if pd.to_datetime(trade_date).date() == today_ist:
            today_open = get_spot_open_for_today(kite, UNDERLYING_NAME)
            if today_open > 0:
                live_ctx_day = pd.DataFrame({"spot_open": [today_open]})
    except Exception:
        live_ctx_day = None

    live_state["market_context"] = build_market_context(
        live_ctx_day,
        prev_high=safe_float(prev_ohlc.get("high"), 0.0),
        prev_low=safe_float(prev_ohlc.get("low"), 0.0),
        prev_close=safe_float(prev_ohlc.get("close"), 0.0),
    )
    set_mtf_interval_from_gap(
        (live_state.get("market_context") or {}).get("gap_pct"),
        source="live-startup",
    )

    token_to_key = {UNDERLYING_TOKEN: "spot", ce_token: "ce", pe_token: "pe"}

    def adopt_open_live_positions_from_broker() -> None:
        if execution_mode != "LIVE_ORDER":
            return
        try:
            positions_payload = kite.positions() or {}
        except Exception as exc:
            print(f"[LIVE ADOPT WARNING] Unable to fetch broker positions on startup: {exc}")
            return

        net_rows = positions_payload.get("net") or []
        if not isinstance(net_rows, list):
            net_rows = []

        symbol_rows: Dict[str, Dict] = {}
        for row in net_rows:
            try:
                qty = safe_int(row.get("quantity") or row.get("net_quantity") or 0, 0)
                symbol = str(row.get("tradingsymbol") or "").strip().upper()
                exchange = str(row.get("exchange") or "").strip().upper()
                product = str(row.get("product") or "").strip().upper()
                if qty <= 0:
                    continue
                if exchange != str(OPTION_EXCHANGE).upper() or product != str(LIVE_ORDER_PRODUCT).upper():
                    continue
                if not (symbol.endswith("CE") or symbol.endswith("PE")):
                    continue
                symbol_rows[symbol] = row
            except Exception:
                continue

        if not symbol_rows:
            return

        try:
            orders_cache = kite.orders() or []
        except Exception:
            orders_cache = []

        adopted_count = 0
        for symbol, row in symbol_rows.items():
            side = "CE" if symbol.endswith("CE") else "PE"
            qty = safe_int(row.get("quantity") or row.get("net_quantity") or 0, 0)
            entry_price = safe_float(
                row.get("average_price") or row.get("buy_price") or row.get("last_price") or 0.0,
                0.0,
            )

            entry_order_id = ""
            entry_time = None
            for order in reversed(orders_cache):
                if str(order.get("tradingsymbol") or "").strip().upper() != symbol:
                    continue
                if str(order.get("transaction_type") or "").strip().upper() != "BUY":
                    continue
                if normalize_order_status(order.get("status")) != "COMPLETE":
                    continue
                filled_qty = safe_int(order.get("filled_quantity") or order.get("quantity") or 0, 0)
                if filled_qty <= 0:
                    continue
                entry_order_id = str(order.get("order_id") or "")
                entry_time = extract_order_time(order)
                avg_from_order = extract_avg_price(order)
                if avg_from_order > 0:
                    entry_price = avg_from_order
                break

            if entry_time is None:
                entry_time = datetime.now(IST)

            entry_reason = "ADOPTED_RESTART_OPEN_POSITION"
            event_id = make_entry_event_id(side, entry_time, symbol)
            if event_id in live_state["broker_positions"]:
                continue

            spot_now = get_spot_ltp_from_live_state(live_state)
            if spot_now <= 0:
                try:
                    spot_now = get_spot_ltp(kite, UNDERLYING_NAME)
                except Exception:
                    spot_now = 0.0

            adopted_entry = {
                "side": side,
                "option_symbol": symbol,
                "qty": qty,
                "entry_reason": entry_reason,
                "entry_time": entry_time,
                "entry_spot": spot_now,
                "signal_time": entry_time,
                "signal_spot_price": spot_now,
                "signal_option_price": entry_price,
                "entry_price_theoretical": entry_price,
                "entry_price_actual": entry_price,
                "mtf_entry_label": "ADOPTED",
                "emergency_sl_pct_used": safe_float(FIXED_SL_POINTS, 50.0),
                "emergency_sl_price": np.nan,
                "emergency_sl_trigger_price": np.nan,
                "emergency_sl_order_id": "",
                "entry_order_id": entry_order_id,
                "entry_order_status": "COMPLETE" if entry_order_id else "ADOPTED",
                "entry_broker_time": entry_time,
                "entry_order_message": "adopted on restart",
                "exit_in_progress": False,
                "best_price": entry_price,
                "trailing_stop_price": 0.0,
                "target_profit_points": float(TRAILING_PROFIT_STAGE1[0][0]) if TRAILING_PROFIT_STAGE1 else 50.0,
                "broker_open_qty": qty,
                "manual_or_broker_exit_detected": False,
            }
            live_state["broker_positions"][event_id] = adopted_entry
            adopted_count += 1

            token_value = safe_int(row.get("instrument_token") or row.get("instrumentToken") or 0, 0)
            if side == "CE":
                old_ce_token = safe_int(live_state.get("current_ce_token"), 0)
                live_state["current_ce_symbol"] = symbol
                if token_value > 0:
                    live_state["current_ce_token"] = token_value
                    if old_ce_token > 0:
                        token_to_key.pop(old_ce_token, None)
                    token_to_key[token_value] = "ce"
            else:
                old_pe_token = safe_int(live_state.get("current_pe_token"), 0)
                live_state["current_pe_symbol"] = symbol
                if token_value > 0:
                    live_state["current_pe_token"] = token_value
                    if old_pe_token > 0:
                        token_to_key.pop(old_pe_token, None)
                    token_to_key[token_value] = "pe"

            print(
                f"[LIVE ADOPT] Adopted open broker position: side={side} symbol={symbol} qty={qty} "
                f"entry={entry_price:.2f} entry_time={entry_time}"
            )

        if adopted_count > 0:
            live_state["max_trades_block_logged"] = False
            print(f"[LIVE ADOPT] Total adopted open positions: {adopted_count}")

    def normalize_exit_reason(reason: str, default: str = "STRATEGY_EXIT") -> str:
        text = str(reason or "").strip()
        if text.startswith("#sym:"):
            text = text[5:]
        return text or default

    def get_new_entry_block_reason() -> str:
        now_local = datetime.now(IST)
        if live_state.get("force_exit_triggered") or now_local >= force_exit_ts:
            return "FORCE_EXIT_WINDOW"
        if now_local >= no_new_entry_after_live_ts:
            return "NO_NEW_ENTRY_AFTER"
        if live_state.get("stop"):
            return "SESSION_STOP"
        return ""

    def extract_option_strike(symbol: str) -> int:
        # Typical symbol: NIFTY2680424050CE -> numeric part includes expiry+strike.
        # Mod 100000 yields the strike for current index option formats.
        s = str(symbol or "").upper()
        if not (s.endswith("CE") or s.endswith("PE")):
            return 0
        num = ""
        for ch in reversed(s[:-2]):
            if ch.isdigit():
                num = ch + num
            elif num:
                break
        if not num:
            return 0
        try:
            return int(num) % 100000
        except Exception:
            return 0

    def refresh_entry_symbols(reason: str = "POST_EXIT", forced_pair: Optional[tuple] = None) -> None:
        if not AUTO_SELECT_OPTION_SYMBOLS:
            return

        def _symbol_refresh_log_context() -> str:
            ts = live_state.get("last_tick_exchange_ts") or live_state.get("last_tick_ts") or datetime.now(IST)
            ts_obj = pd.Timestamp(ts)
            if hasattr(ts_obj, "to_pydatetime"):
                ts_obj = ts_obj.to_pydatetime()
            if ts_obj.tzinfo is None:
                ts_obj = IST.localize(ts_obj)
            ts_obj = ts_obj.astimezone(IST)
            return f"tf={BASE_INTERVAL} at={ts_obj.strftime('%H:%M:%S')}"

        if live_state.get("locked_trade_strike_active") and forced_pair is None:
            skip_key = f"LOCKED:{reason}"
            if live_state.get("last_symbol_lock_skip_reason") != skip_key:
                print(
                    f"[SYMBOL REFRESH SKIPPED] reason={reason} {_symbol_refresh_log_context()} "
                    f"locked_strike CE={live_state.get('locked_ce_symbol')} "
                    f"PE={live_state.get('locked_pe_symbol')}"
                )
                live_state["last_symbol_lock_skip_reason"] = skip_key
            return

        spot_ltp_now = 0.0
        try:
            if forced_pair is not None:
                new_ce_symbol, new_pe_symbol, new_ce_token, new_pe_token = forced_pair
                new_ce_symbol = str(new_ce_symbol)
                new_pe_symbol = str(new_pe_symbol)
                new_ce_token = int(new_ce_token)
                new_pe_token = int(new_pe_token)
            else:
                # Select from the latest completed spot candle shared with the signal engine.
                _spot_completed = rows_to_df(live_state.get("spot_rows") or [])
                if not _spot_completed.empty and "close" in _spot_completed.columns:
                    _spot_completed = _spot_completed.dropna(subset=["close"]).sort_values("ts")
                if not _spot_completed.empty:
                    _last_spot_candle = _spot_completed.iloc[-1]
                    spot_ltp_now = safe_float(_last_spot_candle.get("close"), 0.0)
                    selection_ts = _last_spot_candle.get("ts")
                if spot_ltp_now <= 0:
                    # Startup fallback only if no completed candle is available yet.
                    spot_ltp_now = get_spot_ltp(kite, UNDERLYING_NAME)
                    selection_ts = live_state.get("last_tick_exchange_ts") or live_state.get("last_tick_ts")
                new_ce_symbol, new_pe_symbol, new_ce_token, new_pe_token = auto_select_ce_pe_symbols(
                    kite, spot_ltp_now, OPTION_EXCHANGE, selection_time=selection_ts
                )
        except Exception as exc:
            print(f"[SYMBOL REFRESH FAILED] reason={reason} {_symbol_refresh_log_context()} error={exc}")
            return

        old_ce_symbol = str(live_state.get("current_ce_symbol") or "")
        old_pe_symbol = str(live_state.get("current_pe_symbol") or "")
        old_ce_token = safe_int(live_state.get("current_ce_token"), 0)
        old_pe_token = safe_int(live_state.get("current_pe_token"), 0)

        if forced_pair is None and old_ce_token > 0 and old_pe_token > 0:
            changed = (old_ce_token != new_ce_token) or (old_pe_token != new_pe_token)
            # POST_EXIT uses the same hysteresis as the backtest, which keeps the
            # current strike after an exit unless spot is past the midpoint buffer.
            if changed and reason in {"PRE_ENTRY", "PERIODIC_NO_TRADE", "POST_EXIT"}:
                now_for_switch = datetime.now(IST)
                min_switch_seconds = max(0, safe_int(AUTO_SELECT_MIN_SWITCH_INTERVAL_SECONDS, 45))
                last_switch_ts = live_state.get("last_symbol_switch_ts")
                if reason != "POST_EXIT" and last_switch_ts is not None and min_switch_seconds > 0:
                    elapsed = (now_for_switch - pd.Timestamp(last_switch_ts)).total_seconds()
                    if elapsed < min_switch_seconds:
                        print(
                            f"[SYMBOL REFRESH SKIPPED] reason={reason} {_symbol_refresh_log_context()} debounce_active elapsed={elapsed:.1f}s "
                            f"min_required={min_switch_seconds}s keeping CE={old_ce_symbol} PE={old_pe_symbol}"
                        )
                        return

                old_strike = extract_option_strike(old_ce_symbol or old_pe_symbol)
                new_strike = extract_option_strike(new_ce_symbol or new_pe_symbol)
                step = max(1, safe_int(AUTO_SELECT_STRIKE_STEP, 50))
                buffer_pts = max(1.0, safe_float(AUTO_SELECT_STRIKE_SWITCH_BUFFER_POINTS, 12.5))
                if old_strike > 0 and new_strike > 0 and abs(new_strike - old_strike) == step and spot_ltp_now > 0:
                    midpoint = (old_strike + new_strike) / 2.0
                    direction = 1.0 if new_strike > old_strike else -1.0
                    crossed = ((spot_ltp_now - midpoint) * direction) >= buffer_pts
                    if not crossed:
                        print(
                            f"[SYMBOL REFRESH SKIPPED] reason={reason} {_symbol_refresh_log_context()} hysteresis spot={spot_ltp_now:.2f} "
                            f"mid={midpoint:.2f} buffer={buffer_pts:.2f} keeping CE={old_ce_symbol} PE={old_pe_symbol}"
                        )
                        return

        # Commit new symbols immediately so subsequent rebuilds use them.
        live_state["current_ce_symbol"] = new_ce_symbol
        live_state["current_pe_symbol"] = new_pe_symbol
        live_state["current_ce_token"] = new_ce_token
        live_state["current_pe_token"] = new_pe_token
        live_state["last_symbol_switch_ts"] = datetime.now(IST)

        if old_ce_token == new_ce_token and old_pe_token == new_pe_token:
            unchanged_key = f"{reason}:{new_ce_symbol}:{new_pe_symbol}"
            if live_state.get("last_symbol_refresh_unchanged_key") != unchanged_key:
                print(
                    f"[SYMBOL REFRESH] reason={reason} {_symbol_refresh_log_context()} spot={spot_ltp_now:.2f} -> "
                    f"CE={new_ce_symbol} ({new_ce_token}) PE={new_pe_symbol} ({new_pe_token}) [UNCHANGED]"
                )
                live_state["last_symbol_refresh_unchanged_key"] = unchanged_key
            return

        for token in [old_ce_token, old_pe_token]:
            if token > 0:
                token_to_key.pop(token, None)
                live_state["current"].pop(token, None)
        token_to_key[new_ce_token] = "ce"
        token_to_key[new_pe_token] = "pe"

        # Backfill historical candle data for the new symbols so indicator warmup
        # remains intact and new entry signals can be detected immediately after
        # a mid-session symbol change (avoids "Waiting for candles" stall).
        try:
            _new_ce_hist = fetch_history(kite, new_ce_token, trade_date, BASE_INTERVAL)
            live_state["ce_rows"] = candle_rows_from_history(_new_ce_hist) if not _new_ce_hist.empty else []
        except Exception as _ce_hist_exc:
            print(f"[SYMBOL REFRESH HIST CE] {_ce_hist_exc}")
            live_state["ce_rows"] = []
        try:
            _new_pe_hist = fetch_history(kite, new_pe_token, trade_date, BASE_INTERVAL)
            live_state["pe_rows"] = candle_rows_from_history(_new_pe_hist) if not _new_pe_hist.empty else []
        except Exception as _pe_hist_exc:
            print(f"[SYMBOL REFRESH HIST PE] {_pe_hist_exc}")
            live_state["pe_rows"] = []

        ws = live_state.get("ticker_ws")
        if ws is not None:
            old_tokens = [tok for tok in [old_ce_token, old_pe_token] if tok > 0]
            if old_tokens:
                try:
                    ws.unsubscribe(old_tokens)
                except Exception:
                    pass
            try:
                ws.subscribe([new_ce_token, new_pe_token])
                ws.set_mode(ws.MODE_FULL, [new_ce_token, new_pe_token])
            except Exception as exc:
                print(f"[SYMBOL REFRESH WS WARNING] {exc}")

        print(
            f"[SYMBOL REFRESH] reason={reason} {_symbol_refresh_log_context()} spot={spot_ltp_now:.2f} "
            f"{old_ce_symbol}/{old_pe_symbol} -> {new_ce_symbol}/{new_pe_symbol}"
        )

    def retry_live_history_backfill() -> None:
        """Retry unavailable startup history before evaluating websocket-only signals."""
        if not preload_day_history or live_state.get("history_backfill_complete"):
            return

        now_local = datetime.now(IST)
        last_attempt = live_state.get("last_history_backfill_attempt_ts")
        if last_attempt is not None and (now_local - last_attempt).total_seconds() < 30:
            return
        live_state["last_history_backfill_attempt_ts"] = now_local

        histories = (
            ("spot", UNDERLYING_TOKEN, UNDERLYING_NAME),
            ("ce", safe_int(live_state.get("current_ce_token"), 0), live_state.get("current_ce_symbol")),
            ("pe", safe_int(live_state.get("current_pe_token"), 0), live_state.get("current_pe_symbol")),
        )
        received = 0
        for key, token, label in histories:
            if token <= 0:
                continue
            try:
                history = fetch_history(kite, token, trade_date, BASE_INTERVAL)
                if history.empty:
                    continue
                history_rows = candle_rows_from_history(history)
                existing_rows = live_state.get(f"{key}_rows") or []
                merged = {row["ts"]: row for row in existing_rows}
                merged.update({row["ts"]: row for row in history_rows})
                live_state[f"{key}_rows"] = sorted(merged.values(), key=lambda row: row["ts"])
                live_state["history_backfill_received"].add(key)
                received += 1
            except Exception as exc:
                print(f"[LIVE HISTORY BACKFILL] {label} unavailable: {exc}")

        if received:
            print(
                f"[LIVE HISTORY BACKFILL] received={received}/3 for "
                f"{now_local.strftime('%H:%M:%S %Z')}"
            )
        if live_state["history_backfill_received"] == {"spot", "ce", "pe"}:
            live_state["history_backfill_complete"] = True
            print("[LIVE HISTORY BACKFILL] complete; evaluating with historical warmup")

    def lock_entry_symbols_to_first_trade_strike(option_symbol: str, reason: str = "FIRST_COMPLETED_TRADE") -> None:
        if not AUTO_SELECT_OPTION_SYMBOLS:
            return
        if live_state.get("locked_trade_strike_active"):
            return

        trade_symbol = str(option_symbol or "").strip().upper()
        if not trade_symbol:
            return

        base_symbol = strip_option_side_suffix(trade_symbol)
        if not base_symbol:
            return

        locked_ce_symbol = f"{base_symbol}CE"
        locked_pe_symbol = f"{base_symbol}PE"

        try:
            instruments = get_instruments_df(kite, OPTION_EXCHANGE)
            pair_df = instruments[instruments["tradingsymbol"].isin([locked_ce_symbol, locked_pe_symbol])].copy()
            ce_row = pair_df[pair_df["tradingsymbol"] == locked_ce_symbol].head(1)
            pe_row = pair_df[pair_df["tradingsymbol"] == locked_pe_symbol].head(1)
            if ce_row.empty or pe_row.empty:
                raise ValueError(f"Could not resolve CE/PE pair for locked strike base {base_symbol}")

            locked_ce_token = int(ce_row.iloc[0]["instrument_token"])
            locked_pe_token = int(pe_row.iloc[0]["instrument_token"])
        except Exception as exc:
            print(f"[SYMBOL LOCK FAILED] reason={reason} option_symbol={trade_symbol} error={exc}")
            return

        refresh_entry_symbols(
            reason=f"{reason}_COMMIT",
            forced_pair=(locked_ce_symbol, locked_pe_symbol, locked_ce_token, locked_pe_token),
        )
        live_state["locked_trade_strike_active"] = True
        live_state["locked_ce_symbol"] = locked_ce_symbol
        live_state["locked_pe_symbol"] = locked_pe_symbol
        live_state["locked_ce_token"] = locked_ce_token
        live_state["locked_pe_token"] = locked_pe_token
        live_state["last_symbol_lock_skip_reason"] = ""
        print(
            f"[SYMBOL LOCKED] reason={reason} first_trade_symbol={trade_symbol} "
            f"locked_ce={locked_ce_symbol} ({locked_ce_token}) "
            f"locked_pe={locked_pe_symbol} ({locked_pe_token})"
        )

    def log_entry_diag(reason: str, pending_buy: Optional[Dict] = None, extra: str = "") -> None:
        if not ENTRY_DIAGNOSTICS:
            return
        ts_now = datetime.now(IST).strftime("%H:%M:%S")
        event_id = ""
        side = ""
        symbol = ""
        signal_ts = ""
        if pending_buy:
            side = str(pending_buy.get("side") or "")
            symbol = str(pending_buy.get("option_symbol") or "")
            signal_ts = str(pending_buy.get("entry_time") or "")
            event_id = make_entry_event_id(side, signal_ts, symbol)
        diag_key = f"{reason}|{event_id}|{extra}"
        if live_state.get("last_entry_diag_key") == diag_key:
            return
        live_state["last_entry_diag_key"] = diag_key
        line = (
            f"[ENTRY DIAG] t={ts_now} mode={execution_mode} reason={reason} "
            f"side={side} symbol={symbol} signal_ts={signal_ts} extra={extra}"
        )
        print(line)
        try:
            diag_path = str(live_state.get("entry_diag_file_path") or "")
            if diag_path:
                ensure_parent_dir(diag_path)
                with open(diag_path, "a", encoding="utf-8") as diag_fp:
                    diag_fp.write(line + "\n")
        except Exception as diag_write_exc:
            print(f"[ENTRY DIAG FILE WARNING] {diag_write_exc}")

    def append_completed_row(key: str, minute_ts: datetime, candle: Dict):
        row = {
            "ts": minute_ts,
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume", 0.0) or 0.0),
        }
        live_state[f"{key}_rows"] = [r for r in live_state[f"{key}_rows"] if r["ts"] != minute_ts]
        live_state[f"{key}_rows"].append(row)
        live_state[f"{key}_rows"].sort(key=lambda x: x["ts"])

    def get_execution_entry_count() -> int:
        """Return persistent session-level number of successfully emitted BUY entries."""
        return max(safe_int(live_state.get("execution_entry_count"), 0), 0)

    def get_completed_execution_trade_count() -> int:
        """Backward-compatible helper for reporting completed trades."""
        return len(live_state.get("live_trade_events") or [])

    def max_trades_reached_in_execution() -> bool:
        """MAX_TRADES limits total BUY entries for the session."""
        max_trades_cfg = safe_int(MAX_TRADES, 0)
        if max_trades_cfg <= 0:
            return False
        return get_execution_entry_count() >= max_trades_cfg

    def get_execution_realized_pnl() -> float:
        """Realized P&L of the session's completed execution trades."""
        if execution_mode == "LIVE_ORDER":
            return safe_float(live_state.get("realized_live_pnl_rs"), 0.0)
        return sum(safe_float(t.get("pnl_rs"), 0.0) for t in (live_state.get("live_trade_events") or []))

    def get_execution_stop_reason(trades: pd.DataFrame = None) -> str:
        """Use real execution state for LIVE_ORDER and PAPERTRADE_LIVE stops, not
        rebuilt strategy attrs: the rebuild only covers trades since the last exit,
        so its daily guards would not see the session's earlier trades."""
        now_local = datetime.now(IST)
        if execution_mode == "LIVE_ORDER":
            has_open_trade = get_open_local_position_count(live_state) > 0 or (
                live_state.get("active_order_event_id") is not None
                and live_state.get("active_order_event_id") in (live_state.get("broker_positions") or {})
                and not (live_state.get("broker_positions") or {}).get(
                    live_state.get("active_order_event_id"), {}
                ).get("manual_or_broker_exit_detected")
            )
        else:
            open_pos = trades.attrs.get("open_position") if (trades is not None and hasattr(trades, "attrs")) else None
            has_open_trade = bool(open_pos) or (
                execution_mode == "PAPERTRADE_LIVE"
                and live_state.get("active_order_event_id") is not None
            )

        if now_local >= no_new_entry_after_live_ts:
            if not has_open_trade:
                return "NO_NEW_ENTRY_AFTER_NO_OPEN_TRADE"

        if execution_mode in {"LIVE_ORDER", "PAPERTRADE_LIVE"}:
            live_realized_pnl = get_execution_realized_pnl()
            if ENABLE_PROFIT_LOCK:
                global profit_lock_active, profit_lock_level
                completed = len(live_state.get("live_trade_events") or [])
                if (not profit_lock_active) and completed >= PROFIT_LOCK_AFTER_TRADE_NO and live_realized_pnl >= PROFIT_LOCK_TRIGGER_RS:
                    profit_lock_active = True
                    profit_lock_level = live_realized_pnl * PROFIT_LOCK_PERCENT
                    print(f"[PROFIT LOCK ARMED] MTM={live_realized_pnl:.2f} Lock Level={profit_lock_level:.2f}")
                if profit_lock_active and live_realized_pnl <= profit_lock_level:
                    return "PROFIT_LOCK_HIT"
            if live_realized_pnl >= safe_float(DAILY_PROFIT_TARGET_RS, 0.0):
                return "DAILY_PROFIT_TARGET"
            if live_realized_pnl <= safe_float(DAILY_LOSS_LIMIT_RS, 0.0):
                return "DAILY_LOSS_LIMIT"
            events=live_state.get("live_trade_events") or []
            losses=0
            for t in reversed(events):
                if safe_float(t.get("pnl_rs"),0)<0:
                    losses+=1
                else:
                    break
            if ENABLE_CONSECUTIVE_LOSS_GUARD and losses>=MAX_CONSECUTIVE_LOSSES:
                return f"CONSECUTIVE_LOSSES_{MAX_CONSECUTIVE_LOSSES}"
            # MAX_TRADES limits fresh BUY entries. It must not terminate the
            # session while the last permitted position is still being managed.
            if max_trades_reached_in_execution() and not has_open_trade:
                return "MAX_TRADES_REACHED"
            return ""
        if trades is not None and hasattr(trades, "attrs"):
            reason = str(trades.attrs.get("guard_stop_reason") or "")
            # Only treat as a session-ending stop for explicit session-level reasons.
            # Per-trade exit reasons (TRAILING_STOP_HIT, SL_HIT, TARGET_HIT, FORCE_EXIT, etc.)
            # are stored as a reporting fallback and must NOT stop the live session.
            _SESSION_STOP = {"DAILY_PROFIT_TARGET", "DAILY_LOSS_LIMIT", "MAX_TRADES_REACHED", "PROFIT_LOCK_HIT"}
            if reason in _SESSION_STOP or "CONSECUTIVE_LOSSES" in reason:
                if has_open_trade and reason == "MAX_TRADES_REACHED":
                    return ""
                return reason
            return ""
        return ""

    def normalize_tick_ts_to_ist(raw_ts):
        ts = pd.Timestamp(raw_ts)
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        if ts.tzinfo is not None:
            return ts.astimezone(IST)

        # Some environments deliver naive IST, some deliver naive UTC.
        # Pick whichever interpretation is closest to current IST time.
        now_ist = datetime.now(IST)
        cand_ist = IST.localize(ts)
        cand_utc = UTC.localize(ts).astimezone(IST)
        if abs((now_ist - cand_utc).total_seconds()) < abs((now_ist - cand_ist).total_seconds()):
            return cand_utc
        return cand_ist

    def update_last_tick_ts(tick_ts=None):
        # Use packet-arrival time for stall detection; exchange timestamp can lag.
        live_state["last_tick_ts"] = datetime.now(IST)
        if tick_ts is not None:
            live_state["last_tick_exchange_ts"] = normalize_tick_ts_to_ist(tick_ts)

    def emit_idle_status(reason: str, throttle_seconds: int = IDLE_STATUS_INTERVAL_SECONDS):
        if reason == "Engine running. Waiting for next valid signal / cooldown expiry":
            return
        now_local = datetime.now(IST)
        last_reason = live_state.get("last_idle_status_reason")
        last_ts = live_state.get("last_idle_status_ts")
        should_print = (
            last_reason != reason
            or last_ts is None
            or (now_local - last_ts).total_seconds() >= max(1, safe_int(throttle_seconds, IDLE_STATUS_INTERVAL_SECONDS))
        )
        if should_print:
            print(f"[LIVE STATUS] {reason} | now={now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            live_state["last_idle_status_reason"] = reason
            live_state["last_idle_status_ts"] = now_local

    def emit_open_position_pnl_update(trades: pd.DataFrame) -> None:
        with live_state["open_pnl_log_lock"]:
            now_local = datetime.now(IST)
            last_logged_ts = live_state.get("last_open_pnl_log_ts")
            if (
                last_logged_ts is not None
                and (now_local - last_logged_ts).total_seconds()
                < safe_int(OPEN_PNL_LOG_INTERVAL_SECONDS, 10)
            ):
                return

            if execution_mode == "LIVE_ORDER":
                printed = False
                for _, broker_entry in (live_state.get("broker_positions") or {}).items():
                    if safe_int(broker_entry.get("broker_open_qty"), 0) <= 0:
                        continue
                    if bool(broker_entry.get("manual_or_broker_exit_detected")):
                        continue

                    side = str(broker_entry.get("side") or "")
                    option_symbol = str(broker_entry.get("option_symbol") or "")
                    entry_price = safe_float(broker_entry.get("entry_price_actual"), 0.0)
                    qty = safe_int(broker_entry.get("qty"), QTY)
                    if entry_price <= 0 or qty <= 0:
                        continue

                    try:
                        current_option_price = get_option_symbol_ltp(kite, option_symbol) if option_symbol else 0.0
                    except Exception:
                        current_option_price = 0.0
                    if current_option_price <= 0:
                        current_option_price = get_option_ltp_from_live_state(live_state, side)
                    if current_option_price <= 0:
                        continue

                    unrealized_points = current_option_price - entry_price
                    unrealized_rs = unrealized_points * qty
                    realized_rs = safe_float(live_state.get("realized_live_pnl_rs"), 0.0)
                    total_mtm_rs = realized_rs + unrealized_rs
                    trade_count = get_execution_entry_count()
                    entry_time = broker_entry.get("entry_broker_time") or broker_entry.get("entry_time") or ""
                    trailing_status = get_trailing_profit_status(unrealized_points)
                    trailing_suffix = f" | trailing={trailing_status}" if trailing_status else ""

                    print(
                        f"[OPEN PNL] {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                        f"entry_time={entry_time} | trade_count={trade_count} | symbol={option_symbol} | side={side} | ltp={current_option_price:.2f} | "
                        f"entry={entry_price:.2f} | qty={qty} | unrealized={unrealized_points:.2f} pts ({unrealized_rs:.2f} rs) | "
                        f"realized={realized_rs:.2f} rs | total_mtm={total_mtm_rs:.2f} rs{trailing_suffix}",
                        flush=True,
                    )
                    printed = True

                if printed:
                    live_state["last_open_pnl_log_ts"] = now_local
                return

            open_pos = trades.attrs.get("open_position") if hasattr(trades, "attrs") else None
            if not open_pos:
                # Fallback for temporary strategy-attr gaps during stall/rebuild windows.
                # In paper mode we allow only one open position, so the latest BUY
                # newer than latest SELL can be treated as currently open.
                events = live_state.get("live_events") or []
                last_buy = next((ev for ev in reversed(events) if str(ev.get("event_type") or "").upper() == "BUY"), None)
                last_sell = next((ev for ev in reversed(events) if str(ev.get("event_type") or "").upper() == "SELL"), None)
                if last_buy is not None:
                    buy_ts = pd.Timestamp(last_buy.get("event_time")) if last_buy.get("event_time") is not None else None
                    sell_ts = pd.Timestamp(last_sell.get("event_time")) if last_sell and last_sell.get("event_time") is not None else None
                    if sell_ts is None or (buy_ts is not None and buy_ts > sell_ts):
                        open_pos = {
                            "side": str(last_buy.get("side") or ""),
                            "option_symbol": str(last_buy.get("option_symbol") or ""),
                            "entry_time": last_buy.get("event_time"),
                            "entry_reason": str(last_buy.get("event_reason") or ""),
                            "entry_price": safe_float(last_buy.get("option_price"), 0.0),
                            "qty": safe_int(last_buy.get("qty"), QTY),
                            "emergency_sl_price": safe_float(last_buy.get("emergency_sl_price"), np.nan),
                        }

            if not open_pos:
                live_state["last_open_pnl_event_id"] = None
                return

            side = str(open_pos.get("side") or "")
            entry_reason = str(open_pos.get("entry_reason") or "")
            entry_time = (
                open_pos.get("entry_time")
                or open_pos.get("entry_broker_time")
                or open_pos.get("entry_event_time")
                or ""
            )
            open_event_id = make_entry_event_id(side, entry_time, open_pos.get("option_symbol"))
            if live_state.get("last_open_pnl_event_id") != open_event_id:
                live_state["last_open_pnl_event_id"] = open_event_id
                live_state["last_open_pnl_log_ts"] = None

            # Prefer the immutable simulated fill over rebuilt strategy state.
            pinned_entry_price = safe_float(
                (live_state.get("paper_entry_prices") or {}).get(open_event_id),
                0.0,
            )
            entry_price = pinned_entry_price
            if pinned_entry_price <= 0:
                entry_price = safe_float(open_pos.get("entry_price"), 0.0)
                for event in reversed(live_state.get("live_events") or []):
                    if str(event.get("event_type") or "").upper() != "BUY":
                        continue
                    if str(event.get("side") or "") != side:
                        continue
                    if str(event.get("event_reason") or "") != entry_reason:
                        continue
                    if str(event.get("event_time") or "") != str(entry_time):
                        continue
                    entry_price = safe_float(event.get("option_price"), entry_price)
                    break
            if entry_price <= 0:
                return
            # Try to read current LTP from live state; if unavailable, try a broker/kite lookup
            current_option_price = get_option_ltp_from_live_state(live_state, side)
            if current_option_price <= 0:
                # Prefer explicit option symbol lookup if available
                option_symbol = str(open_pos.get("option_symbol") or "")
                if option_symbol:
                    try:
                        current_option_price = get_option_symbol_ltp(kite, option_symbol)
                    except Exception:
                        current_option_price = 0.0
            # As a last resort, fall back to the entry price so we can still log PnL immediately
            if current_option_price <= 0:
                current_option_price = entry_price

            qty = safe_int(open_pos.get("qty"), QTY)
            unrealized_points = current_option_price - entry_price
            unrealized_rs = unrealized_points * qty
            realized_rs = safe_float(trades.attrs.get("realized_mtm_rs"), 0.0) if hasattr(trades, "attrs") else 0.0
            total_mtm_rs = realized_rs + unrealized_rs
            trade_count = get_execution_entry_count()
            peak_points = max(
                unrealized_points,
                safe_float(open_pos.get("high_during_trade"), current_option_price) - entry_price,
                safe_float(open_pos.get("best_price"), current_option_price) - entry_price,
            )
            trailing_status = get_trailing_profit_status(
                peak_points,
                current_time=now_local,
                stage1_triggered=safe_float(open_pos.get("trailing_stop_price"), 0.0) > safe_float(open_pos.get("emergency_sl_price"), 0.0),
            )
            trailing_suffix = f" | trailing={trailing_status}" if trailing_status else ""

            print(
                f"[OPEN PNL] {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                f"entry_time={entry_time} | trade_count={trade_count} | symbol={open_pos.get('option_symbol')} | side={side} | ltp={current_option_price:.2f} | "
                f"entry={entry_price:.2f} | qty={qty} | unrealized={unrealized_points:.2f} pts ({unrealized_rs:.2f} rs) | "
                f"realized={realized_rs:.2f} rs | total_mtm={total_mtm_rs:.2f} rs{trailing_suffix}",
                flush=True,
            )
            live_state["last_open_pnl_log_ts"] = now_local

    def print_execution_buy_summary(event_row: Dict) -> None:
        event_time = event_row.get("event_time")
        logged_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
        side_value = str(event_row.get("side") or "")
        symbol_value = str(event_row.get("option_symbol") or "")
        reason_value = str(event_row.get("event_reason") or "")
        option_price = safe_float(event_row.get("option_price"), 0.0)
        spot_price = safe_float(event_row.get("spot_price"), 0.0)
        qty_value = safe_int(event_row.get("qty"), QTY)
        emergency_sl = safe_float(event_row.get("emergency_sl_price"), 0.0)
        target_profit_points = safe_float(event_row.get("target_profit_points"), np.nan)
        target_suffix = f" target={target_profit_points:.2f}" if not np.isnan(target_profit_points) else ""
        pending_stage = str(event_row.get("trailing_status") or get_execution_buy_trailing_status(event_time))
        gift_ltp = safe_float(event_row.get("gift_nifty_ltp"), np.nan)
        gift_suffix = ""
        if np.isfinite(gift_ltp):
            gift_chg_pct = safe_float(event_row.get("gift_nifty_change_pct"), np.nan)
            gift_premium = safe_float(event_row.get("gift_nifty_premium"), np.nan)
            gift_suffix = f" gift_nifty={gift_ltp:.2f}"
            if np.isfinite(gift_chg_pct):
                gift_suffix += f" ({gift_chg_pct:+.2f}%)"
            if np.isfinite(gift_premium):
                gift_suffix += f" gift_premium={gift_premium:+.2f}"
        print(
            f"Entered position at {event_time} | logged_at={logged_at} | mode={execution_mode} | side={side_value} symbol={symbol_value} "
            f"reason={reason_value} entry={option_price:.2f} spot={spot_price:.2f} qty={qty_value} "
            f"emergency_sl={emergency_sl:.2f}{target_suffix} trailing={pending_stage}{gift_suffix}"
        )

    def get_execution_buy_trailing_status(event_time) -> str:
        try:
            ts_val = pd.Timestamp(event_time)
            if hasattr(ts_val, "to_pydatetime"):
                ts_val = ts_val.to_pydatetime()
            if ts_val.tzinfo is None:
                ts_val = IST.localize(ts_val)
            else:
                ts_val = ts_val.astimezone(IST)
            return "stage2_pending" if ts_val.strftime("%H:%M") >= TRAILING_PROFIT_STAGE2_TIME else "stage1_pending"
        except Exception:
            return "stage1_pending"

    def get_execution_trailing_status(event_row: Dict, trade_record: Optional[Dict] = None) -> str:
        trade_record = trade_record or {}
        reason_value = str(event_row.get("event_reason") or (trade_record or {}).get("exit_reason") or "").strip().upper()
        exit_time = event_row.get("event_time") or trade_record.get("exit_time")
        entry_price = safe_float(trade_record.get("entry_price"), 0.0)
        high_price = safe_float(trade_record.get("high_during_trade"), safe_float(trade_record.get("best_price"), entry_price))
        max_profit_points = high_price - entry_price if high_price > 0 and entry_price > 0 else 0.0
        try:
            exit_ts = pd.Timestamp(exit_time)
            if hasattr(exit_ts, "to_pydatetime"):
                exit_ts = exit_ts.to_pydatetime()
            if exit_ts.tzinfo is None:
                exit_ts = IST.localize(exit_ts)
            else:
                exit_ts = exit_ts.astimezone(IST)
            active_stage = "stage2" if exit_ts.strftime("%H:%M") >= TRAILING_PROFIT_STAGE2_TIME else "stage1"
        except Exception:
            active_stage = "stage1"
        trigger_points = float(TRAILING_PROFIT_STAGE2[0][0]) if active_stage == "stage2" and TRAILING_PROFIT_STAGE2 else float(TRAILING_PROFIT_STAGE1[0][0]) if TRAILING_PROFIT_STAGE1 else 50.0
        if reason_value in {"TRAILING_SL_HIT", "TRAILING_STOP_HIT"}:
            return f"{active_stage}_hit"
        if max_profit_points >= trigger_points:
            return f"{active_stage}_armed"
        return "not_reached_target"

    def print_execution_sell_summary(event_row: Dict, trade_record: Optional[Dict] = None) -> None:
        event_time = event_row.get("event_time")
        logged_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
        side_value = str(event_row.get("side") or (trade_record or {}).get("side") or "")
        symbol_value = str(event_row.get("option_symbol") or (trade_record or {}).get("option_symbol") or "")
        reason_value = str(event_row.get("event_reason") or (trade_record or {}).get("exit_reason") or "")
        exit_price = safe_float(event_row.get("option_price"), safe_float((trade_record or {}).get("exit_price"), 0.0))
        entry_price = safe_float((trade_record or {}).get("entry_price"), 0.0)
        pnl_points = safe_float((trade_record or {}).get("pnl_points"), exit_price - entry_price)
        pnl_rs = safe_float((trade_record or {}).get("pnl_rs"), pnl_points * safe_int((trade_record or {}).get("qty"), QTY))
        realized_mtm = safe_float(event_row.get("realized_mtm"), safe_float((trade_record or {}).get("realized_mtm_after_trade"), 0.0))
        target_profit_points = safe_float(event_row.get("target_profit_points"), safe_float((trade_record or {}).get("target_profit_points"), np.nan))
        target_suffix = f" target={target_profit_points:.2f}" if not np.isnan(target_profit_points) else ""
        trailing_status = str(event_row.get("trailing_status") or (trade_record or {}).get("trailing_status") or get_execution_trailing_status(event_row, trade_record))
        print(
            f"Exited position at {event_time} | logged_at={logged_at} with reason {reason_value}. "
            f"symbol={symbol_value} side={side_value} entry={entry_price:.2f} exit={exit_price:.2f} "
            f"| P&L: {pnl_rs:.2f} Rs ({pnl_points:.2f} points). Cumulative MTM: {realized_mtm:.2f} Rs."
            f"{target_suffix} trailing={trailing_status}"
        )

    def safe_stop_ws(ws):
        if ws is None:
            return
        try:
            ws.stop()
        except Exception as stop_exc:
            message = str(stop_exc).strip().lower()
            if "reactor" in message and "isn't running" in message:
                return
            print(f"[WS STOP WARNING] {stop_exc}")

    def attempt_tick_stall_recovery(now_local: datetime, stall_seconds: float):
        if stall_seconds < safe_int(TICK_STALL_RESUBSCRIBE_SECONDS, 60):
            return

        ws = live_state.get("ticker_ws")
        if ws is None or not live_state.get("ws_connected"):
            return

        last_attempt = live_state.get("last_tick_resubscribe_attempt_ts")
        cooldown = max(5, safe_int(TICK_STALL_RESUBSCRIBE_COOLDOWN_SECONDS, 45))
        if last_attempt is not None and (now_local - last_attempt).total_seconds() < cooldown:
            return

        tokens = [
            UNDERLYING_TOKEN,
            safe_int(live_state.get("current_ce_token"), ce_token),
            safe_int(live_state.get("current_pe_token"), pe_token),
        ]
        try:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
            print(
                f"[TICK STALL RECOVERY] Re-subscribed tokens after {int(stall_seconds)}s stall: "
                f"{tokens}"
            )
        except Exception as rec_exc:
            print(f"[TICK STALL RECOVERY FAILED] {rec_exc}")
        finally:
            live_state["last_tick_resubscribe_attempt_ts"] = now_local

        reconnect_after = max(
            safe_int(TICK_STALL_RECONNECT_SECONDS, 150),
            safe_int(TICK_STALL_RESUBSCRIBE_SECONDS, 60) + 10,
        )
        if stall_seconds < reconnect_after:
            return

        last_reconnect = live_state.get("last_tick_reconnect_attempt_ts")
        reconnect_cooldown = max(20, safe_int(TICK_STALL_RECONNECT_COOLDOWN_SECONDS, 120))
        if last_reconnect is not None and (now_local - last_reconnect).total_seconds() < reconnect_cooldown:
            return

        live_state["last_tick_reconnect_attempt_ts"] = now_local
        print(
            f"[TICK STALL RECONNECT] No ticks for {int(stall_seconds)}s. "
            "Restarting websocket connection..."
        )
        try:
            ws._close(reason="Tick stall recovery")
        except Exception as reconnect_exc:
            print(f"[TICK STALL RECONNECT FAILED] {reconnect_exc}")

    def detect_tick_stall():
        last_tick_ts = live_state.get("last_tick_ts")
        if last_tick_ts is None:
            return
        now_local = datetime.now(IST)
        stall_seconds = (now_local - last_tick_ts).total_seconds()
        if stall_seconds > safe_int(TICK_STALL_WARN_SECONDS, 20):
            last_logged = live_state.get("last_tick_stall_logged_ts")
            if last_logged is None or (now_local - last_logged).total_seconds() >= safe_int(IDLE_STATUS_INTERVAL_SECONDS, 30):
                print(f"[TICK STALL] No ticks received for {int(stall_seconds)} second(s). Last tick at {last_tick_ts}")
                live_state["last_tick_stall_logged_ts"] = now_local
            attempt_tick_stall_recovery(now_local, stall_seconds)

    def sync_broker_positions_if_needed(force: bool = False):
        if execution_mode != "LIVE_ORDER":
            return
        now_ts = time.time()
        last_sync = live_state.get("last_broker_sync_ts")
        if (not force) and last_sync and (now_ts - last_sync) < BROKER_SYNC_POLL_SECONDS:
            return
        live_state["last_broker_sync_ts"] = now_ts

        for event_id, broker_entry in list(live_state["broker_positions"].items()):
            synced = sync_broker_position_state(kite, broker_entry)

            # Live TP enforcement: do not wait for candle-close strategy exits.
            if ENABLE_TRAILING_PROFIT_EXIT and (not synced.get("manual_or_broker_exit_detected")) and safe_int(synced.get("broker_open_qty"), 0) > 0:
                side = str(synced.get("side") or "")
                entry_price_actual = safe_float(synced.get("entry_price_actual"), 0.0)
                option_symbol = str(synced.get("option_symbol") or synced.get("entry_order_symbol") or "")
                option_ltp = 0.0

                # Use the exact broker symbol first. The current websocket token
                # can still refer to a prior ATM strike after a debounced refresh.
                if option_symbol:
                    option_ltp = get_option_symbol_ltp(kite, option_symbol)
                if option_ltp <= 0 and side == "CE":
                    ce_tick = live_state.get("current", {}).get(safe_int(live_state.get("current_ce_token"), 0)) or {}
                    option_ltp = safe_float(ce_tick.get("close"), 0.0)
                elif option_ltp <= 0 and side == "PE":
                    pe_tick = live_state.get("current", {}).get(safe_int(live_state.get("current_pe_token"), 0)) or {}
                    option_ltp = safe_float(pe_tick.get("close"), 0.0)

                if entry_price_actual > 0 and option_ltp > 0:
                    prev_best_price = safe_float(synced.get("best_price"), entry_price_actual)
                    if prev_best_price <= 0:
                        prev_best_price = entry_price_actual
                    synced["best_price"] = max(prev_best_price, option_ltp)
                    trail_now = datetime.now(IST)
                    synced["best_price_stage2"] = update_stage2_peak(synced.get("best_price_stage2"), option_ltp, trail_now)
                    pnl_points_live = option_ltp - entry_price_actual
                    pnl_points_high = get_trailing_peak_points(
                        entry_price_actual, synced["best_price"], synced["best_price_stage2"], trail_now
                    )
                    synced["live_pnl_points"] = pnl_points_live
                    synced["live_pnl_points_high"] = synced["best_price"] - entry_price_actual
                    current_stop = safe_float(synced.get("trailing_stop_price"), safe_float(synced.get("emergency_sl_price"), 0.0))
                    _stage1_hit = bool(synced.get("trailing_stop_active"))
                    if not _stage1_hit and safe_float(synced.get("trailing_stop_price"), 0.0) > safe_float(synced.get("emergency_sl_price"), 0.0):
                        synced["trailing_stop_price"] = safe_float(synced.get("emergency_sl_price"), 0.0)
                        current_stop = safe_float(synced.get("trailing_stop_price"), 0.0)
                    trailing_stop_price = get_trailing_stop_for_profit(
                        entry_price_actual,
                        pnl_points_high,
                        current_stop,
                        current_time=trail_now,
                        stage1_triggered=_stage1_hit,
                    )
                    # Sanity-check the computed trailing stop before applying it.
                    # Only adopt a raised trailing stop (which is entry_price + stop_profit)
                    # as actionable if the live PnL has reached at least the first
                    # configured trigger level; otherwise keep the previous stop to
                    # avoid an immediate accidental exit.
                    min_trigger = None
                    try:
                        active = get_active_trailing_stages(current_time=trail_now, stage1_triggered=_stage1_hit)
                        if active and len(active) > 0:
                            min_trigger = float(active[0][0])
                    except Exception:
                        min_trigger = None

                    # If trailing_stop_price looks like it was raised above current
                    # market LTP but the PnL hasn't reached the trigger, ignore the
                    # raised value to avoid immediate exits.
                    if (min_trigger is not None) and (pnl_points_high < float(min_trigger)) and (trailing_stop_price > option_ltp):
                        print(
                            f"[TRAIL SANITY] Ignoring raised trailing_stop_price={trailing_stop_price:.2f} "
                            f"because peak_pnl_pts={pnl_points_high:.2f} < trigger={min_trigger:.2f}. Keeping current_stop={current_stop:.2f}"
                        )
                        synced["trailing_stop_price"] = current_stop
                    else:
                        synced["trailing_stop_price"] = trailing_stop_price
                    if min_trigger is not None and pnl_points_high >= float(min_trigger):
                        synced["trailing_stop_active"] = True

                    if synced.get("trailing_stop_price", 0) > 0 and option_ltp <= float(synced.get("trailing_stop_price", 0)):
                        print(
                            f"[LIVE TRAIL EXIT] event_id={event_id} symbol={option_symbol} "
                            f"entry={entry_price_actual:.2f} ltp={option_ltp:.2f} "
                            f"pnl_pts={pnl_points_live:.2f} trail={trailing_stop_price:.2f}"
                        )
                        exit_order_id = str(synced.get("exit_order_id") or "").strip()
                        existing_exit_open = False
                        if exit_order_id:
                            exit_snap = get_order_snapshot(kite, exit_order_id)
                            exit_status = normalize_order_status(exit_snap.get("status"))
                            synced["exit_order_status"] = exit_status
                            if exit_status in {"COMPLETE", "OPEN", "TRIGGER PENDING", "PENDING", "PUT ORDER REQ RECEIVED", "VALIDATION PENDING", "MODIFY VALIDATION PENDING", "MODIFY PENDING", "AMO REQ RECEIVED"}:
                                existing_exit_open = True
                            if exit_status == "COMPLETE":
                                synced.update({
                                    "broker_exit_order_id": exit_order_id,
                                    "broker_exit_avg_price": extract_avg_price(exit_snap),
                                    "broker_exit_time": extract_order_time(exit_snap),
                                    "broker_exit_reason": "TRAILING_STOP_HIT",
                                    "manual_or_broker_exit_detected": True,
                                    "broker_open_qty": 0,
                                })

                        if (not existing_exit_open) and (not synced.get("manual_or_broker_exit_detected")) and side in {"CE", "PE"}:
                            exit_qty = safe_int(synced.get("broker_open_qty"), 0)
                            if exit_qty > 0:
                                try:
                                    order_meta = safe_place_live_exit_order(
                                        kite,
                                        side,
                                        ce_symbol,
                                        pe_symbol,
                                        exit_qty,
                                        broker_entry=synced,
                                    )
                                    if order_meta.get("skipped"):
                                        if order_meta.get("skip_reason") == "NO_POSITION_TO_EXIT":
                                            synced["manual_or_broker_exit_detected"] = True
                                    else:
                                        synced.update(order_meta)
                                        exit_snapshot = wait_for_order_terminal(kite, str(order_meta.get("exit_order_id") or ""))
                                        exit_status = normalize_order_status(exit_snapshot.get("status"))
                                        synced["exit_order_status"] = exit_status
                                        if exit_status == "COMPLETE":
                                            synced.update({
                                                "broker_exit_order_id": str(order_meta.get("exit_order_id") or ""),
                                                "broker_exit_avg_price": extract_avg_price(exit_snapshot),
                                                "broker_exit_time": extract_order_time(exit_snapshot),
                                                "broker_exit_reason": "TRAILING_STOP_HIT",
                                                "manual_or_broker_exit_detected": True,
                                                "broker_open_qty": 0,
                                            })
                                        else:
                                            print(
                                                f"[LIVE TRAIL EXIT PENDING] event_id={event_id} "
                                                f"order_id={order_meta.get('exit_order_id')} status={exit_status}"
                                            )
                                except Exception as exc:
                                    synced["exit_order_message"] = str(exc)
                                    print(f"[LIVE TP EXIT ERROR] event_id={event_id} error={exc}")

            live_state["broker_positions"][event_id] = synced
            if synced.get("manual_or_broker_exit_detected") and live_state["active_order_event_id"] == event_id:
                live_state["active_order_event_id"] = None

    def append_broker_forced_exit_trades():
        if execution_mode != "LIVE_ORDER":
            return False
        new_events_added = False
        for event_id, broker_entry in list(live_state["broker_positions"].items()):
            if not broker_entry.get("manual_or_broker_exit_detected"):
                continue

            exit_reason = normalize_exit_reason(
                str(broker_entry.get("broker_exit_reason") or "MANUAL_OR_EXTERNAL_EXIT"),
                default="MANUAL_OR_EXTERNAL_EXIT",
            )
            trade_id = f"{event_id}|BROKER_EXIT|{exit_reason}"
            if trade_id in live_state["printed_trade_ids"]:
                continue

            actual_entry_price = safe_float(broker_entry.get("entry_price_actual"))
            actual_exit_price = safe_float(broker_entry.get("broker_exit_avg_price"))
            actual_qty = safe_int(broker_entry.get("qty") or QTY, QTY)
            actual_pnl_points = actual_exit_price - actual_entry_price
            actual_pnl_rs = actual_pnl_points * actual_qty
            prior_realized = safe_float(live_state.get("realized_live_pnl_rs", 0.0))
            live_state["realized_live_pnl_rs"] = prior_realized + actual_pnl_rs

            exit_time = broker_entry.get("broker_exit_time") or datetime.now(IST)
            live_state["last_live_exit_time"] = exit_time
            exit_spot = get_last_price(kite, UNDERLYING_TOKEN, "NSE")

            signal_entry_price = safe_float(broker_entry.get("signal_option_price"), safe_float(broker_entry.get("entry_price_theoretical")))
            broker_trade = {
                "run_mode": execution_mode,
                "trade_date": trade_date,
                "underlying": UNDERLYING_NAME,
                "side": str(broker_entry.get("side", "")),
                "option_symbol": str(broker_entry.get("option_symbol", "")),
                "entry_time": broker_entry.get("entry_time"),
                "exit_time": exit_time,
                "entry_reason": str(broker_entry.get("entry_reason", "")),
                "exit_reason": exit_reason,
                "entry_spot": safe_float(broker_entry.get("entry_spot")),
                "exit_spot": safe_float(exit_spot),
                "signal_time": broker_entry.get("signal_time", broker_entry.get("entry_time")),
                "signal_spot_price": safe_float(broker_entry.get("signal_spot_price"), safe_float(broker_entry.get("entry_spot"))),
                "signal_option_price": signal_entry_price,
                "entry_price_strategy": signal_entry_price,
                "exit_signal_time": exit_time,
                "exit_signal_spot_price": safe_float(exit_spot),
                "exit_signal_option_price": np.nan,
                "exit_price_strategy": np.nan,
                "entry_price": actual_entry_price,
                "exit_price": actual_exit_price,
                "qty": actual_qty,
                "pnl_points": actual_pnl_points,
                "pnl_rs": actual_pnl_rs,
                "realized_mtm_after_trade": live_state["realized_live_pnl_rs"],
                "best_price": safe_float(broker_entry.get("best_price"), actual_entry_price),
                "high_during_trade": safe_float(broker_entry.get("best_price"), actual_entry_price),
                "mtf_entry_label": str(broker_entry.get("mtf_entry_label", "")),
                "mtf_exit_label": "",
                "entry_order_id": str(broker_entry.get("entry_order_id", "")),
                "entry_order_status": str(broker_entry.get("entry_order_status", "")),
                "entry_broker_time": broker_entry.get("entry_broker_time"),
                "entry_order_message": str(broker_entry.get("entry_order_message", "")),
                "exit_order_id": str(broker_entry.get("broker_exit_order_id", "")),
                "exit_order_status": "COMPLETE" if actual_exit_price > 0 else "",
                "exit_broker_time": broker_entry.get("broker_exit_time"),
                "exit_order_message": "",
                "slippage_entry_points": actual_entry_price - signal_entry_price,
                "slippage_exit_points": np.nan,
                "slippage_total_points": np.nan,
                "emergency_sl_pct_used": safe_float(broker_entry.get("emergency_sl_pct_used"), 0.0),
                "target_profit_points": safe_float(broker_entry.get("target_profit_points"), np.nan),
            }
            broker_trade["trailing_status"] = get_execution_trailing_status({}, broker_trade)

            sell_event_row = format_live_event_row(
                event_type="SELL",
                run_mode=execution_mode,
                trade_date=trade_date,
                underlying=UNDERLYING_NAME,
                side=str(broker_entry.get("side", "")),
                option_symbol=str(broker_entry.get("option_symbol", "")),
                event_time=exit_time,
                event_reason=exit_reason,
                spot_price=exit_spot,
                option_price=actual_exit_price,
                qty=actual_qty,
                realized_mtm=live_state["realized_live_pnl_rs"],
                mtf_label="",
                emergency_sl_pct_used=safe_float(broker_entry.get("emergency_sl_pct_used"), 0.0),
                emergency_sl_price=safe_float(broker_entry.get("emergency_sl_price"), np.nan),
                target_profit_points=safe_float(broker_entry.get("target_profit_points"), np.nan),
                trailing_status=str(broker_trade.get("trailing_status") or ""),
            )
            print_execution_sell_summary(sell_event_row, broker_trade)
            print(f"\n========== {execution_mode} SELL ==========")
            print(pd.DataFrame([sell_event_row]).to_string(index=False))
            print(f"\n========== NEW {execution_mode} TRADE(S) ==========")
            print(pd.DataFrame([broker_trade]).to_string(index=False))
            live_state["live_events"].append(sell_event_row)
            live_state["live_trade_events"].append(broker_trade)
            register_symbol_exit_cooldown(
                live_state,
                broker_entry.get("option_symbol", ""),
                exit_time,
                broker_entry.get("broker_exit_avg_price"),
            )
            live_state["printed_sell_event_ids"].add(event_id)
            live_state["printed_trade_ids"].add(event_id)
            live_state["broker_positions"].pop(event_id, None)

            # The broker exit has already flattened the position. Do not keep
            # the first-trade strike lock active after an external/emergency exit.
            live_state["locked_trade_strike_active"] = False
            live_state["locked_ce_symbol"] = ""
            live_state["locked_pe_symbol"] = ""
            live_state["locked_ce_token"] = 0
            live_state["locked_pe_token"] = 0
            live_state["last_symbol_lock_skip_reason"] = ""
            try:
                refresh_entry_symbols(reason="POST_EXIT")
            except Exception:
                pass

    def force_exit_live_order_positions(exit_reason: str = "FORCE_EXIT"):
        """Immediately flatten all tracked broker positions in LIVE_ORDER mode."""
        if execution_mode != "LIVE_ORDER":
            return

        normalized_exit_reason = normalize_exit_reason(exit_reason, default="FORCE_EXIT")
        print(f"[FORCE EXIT ACTION] Flattening open LIVE_ORDER positions with reason={normalized_exit_reason}")

        for event_id, broker_entry in list(live_state["broker_positions"].items()):
            try:
                synced = sync_broker_position_state(kite, broker_entry)
                live_state["broker_positions"][event_id] = synced

                if synced.get("manual_or_broker_exit_detected"):
                    continue

                open_qty = safe_int(synced.get("broker_open_qty"), 0)
                if open_qty <= 0:
                    continue

                side = str(synced.get("side") or "")
                if side not in {"CE", "PE"}:
                    continue

                order_meta = safe_place_live_exit_order(
                    kite,
                    side,
                    live_state.get("current_ce_symbol", ce_symbol),
                    live_state.get("current_pe_symbol", pe_symbol),
                    open_qty,
                    broker_entry=synced,
                )

                if order_meta.get("skipped"):
                    continue

                exit_snapshot = wait_for_order_terminal(kite, str(order_meta.get("exit_order_id") or ""))
                exit_status = normalize_order_status(exit_snapshot.get("status"))
                synced["exit_order_status"] = exit_status

                if exit_status == "COMPLETE":
                    synced.update(order_meta)
                    synced.update({
                        "broker_exit_order_id": str(order_meta.get("exit_order_id") or ""),
                        "broker_exit_avg_price": extract_avg_price(exit_snapshot),
                        "broker_exit_time": extract_order_time(exit_snapshot),
                        "broker_exit_reason": normalized_exit_reason,
                        "manual_or_broker_exit_detected": True,
                        "broker_open_qty": 0,
                        "exit_in_progress": False,
                    })
                else:
                    print(
                        f"[FORCE EXIT WARNING] event_id={event_id} order_id={order_meta.get('exit_order_id')} "
                        f"status={exit_status}"
                    )

                live_state["broker_positions"][event_id] = synced
            except Exception as exc:
                print(f"[FORCE EXIT ERROR] event_id={event_id} error={exc}")

    def rebuild_and_print():
        sync_broker_positions_if_needed()

        retry_live_history_backfill()

        # Periodic ATM symbol refresh: re-select every N minutes when no position is open
        if AUTO_SELECT_OPTION_SYMBOLS and not AUTO_SELECT_SESSION_LOCKED:
            refresh_interval = max(1, safe_int(AUTO_SELECT_REFRESH_INTERVAL_MINUTES, 5))
            now_for_refresh = datetime.now(IST)
            last_refresh = live_state.get("last_symbol_refresh_ts")
            no_open_positions = (
                get_open_local_position_count(live_state) == 0
                and live_state.get("active_order_event_id") is None
            )
            if no_open_positions and live_state.get("locked_trade_strike_active"):
                live_state["locked_trade_strike_active"] = False
                live_state["locked_ce_symbol"] = ""
                live_state["locked_pe_symbol"] = ""
                live_state["locked_ce_token"] = 0
                live_state["locked_pe_token"] = 0
                live_state["last_symbol_lock_skip_reason"] = ""
            entry_window_open = get_new_entry_block_reason() == ""
            refresh_due = (
                last_refresh is None
                or (now_for_refresh - last_refresh).total_seconds() >= refresh_interval * 60
            )
            if no_open_positions and refresh_due and entry_window_open:
                try:
                    refresh_entry_symbols(reason="PERIODIC_NO_TRADE")
                except Exception as _ref_exc:
                    print(f"[PERIODIC SYMBOL REFRESH ERROR] {_ref_exc}")
                live_state["last_symbol_refresh_ts"] = now_for_refresh

        spot_df = rows_to_df(live_state["spot_rows"])
        ce_df = rows_to_df(live_state["ce_rows"])
        pe_df = rows_to_df(live_state["pe_rows"])

        if spot_df.empty or ce_df.empty or pe_df.empty:
            emit_idle_status("Waiting for enough completed candles to evaluate signals")
            return

        day = prepare_merged_day(spot_df, ce_df, pe_df)
        for k, v in (live_state.get("market_context") or {}).items():
            day.attrs[k] = v
        live_state["last_day_debug_df"] = day.copy()
        paper_position_active = execution_mode == "PAPERTRADE_LIVE" and live_state.get("active_order_event_id") is not None
        previous_paper_open = None
        if execution_mode == "PAPERTRADE_LIVE" and isinstance(live_state.get("final_trades"), pd.DataFrame):
            previous_paper_open = live_state["final_trades"].attrs.get("open_position")
        live_symbols_locked = (
            execution_mode == "LIVE_ORDER"
            and live_state.get("locked_trade_strike_active")
            and get_open_local_position_count(live_state) > 0
        )
        strategy_tracking_start = live_tracking_start
        if execution_mode in {"LIVE_ORDER", "PAPERTRADE_LIVE"} and live_state.get("last_live_exit_time") is not None:
            try:
                last_live_exit = pd.Timestamp(live_state["last_live_exit_time"])
                if last_live_exit.tzinfo is None:
                    last_live_exit = last_live_exit.tz_localize(IST)
                else:
                    last_live_exit = last_live_exit.tz_convert(IST)
                cooldown_after_exit = timedelta(minutes=safe_int(REENTRY_COOLDOWN_MINUTES, 0))
                strategy_tracking_start = max(
                    strategy_tracking_start,
                    last_live_exit + cooldown_after_exit + timedelta(seconds=1),
                )
            except Exception:
                strategy_tracking_start = live_tracking_start
        # The open trade is already in the execution entry count. Count only the
        # entries before it, or MAX_TRADES stops the rebuild before it can
        # re-create the open trade and its stops are never evaluated.
        open_execution_trades = (
            get_open_local_position_count(live_state)
            if execution_mode == "LIVE_ORDER"
            else int(live_state.get("active_order_event_id") is not None)
        )
        trades = run_single_day_test(
            day,
            execution_mode,
            # The rebuild starts after the last exit, so seed it with the session's
            # realized P&L for the same MTM guards the backtest applies.
            previous_realized_mtm=get_execution_realized_pnl(),
            execution_entry_count=max(0, get_execution_entry_count() - open_execution_trades),
            tracking_start_override=strategy_tracking_start,
            ce_symbol_override=live_state.get("current_ce_symbol") or ce_symbol,
            pe_symbol_override=live_state.get("current_pe_symbol") or pe_symbol,
            ce_token_override=safe_int(live_state.get("current_ce_token"), 0),
            pe_token_override=safe_int(live_state.get("current_pe_token"), 0),
            lock_symbols_during_open=paper_position_active or live_symbols_locked,
        )

        # PAPERTRADE_LIVE must enforce the same stop against the newest
        # completed option candle even if an incremental rebuild leaves the
        # strategy result marked as open.
        if execution_mode == "PAPERTRADE_LIVE":
            paper_open = trades.attrs.get("open_position") if hasattr(trades, "attrs") else None
            paper_pending = live_state.get("paper_exit_pending") or {}
            if isinstance(paper_open, dict):
                latest_buy = next(
                    (
                        event for event in reversed(live_state.get("live_events") or [])
                        if str(event.get("event_type") or "").upper() == "BUY"
                        and str(event.get("side") or "").upper() == str(paper_open.get("side") or "").upper()
                        and (
                            not str(paper_open.get("option_symbol") or "")
                            or str(event.get("option_symbol") or "") == str(paper_open.get("option_symbol") or "")
                        )
                    ),
                    None,
                )
                if latest_buy is not None:
                    recorded_entry = safe_float(latest_buy.get("option_price"), 0.0)
                    rebuilt_entry = safe_float(paper_open.get("entry_price"), 0.0)
                    if recorded_entry > 0 and abs(rebuilt_entry - recorded_entry) > OPTION_TICK_SIZE:
                        paper_open.update({
                            "entry_time": latest_buy.get("event_time"),
                            "entry_reason": str(latest_buy.get("event_reason") or ""),
                            "entry_spot": safe_float(latest_buy.get("spot_price"), 0.0),
                            "entry_price": recorded_entry,
                            "best_price": recorded_entry,
                            "high_during_trade": recorded_entry,
                            "emergency_sl_price": safe_float(
                                latest_buy.get("emergency_sl_price"),
                                get_entry_stop_price(recorded_entry),
                            ),
                            "trailing_stop_price": safe_float(
                                latest_buy.get("emergency_sl_price"),
                                get_entry_stop_price(recorded_entry),
                            ),
                            "trailing_stop_active": False,
                        })
            if paper_pending and not paper_open:
                # Preserve a tick-triggered exit until the paper position is
                # visible to the reconciliation pass. Clearing it here can
                # lose the only stop event during a transient rebuild gap.
                paper_open = None
            if paper_open and not day.empty:
                if isinstance(previous_paper_open, dict):
                    previous_side = str(previous_paper_open.get("side") or "").upper()
                    current_side = str(paper_open.get("side") or "").upper()
                    if previous_side == current_side:
                        previous_high = safe_float(
                            previous_paper_open.get("high_during_trade"),
                            safe_float(previous_paper_open.get("best_price"), 0.0),
                        )
                        current_high = safe_float(
                            paper_open.get("high_during_trade"),
                            safe_float(paper_open.get("best_price"), 0.0),
                        )
                        preserved_high = max(previous_high, current_high)
                        paper_open["best_price"] = preserved_high
                        paper_open["high_during_trade"] = preserved_high
                        stage2_peaks = [
                            safe_float(peak, 0.0)
                            for peak in (previous_paper_open.get("best_price_stage2"), paper_open.get("best_price_stage2"))
                            if peak is not None
                        ]
                        paper_open["best_price_stage2"] = max(stage2_peaks) if stage2_peaks else None
                        previous_trailing_stop = safe_float(
                            previous_paper_open.get("trailing_stop_price"),
                            safe_float(previous_paper_open.get("emergency_sl_price"), 0.0),
                        )
                        current_trailing_stop = safe_float(
                            paper_open.get("trailing_stop_price"),
                            safe_float(paper_open.get("emergency_sl_price"), 0.0),
                        )
                        paper_open["trailing_stop_price"] = max(
                            previous_trailing_stop,
                            current_trailing_stop,
                        )
                        paper_open["trailing_stop_active"] = bool(
                            previous_paper_open.get("trailing_stop_active")
                            or paper_open.get("trailing_stop_active")
                            or paper_open["trailing_stop_price"]
                            > safe_float(paper_open.get("emergency_sl_price"), 0.0)
                        )
                paper_side = str(paper_open.get("side") or "").upper()
                pending_matches = str(paper_pending.get("side") or "").upper() == paper_side
                paper_ltp = safe_float(
                    paper_pending.get("ltp") if pending_matches else day.iloc[-1].get("ce_close" if paper_side == "CE" else "pe_close"),
                    0.0,
                )
                paper_low = safe_float(
                    paper_pending.get("ltp")
                    if pending_matches
                    else day.iloc[-1].get("ce_low" if paper_side == "CE" else "pe_low"),
                    paper_ltp,
                )
                emergency_stop = safe_float(paper_open.get("emergency_sl_price"), 0.0)
                # Without a tick-triggered exit, test the newest candle against the stop
                # in place when it opened (the previous rebuild's stop): a stop raised
                # from this candle's high only applies from the next candle.
                paper_prev_stop = (
                    safe_float(previous_paper_open.get("trailing_stop_price"), 0.0)
                    if isinstance(previous_paper_open, dict)
                    and str(previous_paper_open.get("side") or "").upper() == paper_side
                    else 0.0
                )
                paper_stop = safe_float(
                    paper_pending.get("stop") if pending_matches else (paper_prev_stop or emergency_stop),
                    0.0,
                )
                pending_exit_reason = str(paper_pending.get("exit_reason") or "TRAILING_STOP_HIT") if pending_matches else "TRAILING_STOP_HIT"
                paper_exit_reason = (
                    "EMERGENCY_SL_HIT"
                    if emergency_stop > 0 and paper_stop <= emergency_stop + (OPTION_TICK_SIZE / 2.0)
                    else pending_exit_reason
                )
                paper_stop_hit = (
                    paper_stop > 0
                    and (
                        (pending_matches and paper_ltp <= paper_stop)
                        or (not pending_matches and paper_low <= paper_stop)
                    )
                )
                if paper_side in {"CE", "PE"} and paper_ltp > 0 and paper_stop_hit:
                    # A tick exit fills at the crossing tick; a candle exit fills at the
                    # stop level, or at the open if the candle gapped below the stop.
                    # Configured sell slippage is applied on top.
                    paper_candle_open = safe_float(day.iloc[-1].get("ce_open" if paper_side == "CE" else "pe_open"), 0.0)
                    if pending_matches:
                        paper_fill = min(paper_stop, paper_ltp)
                    elif 0 < paper_candle_open <= paper_stop:
                        paper_fill = paper_candle_open
                    else:
                        paper_fill = paper_stop
                    paper_exit_price = apply_paper_exit_slippage(paper_fill, side="SELL")
                    paper_entry_price = safe_float(paper_open.get("entry_price"), 0.0)
                    paper_qty = safe_int(paper_open.get("qty"), QTY)
                    paper_pnl_points = paper_exit_price - paper_entry_price
                    paper_pnl_rs = paper_pnl_points * paper_qty
                    paper_realized = safe_float(trades.attrs.get("realized_mtm_rs"), 0.0) + paper_pnl_rs
                    paper_exit_ts = paper_pending.get("exit_time") if pending_matches else day.iloc[-1].get("spot_ts") or day.iloc[-1].get("ts")
                    paper_trade = {
                        "run_mode": execution_mode,
                        "trade_date": trade_date,
                        "underlying": UNDERLYING_NAME,
                        "side": paper_side,
                        "option_symbol": str(paper_open.get("option_symbol") or (live_state.get("current_ce_symbol") if paper_side == "CE" else live_state.get("current_pe_symbol"))),
                        "entry_time": paper_open.get("entry_time"),
                        "exit_time": paper_exit_ts,
                        "entry_reason": str(paper_open.get("entry_reason") or ""),
                        "exit_reason": paper_exit_reason,
                        "entry_spot": safe_float(paper_open.get("entry_spot"), 0.0),
                        "exit_spot": safe_float(day.iloc[-1].get("spot_close"), 0.0),
                        "entry_price": paper_entry_price,
                        "exit_price": paper_exit_price,
                        "best_price": safe_float(paper_open.get("best_price"), paper_entry_price),
                        "high_during_trade": safe_float(paper_open.get("high_during_trade"), paper_entry_price),
                        "qty": paper_qty,
                        "pnl_points": paper_pnl_points,
                        "pnl_rs": paper_pnl_rs,
                        "realized_mtm_after_trade": paper_realized,
                        "mtf_entry_label": str(paper_open.get("mtf_entry_label") or ""),
                        "mtf_exit_label": str(day.iloc[-1].get("mtf_label") or ""),
                        "emergency_sl_pct_used": safe_float(paper_open.get("emergency_sl_pct_used"), 0.0),
                        "emergency_sl_price": safe_float(paper_open.get("emergency_sl_price"), np.nan),
                        "target_profit_points": safe_float(paper_open.get("target_profit_points"), 50.0),
                        "trailing_status": "stage2_hit" if pd.Timestamp(paper_exit_ts).strftime("%H:%M") >= TRAILING_PROFIT_STAGE2_TIME else "stage1_hit",
                    }
                    trades = pd.concat([trades, pd.DataFrame([paper_trade])], ignore_index=True)
                    trades.attrs.update({
                        "open_position": None,
                        "realized_mtm_rs": paper_realized,
                        "guard_stop_reason": paper_exit_reason,
                    })
                    live_state["paper_exit_pending"] = None
        live_state["final_trades"] = trades

        new_events_added = append_broker_forced_exit_trades()

        open_position = trades.attrs.get("open_position")
        if execution_mode == "PAPERTRADE_LIVE" and open_position:
            live_state["paper_open_position"] = dict(open_position)
        current_open_event_id = None
        pending_buy = None  # defer BUY printing until after closed trades
        if open_position:
            current_open_event_id = make_entry_event_id(
                open_position["side"], open_position["entry_time"], open_position.get("option_symbol")
            )

            if execution_mode != "LIVE_ORDER":
                # Paper mode follows strategy state directly.
                if live_state["active_order_event_id"] is None:
                    live_state["active_order_event_id"] = current_open_event_id
                    log_entry_diag("SET_ACTIVE_ORDER_EVENT", open_position)
                if current_open_event_id not in live_state["printed_buy_event_ids"] and live_state["active_order_event_id"] == current_open_event_id:
                    pending_buy = open_position  # will be printed after closed trades
                    log_entry_diag("PENDING_BUY_CREATED", pending_buy)
                elif current_open_event_id in live_state["printed_buy_event_ids"]:
                    log_entry_diag("BUY_ALREADY_PRINTED", open_position)
                elif live_state["active_order_event_id"] != current_open_event_id:
                    log_entry_diag(
                        "BLOCKED_BY_ACTIVE_ORDER_EVENT",
                        open_position,
                        extra=f"active={live_state.get('active_order_event_id')} current={current_open_event_id}",
                    )
            else:
                # Live mode supports multiple open positions. Each broker entry is tracked
                # independently, so a fresh BUY can be placed for a new signal even while
                # another position remains open.
                if (
                    current_open_event_id not in live_state["printed_buy_event_ids"]
                    and live_state.get("live_entry_in_progress_event_id") is None
                ):
                    pending_buy = open_position
                    log_entry_diag("PENDING_BUY_CREATED", pending_buy)
        elif live_state["active_order_event_id"] is not None:
            if execution_mode != "LIVE_ORDER":
                # Paper mode unlock is handled only when a SELL is emitted for the
                # active event. This avoids transient rebuild states from releasing
                # the BUY lock and allowing an opposite-side BUY without a logged SELL.
                pass

        # --- Process closed trades first (chronological order) ---
        terminal_paper_exit = False
        if not trades.empty:
            for _, trade in trades.iterrows():
                trade_id = f"{trade['side']}|{trade['entry_time']}|{trade['entry_reason']}|{trade['option_symbol']}"
                if trade_id in live_state["printed_trade_ids"]:
                    continue

                closed_order_event_id = make_entry_event_id(
                    trade["side"], trade["entry_time"], trade.get("option_symbol")
                )
                if execution_mode == "LIVE_ORDER":
                    # Do not replay historical strategy-only trades in LIVE_ORDER mode.
                    # Only process trades that are backed by a tracked broker entry.
                    if closed_order_event_id not in live_state["broker_positions"]:
                        continue

                sell_event_id = trade_id
                # Emit BUY event for this trade if not already printed
                buy_event_id = make_entry_event_id(
                    trade["side"], trade["entry_time"], trade.get("option_symbol")
                )
                if buy_event_id not in live_state["printed_buy_event_ids"]:
                    buy_event_row = format_live_event_row(
                        event_type="BUY",
                        run_mode=str(trade["run_mode"]),
                        trade_date=str(trade["trade_date"]),
                        underlying=str(trade["underlying"]),
                        side=str(trade["side"]),
                        option_symbol=str(trade["option_symbol"]),
                        event_time=trade["entry_time"],
                        event_reason=str(trade["entry_reason"]),
                        spot_price=float(trade["entry_spot"]),
                        option_price=float(trade["entry_price"]),
                        qty=int(trade["qty"]),
                        realized_mtm=0.0,
                        mtf_label=str(trade["mtf_entry_label"]),
                        gap_type=str(trade.get("gap_type", "")),
                        gap_points=safe_float(trade.get("gap_points"), np.nan),
                        gap_pct=safe_float(trade.get("gap_pct"), np.nan),
                        cpr_type=str(trade.get("cpr_type", "")),
                        cpr_width_pct=safe_float(trade.get("cpr_width_pct"), np.nan),
                        emergency_sl_pct_used=safe_float(trade.get("emergency_sl_pct_used"), 0.0),
                        emergency_sl_price=safe_float(trade.get("emergency_sl_price"), np.nan),
                        target_profit_points=safe_float(trade.get("target_profit_points"), np.nan),
                        trailing_status=str(trade.get("trailing_status") or get_execution_buy_trailing_status(trade.get("entry_time"))),
                    )
                    print_execution_buy_summary(buy_event_row)
                    print(f"\n========== {execution_mode} BUY ==========")
                    print(pd.DataFrame([buy_event_row]).to_string(index=False))
                    live_state["live_events"].append(buy_event_row)
                    live_state["printed_buy_event_ids"].add(buy_event_id)
                    new_events_added = True
                if sell_event_id not in live_state["printed_sell_event_ids"]:
                    sell_event_row = format_live_event_row(
                        event_type="SELL",
                        run_mode=str(trade["run_mode"]),
                        trade_date=str(trade["trade_date"]),
                        underlying=str(trade["underlying"]),
                        side=str(trade["side"]),
                        option_symbol=str(trade["option_symbol"]),
                        event_time=trade["exit_time"],
                        event_reason=str(trade["exit_reason"]),
                        spot_price=float(trade["exit_spot"]),
                        option_price=float(trade["exit_price"]),
                        qty=int(trade["qty"]),
                        realized_mtm=float(trade["realized_mtm_after_trade"]),
                        mtf_label=str(trade["mtf_exit_label"]),
                        gap_type=str(trade.get("gap_type", "")),
                        gap_points=safe_float(trade.get("gap_points"), np.nan),
                        gap_pct=safe_float(trade.get("gap_pct"), np.nan),
                        cpr_type=str(trade.get("cpr_type", "")),
                        cpr_width_pct=safe_float(trade.get("cpr_width_pct"), np.nan),
                        emergency_sl_pct_used=safe_float(trade.get("emergency_sl_pct_used"), 0.0),
                        emergency_sl_price=safe_float(trade.get("emergency_sl_price"), np.nan),
                    )
                    trade_record = trade.to_dict()
                    if execution_mode == "LIVE_ORDER":
                        force_exit_active = live_state.get("force_exit_triggered") or datetime.now(IST) >= force_exit_ts
                        resolved_exit_reason = "FORCE_EXIT" if force_exit_active else str(trade_record.get("exit_reason") or "STRATEGY_EXIT")
                        resolved_exit_reason = normalize_exit_reason(resolved_exit_reason)
                        trade_record["exit_reason"] = resolved_exit_reason
                        sell_event_row["event_reason"] = resolved_exit_reason
                    sell_event_row["trailing_status"] = str(trade_record.get("trailing_status") or get_execution_trailing_status(sell_event_row, trade_record))
                    if execution_mode == "LIVE_ORDER":
                        broker_entry = live_state["broker_positions"].get(closed_order_event_id, {})
                        broker_entry = sync_broker_position_state(kite, broker_entry) if broker_entry else {}
                        if broker_entry:
                            live_state["broker_positions"][closed_order_event_id] = broker_entry
                        if broker_entry and broker_entry.get("manual_or_broker_exit_detected"):
                            continue
                        if not broker_entry:
                            continue
                        order_meta = safe_place_live_exit_order(
                            kite,
                            str(trade["side"]),
                            live_state.get("current_ce_symbol", ce_symbol),
                            live_state.get("current_pe_symbol", pe_symbol),
                            int(trade["qty"]),
                            broker_entry=broker_entry,
                        )
                        if order_meta.get("skipped"):
                            skip_reason = str(order_meta.get("skip_reason", ""))
                            if skip_reason == "NO_POSITION_TO_EXIT":
                                live_state["broker_positions"].pop(closed_order_event_id, None)
                                register_symbol_exit_cooldown(live_state, broker_entry.get("option_symbol", ""), datetime.now(IST))
                            continue
                        exit_snapshot = wait_for_order_terminal(kite, order_meta["exit_order_id"])
                        exit_status = normalize_order_status(exit_snapshot.get("status"))
                        if exit_status != "COMPLETE":
                            broker_entry["exit_in_progress"] = False
                            raise RuntimeError(f"Exit order not completed. order_id={order_meta['exit_order_id']} status={exit_status} message={exit_snapshot.get('status_message') or exit_snapshot.get('status_message_raw') or ''}")
                        actual_exit_price = extract_avg_price(exit_snapshot)
                        actual_exit_time = extract_order_time(exit_snapshot)
                        actual_entry_price = safe_float(broker_entry.get("entry_price_actual"), safe_float(trade_record.get("entry_price")))
                        actual_qty = safe_int(broker_entry.get("qty") or trade_record.get("qty"), safe_int(trade_record.get("qty"), QTY))
                        trade_record.update({
                            "entry_price_strategy": safe_float(broker_entry.get("entry_price_theoretical"), safe_float(trade_record.get("entry_price"))),
                            "exit_price_strategy": safe_float(trade_record.get("exit_price")),
                            "entry_time": broker_entry.get("entry_broker_time") or trade_record.get("entry_time"),
                            "entry_price": actual_entry_price,
                            "exit_price": actual_exit_price,
                            "exit_time": actual_exit_time,
                            "qty": actual_qty,
                            "pnl_points": actual_exit_price - actual_entry_price,
                            "entry_order_id": str(broker_entry.get("entry_order_id", "")),
                            "entry_order_status": str(broker_entry.get("entry_order_status", "COMPLETE")),
                            "entry_broker_time": broker_entry.get("entry_broker_time"),
                            "entry_order_message": str(broker_entry.get("entry_order_message", "")),
                            "exit_order_id": str(order_meta.get("exit_order_id", "")),
                            "exit_order_status": exit_status,
                            "exit_broker_time": actual_exit_time,
                            "exit_order_message": str(exit_snapshot.get("status_message") or exit_snapshot.get("status_message_raw") or ""),
                            "slippage_entry_points": actual_entry_price - safe_float(broker_entry.get("entry_price_theoretical"), safe_float(trade_record.get("entry_price"))),
                            "slippage_exit_points": actual_exit_price - safe_float(trade_record.get("exit_price")),
                            "emergency_sl_pct_used": safe_float(broker_entry.get("emergency_sl_pct_used"), 0.0),
                            "target_profit_points": safe_float(broker_entry.get("target_profit_points"), np.nan),
                        })
                        trade_record["slippage_total_points"] = safe_float(trade_record["slippage_entry_points"]) + safe_float(trade_record["slippage_exit_points"])
                        trade_record["pnl_rs"] = safe_float(trade_record["pnl_points"]) * actual_qty
                        live_state["realized_live_pnl_rs"] = safe_float(live_state.get("realized_live_pnl_rs", 0.0)) + safe_float(trade_record["pnl_rs"])
                        trade_record["realized_mtm_after_trade"] = live_state["realized_live_pnl_rs"]
                        trade_record["trailing_status"] = get_execution_trailing_status(sell_event_row, trade_record)
                        broker_entry.update(order_meta)
                        broker_entry.update({
                            "broker_exit_order_id": str(order_meta.get("exit_order_id", "")),
                            "broker_exit_avg_price": actual_exit_price,
                            "broker_exit_time": actual_exit_time,
                            "broker_exit_reason": normalize_exit_reason(str(trade_record.get("exit_reason", "STRATEGY_EXIT"))),
                            "manual_or_broker_exit_detected": True,
                            "broker_open_qty": 0,
                            "exit_in_progress": False,
                        })
                        live_state["broker_positions"].pop(closed_order_event_id, None)
                        register_symbol_exit_cooldown(live_state, broker_entry.get("option_symbol", ""), actual_exit_time, actual_exit_price)
                        sell_event_row.update(order_meta)
                        sell_event_row["option_price"] = actual_exit_price
                        sell_event_row["event_time"] = actual_exit_time
                        sell_event_row["realized_mtm"] = live_state["realized_live_pnl_rs"]
                    print_execution_sell_summary(sell_event_row, trade_record)
                    print(f"\n========== {execution_mode} SELL ==========")
                    print(pd.DataFrame([sell_event_row]).to_string(index=False))
                    live_state["live_events"].append(sell_event_row)
                    live_state["printed_sell_event_ids"].add(sell_event_id)

                    # Unlock BUY gating when the active order is sold.
                    if (
                        live_state["active_order_event_id"] == closed_order_event_id
                        or execution_mode == "PAPERTRADE_LIVE"
                    ):
                        live_state["active_order_event_id"] = None
                        if execution_mode == "PAPERTRADE_LIVE":
                            live_state["paper_open_position"] = None
                    live_state.get("paper_entry_prices", {}).pop(closed_order_event_id, None)
                    register_symbol_exit_cooldown(
                        live_state,
                        trade_record.get("option_symbol", ""),
                        trade_record.get("exit_time") or sell_event_row.get("event_time"),
                        trade_record.get("exit_price"),
                    )
                    live_state["last_live_exit_time"] = trade_record.get("exit_time") or sell_event_row.get("event_time")
                    live_state["cooldown_block_logged_for"] = None
                    live_state["locked_trade_strike_active"] = False
                    live_state["locked_ce_symbol"] = ""
                    live_state["locked_pe_symbol"] = ""
                    live_state["locked_ce_token"] = 0
                    live_state["locked_pe_token"] = 0
                    live_state["last_symbol_lock_skip_reason"] = ""
                    try:
                        refresh_entry_symbols(reason="POST_EXIT")
                    except Exception:
                        pass
                    new_events_added = True

                print(f"\n========== NEW {execution_mode} TRADE(S) ==========")
                trade_df = pd.DataFrame([trade_record])
                print(trade_df.to_string(index=False))
                live_state["live_trade_events"].append(trade_record)
                live_state["printed_trade_ids"].add(trade_id)
                new_events_added = True

        # A paper rebuild can contain the prior SELL and a new open position
        # in the same callback. Re-check the new position after the SELL has
        # released active_order_event_id so the second BUY is not lost.
        if (
            pending_buy is None
            and open_position is not None
            and execution_mode != "LIVE_ORDER"
            and live_state.get("active_order_event_id") is None
            and current_open_event_id not in live_state["printed_buy_event_ids"]
        ):
            live_state["active_order_event_id"] = current_open_event_id
            pending_buy = open_position
            log_entry_diag("PENDING_BUY_CREATED_AFTER_CLOSE", pending_buy)

        # A funds rejection is terminal for this exact signal. Without this
        # guard, each tick rebuilds the same pending BUY and resubmits it.
        if pending_buy is not None:
            pending_event_id = make_entry_event_id(
                pending_buy.get("side"),
                pending_buy.get("entry_time"),
                pending_buy.get("option_symbol"),
            )
            if pending_event_id in live_state["insufficient_funds_event_ids"]:
                log_entry_diag(
                    "BLOCK_INSUFFICIENT_FUNDS_RETRY",
                    pending_buy,
                    extra="same signal was already rejected by broker",
                )
                pending_buy = None

        # Hard execution-level cap: once completed execution trades hit MAX_TRADES,
        # do not allow any fresh BUY to be placed from pending state.
        if pending_buy is not None and max_trades_reached_in_execution():
            log_entry_diag(
                "BLOCK_MAX_TRADES_REACHED",
                pending_buy,
                extra=f"entries={get_execution_entry_count()} cfg={safe_int(MAX_TRADES, 0)}",
            )
            pending_buy = None
            # In LIVE_ORDER, do not write MAX_TRADES into strategy attrs; live stop is decided from completed execution trades only.
            if execution_mode != "LIVE_ORDER" and not trades.attrs.get("guard_stop_reason"):
                trades.attrs["guard_stop_reason"] = "MAX_TRADES_REACHED"
            if not live_state.get("max_trades_block_logged"):
                print(
                    f"[MAX_TRADES BLOCK] Fresh entry skipped because session entry count "
                    f"{get_execution_entry_count()} reached MAX_TRADES={safe_int(MAX_TRADES, 0)}"
                )
                live_state["max_trades_block_logged"] = True

        # --- Now print deferred BUY (after closed trades, so chronological order is preserved) ---
        if pending_buy is not None and execution_mode == "LIVE_ORDER":
            current_open_positions = get_open_local_position_count(live_state)
            if current_open_positions >= safe_int(MAX_CONCURRENT_TRADES, 1):
                pending_buy = None
                if not live_state.get("max_trades_block_logged"):
                    print(
                        f"[MAX CONCURRENT BLOCK] Fresh entry skipped because open position count reached "
                        f"MAX_CONCURRENT_TRADES={safe_int(MAX_CONCURRENT_TRADES, 1)}"
                    )
                    live_state["max_trades_block_logged"] = True

        if pending_buy is not None:
            live_signal_now = live_state.get("last_tick_exchange_ts") or datetime.now(IST)
            if execution_mode in {"LIVE_ORDER", "PAPERTRADE_LIVE"} and is_signal_too_old(
                pending_buy.get("entry_time"), live_signal_now
            ):
                stale_event_id = make_entry_event_id(
                    pending_buy.get("side"), pending_buy.get("entry_time"), pending_buy.get("option_symbol")
                )
                if stale_event_id not in live_state["stale_live_signal_event_ids"]:
                    log_entry_diag("BLOCK_STALE_LIVE_SIGNAL", pending_buy)
                    print(
                        f"[{execution_mode} ENTRY BLOCK] Stale signal skipped: symbol={pending_buy.get('option_symbol')} "
                        f"signal_time={pending_buy.get('entry_time')} max_age={safe_int(MAX_ENTRY_SIGNAL_AGE_SECONDS, 300)}s"
                    )
                    live_state["stale_live_signal_event_ids"].add(stale_event_id)
                pending_buy = None

        if pending_buy is not None:
            gate_reason = get_new_entry_block_reason()
            if gate_reason:
                log_entry_diag("BLOCK_TIME_WINDOW", pending_buy, extra=gate_reason)
                if live_state.get("last_entry_block_reason") != gate_reason:
                    print(f"[ENTRY BLOCK] Fresh entry skipped due to {gate_reason}")
                    live_state["last_entry_block_reason"] = gate_reason
                pending_buy = None

        if pending_buy is not None:
            blocked, block_reason, _ = should_block_entry_reentry(live_state, pending_buy)
            if blocked:
                log_entry_diag("BLOCK_SAME_SYMBOL_REENTRY", pending_buy, extra=block_reason)
                reentry_block_key = (
                    str(pending_buy.get("option_symbol") or "").upper(),
                    str(block_reason),
                    f"{safe_float(pending_buy.get('entry_price'), 0.0):.2f}",
                )
                if live_state.get("last_reentry_block_key") != reentry_block_key:
                    print(f"[ENTRY RE-ENTRY BLOCK] {pending_buy['option_symbol']} skipped: {block_reason}")
                    live_state["last_reentry_block_key"] = reentry_block_key
                pending_buy = None

        if pending_buy is not None:
            if execution_mode == "LIVE_ORDER":
                current_open_positions = get_open_local_position_count(live_state)
                if current_open_positions >= safe_int(MAX_CONCURRENT_TRADES, 1):
                    log_entry_diag(
                        "BLOCK_MAX_CONCURRENT",
                        pending_buy,
                        extra=f"open={current_open_positions} cfg={safe_int(MAX_CONCURRENT_TRADES, 1)}",
                    )
                    # Skip PRE_ENTRY symbol refresh/noise when an existing open trade already uses capacity.
                    emit_open_position_pnl_update(trades)
                    detect_tick_stall()
                    return
            # The pending signal was generated from the current symbol pair. Do not
            # refresh symbols here in any mode: that could replace the pair before
            # the position is recorded and make later rebuilds price another strike.
            log_entry_diag("ENTRY_PREPARED", pending_buy)
            event_row = format_live_event_row(
                event_type="BUY",
                run_mode=execution_mode,
                trade_date=trade_date,
                underlying=UNDERLYING_NAME,
                side=str(pending_buy["side"]),
                option_symbol=str(pending_buy["option_symbol"]),
                event_time=pending_buy["entry_time"],
                event_reason=str(pending_buy["entry_reason"]),
                spot_price=float(pending_buy["entry_spot"]),
                option_price=float(pending_buy["entry_price"]),
                qty=int(pending_buy.get("qty", QTY)),
                realized_mtm=float(trades.attrs.get("realized_mtm_rs", 0.0) or 0.0),
                mtf_label=str(pending_buy.get("mtf_entry_label", "")),
                gap_type=str(pending_buy.get("gap_type", "")),
                gap_points=safe_float(pending_buy.get("gap_points"), np.nan),
                gap_pct=safe_float(pending_buy.get("gap_pct"), np.nan),
                cpr_type=str(pending_buy.get("cpr_type", "")),
                cpr_width_pct=safe_float(pending_buy.get("cpr_width_pct"), np.nan),
                emergency_sl_pct_used=safe_float(pending_buy.get("emergency_sl_pct_used"), 0.0),
                emergency_sl_price=safe_float(pending_buy.get("emergency_sl_price"), np.nan),
                target_profit_points=safe_float(
                    pending_buy.get("target_profit_points"),
                    float(TRAILING_PROFIT_STAGE1[0][0]) if TRAILING_PROFIT_STAGE1 else 50.0,
                ),
                trailing_status=str(pending_buy.get("trailing_status") or get_execution_buy_trailing_status(pending_buy.get("entry_time"))),
                gift_nifty=get_gift_nifty_snapshot(kite, spot_price=safe_float(pending_buy.get("entry_spot"), 0.0)),
            )
            if execution_mode == "LIVE_ORDER":
                sync_broker_positions_if_needed(force=True)

                current_ts_for_entry = datetime.now(IST)
                last_exit_time = live_state.get("last_live_exit_time")
                if is_live_reentry_cooldown_active(last_exit_time, current_ts_for_entry):
                    log_entry_diag("BLOCK_LIVE_REENTRY_COOLDOWN", pending_buy, extra=f"last_exit={last_exit_time}")
                    block_key = f"{current_open_event_id}|{last_exit_time}"
                    if live_state.get("cooldown_block_logged_for") != block_key:
                        cooldown_until = pd.Timestamp(last_exit_time) + timedelta(minutes=safe_int(REENTRY_COOLDOWN_MINUTES, 0))
                        print(
                            f"[LIVE ENTRY COOLDOWN] Fresh entry skipped for {pending_buy['option_symbol']} because "
                            f"re-entry cooldown of {safe_int(REENTRY_COOLDOWN_MINUTES, 0)} minute(s) is still active "
                            f"after exit at {last_exit_time}. Cooldown until {cooldown_until}"
                        )
                        live_state["cooldown_block_logged_for"] = block_key
                        emit_idle_status(f"Cooldown active for {pending_buy['option_symbol']}")
                    return
                live_state["cooldown_block_logged_for"] = None

                entry_ce_symbol = str(live_state.get("current_ce_symbol") or ce_symbol)
                entry_pe_symbol = str(live_state.get("current_pe_symbol") or pe_symbol)

                # Prefer the strategy-generated pending symbol when available
                try:
                    pb_sym = str(pending_buy.get("option_symbol") or "")
                    if pb_sym:
                        if str(pending_buy.get("side") or "").upper() == "CE":
                            entry_ce_symbol = pb_sym
                        else:
                            entry_pe_symbol = pb_sym
                except Exception:
                    pass

                current_open_positions = get_open_local_position_count(live_state)
                if current_open_positions >= safe_int(MAX_CONCURRENT_TRADES, 1):
                    log_entry_diag(
                        "BLOCK_MAX_CONCURRENT",
                        pending_buy,
                        extra=f"open={current_open_positions} cfg={safe_int(MAX_CONCURRENT_TRADES, 1)}",
                    )
                    if current_open_positions > 0:
                        # Keep runtime noise low while an open position is active; continue PnL visibility.
                        emit_open_position_pnl_update(trades)
                        detect_tick_stall()
                        return
                    if not live_state.get("max_trades_block_logged"):
                        print(
                            f"[MAX CONCURRENT BLOCK] Fresh entry skipped because open position count reached "
                            f"MAX_CONCURRENT_TRADES={safe_int(MAX_CONCURRENT_TRADES, 1)}"
                        )
                        live_state["max_trades_block_logged"] = True
                                    # Only emit the idle status once when the block is first detected
                        emit_idle_status("Max concurrent open-position cap reached")
                    return
                live_state["max_trades_block_logged"] = False

                side_counts = get_open_positions_by_side(live_state)
                pending_side = str(pending_buy["side"]).upper()
                option_symbol_for_block = entry_ce_symbol if pending_side == "CE" else entry_pe_symbol
                if is_same_symbol_reentry_blocked(live_state, option_symbol_for_block, current_ts_for_entry):
                    log_entry_diag("BLOCK_SAME_SYMBOL_COOLDOWN", pending_buy, extra=option_symbol_for_block)
                    print(f"[SAME SYMBOL BLOCK] Fresh entry skipped for {option_symbol_for_block} because symbol re-entry cooldown is active")
                    emit_idle_status(f"Same-symbol cooldown active for {option_symbol_for_block}")
                    return
                if pending_side == "CE" and side_counts.get("CE", 0) >= safe_int(MAX_CONCURRENT_TRADES_CE, 1):
                    log_entry_diag(
                        "BLOCK_PER_SIDE_CE",
                        pending_buy,
                        extra=f"open_ce={side_counts.get('CE', 0)} cfg={safe_int(MAX_CONCURRENT_TRADES_CE, 1)}",
                    )
                    print(
                        f"[PER-SIDE BLOCK] Fresh entry skipped because CE open position count reached "
                        f"MAX_CONCURRENT_TRADES_CE={safe_int(MAX_CONCURRENT_TRADES_CE, 1)}"
                    )
                    emit_idle_status("CE per-side concurrent cap reached")
                    return
                if pending_side == "PE" and side_counts.get("PE", 0) >= safe_int(MAX_CONCURRENT_TRADES_PE, 1):
                    log_entry_diag(
                        "BLOCK_PER_SIDE_PE",
                        pending_buy,
                        extra=f"open_pe={side_counts.get('PE', 0)} cfg={safe_int(MAX_CONCURRENT_TRADES_PE, 1)}",
                    )
                    print(
                        f"[PER-SIDE BLOCK] Fresh entry skipped because PE open position count reached "
                        f"MAX_CONCURRENT_TRADES_PE={safe_int(MAX_CONCURRENT_TRADES_PE, 1)}"
                    )
                    emit_idle_status("PE per-side concurrent cap reached")
                    return

                live_state["live_entry_in_progress_event_id"] = current_open_event_id
                if safe_int(ENTRY_CONFIRM_DELAY_SECONDS, 0) > 0:
                    time.sleep(safe_int(ENTRY_CONFIRM_DELAY_SECONDS, 0))
                try:
                    order_meta = place_live_entry_order(
                        kite,
                        str(pending_buy["side"]),
                        entry_ce_symbol,
                        entry_pe_symbol,
                        int(pending_buy.get("qty", QTY)),
                        entry_price_hint=float(pending_buy["entry_price"]),
                        entry_number=get_execution_entry_count() + 1,
                    )
                except Exception as exc:
                    if not is_insufficient_funds_error(exc):
                        raise
                    live_state["insufficient_funds_event_ids"].add(current_open_event_id)
                    print(
                        f"[LIVE ENTRY REJECTED] Insufficient funds for "
                        f"{pending_buy.get('option_symbol', entry_ce_symbol if pending_side == 'CE' else entry_pe_symbol)}; "
                        f"stopping live processing: {exc}"
                    )
                    live_state["stop"] = True
                    try:
                        threading.Thread(
                            target=lambda: run_shutdown_cleanup("INSUFFICIENT_FUNDS"),
                            daemon=True,
                            name="insufficient-funds-shutdown",
                        ).start()
                        safe_stop_ws(live_state.get("ticker_ws"))
                    except Exception as shutdown_exc:
                        print(f"[SHUTDOWN WARNING] Unable to start insufficient-funds cleanup: {shutdown_exc}")
                        safe_stop_ws(live_state.get("ticker_ws"))
                    return
                entry_snapshot = wait_for_order_terminal(kite, order_meta["entry_order_id"])
                entry_status = normalize_order_status(entry_snapshot.get("status"))
                if entry_status not in {"COMPLETE", "REJECTED", "CANCELLED"}:
                    # Never leave an unfilled entry (e.g. a LIMIT fallback) working at the
                    # broker once we stop tracking it: a late fill would have no SL.
                    print(f"[LIVE ENTRY UNFILLED] order_id={order_meta['entry_order_id']} status={entry_status}; cancelling")
                    cancel_order_and_wait(kite, order_meta["entry_order_id"])
                    entry_snapshot = get_order_snapshot(kite, order_meta["entry_order_id"]) or entry_snapshot
                    entry_status = normalize_order_status(entry_snapshot.get("status"))
                if entry_status != "COMPLETE":
                    partial_qty = safe_int(entry_snapshot.get("filled_quantity"), 0)
                    if partial_qty > 0:
                        partial_symbol = str(entry_snapshot.get("tradingsymbol") or order_meta.get("entry_order_symbol") or "")
                        print(f"[LIVE ENTRY PARTIAL] {partial_symbol} filled {partial_qty} before cancel; flattening")
                        try:
                            exit_meta = safe_place_live_exit_order(kite, str(pending_buy["side"]), entry_ce_symbol, entry_pe_symbol, partial_qty, broker_entry={"option_symbol": partial_symbol, "qty": partial_qty})
                            if not exit_meta.get("skipped"):
                                wait_for_order_terminal(kite, exit_meta["exit_order_id"])
                        except Exception as flatten_exc:
                            print(f"[LIVE ENTRY PARTIAL WARNING] Could not flatten {partial_symbol}: {flatten_exc}. Check the broker terminal.")
                    raise RuntimeError(f"Entry order not completed. order_id={order_meta['entry_order_id']} status={entry_status} message={entry_snapshot.get('status_message') or entry_snapshot.get('status_message_raw') or ''}")
                actual_entry_price = extract_avg_price(entry_snapshot)
                actual_entry_time = extract_order_time(entry_snapshot)
                option_symbol = str(entry_snapshot.get("tradingsymbol") or (entry_ce_symbol if str(pending_buy["side"]) == "CE" else entry_pe_symbol))
                if should_skip_entry_for_slippage(float(pending_buy["entry_price"]), actual_entry_price):
                    print(f"[SMART ENTRY EXIT] Immediate protective exit because fill slippage is too high for {option_symbol}: signal={float(pending_buy['entry_price']):.2f} fill={actual_entry_price:.2f}")
                    exit_meta = safe_place_live_exit_order(kite, str(pending_buy["side"]), entry_ce_symbol, entry_pe_symbol, int(pending_buy.get("qty", QTY)), broker_entry={"option_symbol": option_symbol, "qty": int(pending_buy.get("qty", QTY))})
                    if not exit_meta.get("skipped"):
                        wait_for_order_terminal(kite, exit_meta["exit_order_id"])
                    register_symbol_exit_cooldown(live_state, option_symbol, actual_entry_time, actual_entry_price)
                    live_state["live_entry_in_progress_event_id"] = None
                    return
                if order_meta.get("cover_order_used"):
                    sl_meta = {
                        "emergency_sl_order_id": "",
                        "emergency_sl_price": np.nan,
                        "emergency_sl_trigger_price": safe_float(order_meta.get("cover_order_trigger_price"), 0.0),
                        "emergency_sl_pct_used": safe_float(order_meta.get("emergency_sl_pct_used"), 0.0),
                    }
                else:
                    # A rejected protective order must not orphan the already-filled position.
                    try:
                        sl_meta = place_emergency_sl_order(
                            kite,
                            option_symbol,
                            int(pending_buy.get("qty", QTY)),
                            actual_entry_price,
                            entry_number=get_execution_entry_count() + 1,
                        )
                    except Exception as sl_exc:
                        print(f"[EMERGENCY SL WARNING] Could not place SL order for {option_symbol}: {sl_exc}. Position stays tracked with software stop.")
                        sl_points_fb = get_dynamic_emergency_sl_points(actual_entry_price, get_execution_entry_count() + 1)
                        sl_meta = {
                            "emergency_sl_order_id": "",
                            "emergency_sl_price": round(max(0.05, actual_entry_price - sl_points_fb), 1),
                            "emergency_sl_trigger_price": round(max(0.05, actual_entry_price - sl_points_fb), 1),
                            "emergency_sl_pct_used": sl_points_fb,
                        }
                order_meta.update({
                    "entry_order_status": entry_status,
                    "entry_broker_time": actual_entry_time,
                    "entry_order_message": str(entry_snapshot.get("status_message") or entry_snapshot.get("status_message_raw") or ""),
                    "entry_price_actual": actual_entry_price,
                })
                order_meta.update(sl_meta)
                event_row.update(order_meta)
                event_row["option_price"] = actual_entry_price
                event_row["event_time"] = actual_entry_time
                actual_entry_time_resolved = actual_entry_time or pending_buy["entry_time"]
                live_state["broker_positions"][current_open_event_id] = {
                    "side": str(pending_buy["side"]),
                    "option_symbol": option_symbol,
                    "qty": int(pending_buy.get("qty", QTY)),
                    "entry_reason": str(pending_buy["entry_reason"]),
                    "entry_time": actual_entry_time_resolved,
                    "entry_spot": float(pending_buy["entry_spot"]),
                    "signal_time": pending_buy["entry_time"],
                    "signal_spot_price": float(pending_buy["entry_spot"]),
                    "signal_option_price": float(pending_buy["entry_price"]),
                    "entry_price_theoretical": float(pending_buy["entry_price"]),
                    "entry_price_actual": actual_entry_price,
                    "broker_open_qty": int(pending_buy.get("qty", QTY)),
                    "mtf_entry_label": str(pending_buy.get("mtf_entry_label", "")),
                    "emergency_sl_pct_used": safe_float(order_meta.get("emergency_sl_pct_used"), 0.0),
                    "exit_in_progress": False,
                    "best_price": actual_entry_price,
                    "trailing_stop_active": False,
                    "trailing_stop_price": safe_float(order_meta.get("emergency_sl_price"), 0.0),
                    "target_profit_points": float(TRAILING_PROFIT_STAGE1[0][0]) if TRAILING_PROFIT_STAGE1 else 50.0,
                    **order_meta,
                }
            else:
                if execution_mode == "PAPERTRADE_LIVE":
                    # Keep paper fills anchored to the signal candle. Using
                    # the mutable websocket LTP can record a later tick as
                    # the entry price.
                    signal_entry_price = float(pending_buy["entry_price"])
                    simulated_entry_price = signal_entry_price
                    event_row["signal_option_price"] = signal_entry_price
                    event_row["entry_price_strategy"] = signal_entry_price
                    event_row["option_price"] = simulated_entry_price
                elif SIMULATE_LIVE_EXECUTION_IN_PAPERTRADE:
                    signal_entry_price = float(pending_buy["entry_price"])
                    simulated_entry_price = apply_paper_entry_slippage(signal_entry_price, side="BUY")
                    event_row["signal_option_price"] = signal_entry_price
                    event_row["entry_price_strategy"] = signal_entry_price
                    event_row["option_price"] = simulated_entry_price
                else:
                    simulated_entry_price = float(pending_buy["entry_price"])
                live_state["paper_entry_prices"][current_open_event_id] = simulated_entry_price
            print_execution_buy_summary(event_row)
            print(f"\n========== {execution_mode} BUY ==========")
            print(pd.DataFrame([event_row]).to_string(index=False))
            live_state["live_events"].append(event_row)
            if EXPORT_EXCEL:
                try:
                    excel_file = export_papertrade_events_to_excel(
                        trade_date=trade_date,
                        ce_symbol=ce_symbol,
                        pe_symbol=pe_symbol,
                        live_events=live_state["live_events"],
                        live_trade_events=live_state["live_trade_events"],
                        guard_stop_reason=get_execution_stop_reason(trades),
                        execution_mode=execution_mode,
                        reuse_path=live_state["excel_file_path"],
                        debug_day=live_state.get("last_day_debug_df"),
                    )
                    live_state["excel_file_path"] = excel_file
                    print(f"[Excel updated after BUY] {excel_file}")
                except Exception as _exc:
                    print(f"[Excel write error after BUY] {_exc}")
            live_state["printed_buy_event_ids"].add(current_open_event_id)
            live_state["live_entry_in_progress_event_id"] = None
            live_state["execution_entry_count"] = get_execution_entry_count() + 1
            log_entry_diag(
                "ENTRY_COUNT_INCREMENTED",
                pending_buy,
                extra=f"entries={get_execution_entry_count()} cfg={safe_int(MAX_TRADES, 0)}",
            )
            if execution_mode == "PAPERTRADE_LIVE":
                lock_entry_symbols_to_first_trade_strike(
                    str(event_row.get("option_symbol") or pending_buy.get("option_symbol") or ""),
                    reason="OPEN_POSITION",
                )
            log_entry_diag("BUY_EMITTED", pending_buy)
            new_events_added = True

        if pending_buy is None and not new_events_added:
            emit_open_position_pnl_update(trades)
            detect_tick_stall()
            # Keep idle status quiet while a trade is actively open.
            has_open_trade = bool(open_position) or (get_open_local_position_count(live_state) > 0)
            if not has_open_trade:
                emit_idle_status("Engine running. Waiting for next valid signal / cooldown expiry")

        # Write Excel immediately whenever a new BUY, SELL or trade is detected
        if EXPORT_EXCEL and new_events_added:
            try:
                excel_file = export_papertrade_events_to_excel(
                    trade_date=trade_date,
                    ce_symbol=ce_symbol,
                    pe_symbol=pe_symbol,
                    live_events=live_state["live_events"],
                    live_trade_events=live_state["live_trade_events"],
                    guard_stop_reason=get_execution_stop_reason(trades),
                    execution_mode=execution_mode,
                    reuse_path=live_state["excel_file_path"],
                    debug_day=live_state.get("last_day_debug_df"),
                )
                live_state["excel_file_path"] = excel_file
                print(f"[Excel updated] {excel_file}")
                # Force an immediate open-position PnL print after a new trade is recorded.
                try:
                    emit_open_position_pnl_update(trades)
                except Exception:
                    pass
            except Exception as _exc:
                print(f"[Excel write error] {_exc}")

        stop_reason = get_execution_stop_reason(trades)
        if stop_reason and live_state.get("last_stop_reason_logged") != stop_reason:
            print(f"\nTrading stopped by: {stop_reason}")
            live_state["last_stop_reason_logged"] = stop_reason
            live_state["stop"] = True
        elif stop_reason:
            live_state["stop"] = True

    def finalize_completed_minutes(force_all: bool = False):
        now_local = datetime.now(IST)
        current_minute = floor_to_minute(now_local)
        completed_before = current_minute if force_all else current_minute - timedelta(minutes=1)
        for token, key in token_to_key.items():
            state = live_state["current"].get(token)
            if not state:
                continue
            minute_ts = state["minute"]
            if minute_ts <= completed_before:
                append_completed_row(key, minute_ts, state)
                del live_state["current"][token]
        if live_state["spot_rows"] and live_state["ce_rows"] and live_state["pe_rows"]:
            rebuild_and_print()

    def refresh_stale_open_position_ltp() -> None:
        """Safety net for a single option leg's tick stream silently stalling.

        The global tick-stall detector only tracks the time of the last tick
        of *any* instrument, so it stays quiet while the spot leg keeps
        ticking even if the currently open position's own option symbol has
        stopped producing ticks (thin liquidity, a missed packet, etc.).
        Left unchecked, both the day-driven simulation and the tick-driven
        sync_paper_position_from_tick() path keep evaluating SL/trailing/
        target against a stale, frozen price and can silently miss real exits
        (confirmed against live trades that exited correctly via broker-side
        orders on the same signal).
        """
        if execution_mode != "PAPERTRADE_LIVE":
            return
        final_trades = live_state.get("final_trades")
        if not isinstance(final_trades, pd.DataFrame):
            return
        open_position = final_trades.attrs.get("open_position")
        if not open_position:
            return
        token = safe_int(open_position.get("option_token"), 0)
        option_symbol = str(open_position.get("option_symbol") or "")
        if token <= 0 or not option_symbol:
            return

        now_local = datetime.now(IST)
        last_tick_ts = live_state.get("last_token_tick_ts", {}).get(token)
        stale_seconds = (now_local - last_tick_ts).total_seconds() if last_tick_ts is not None else float("inf")
        if stale_seconds < safe_int(OPEN_POSITION_LTP_STALE_SECONDS, 20):
            return

        last_refresh_ts = live_state.get("last_open_position_ltp_refresh_ts")
        cooldown = max(1, safe_int(OPEN_POSITION_LTP_REFRESH_COOLDOWN_SECONDS, 10))
        if last_refresh_ts is not None and (now_local - last_refresh_ts).total_seconds() < cooldown:
            return
        live_state["last_open_position_ltp_refresh_ts"] = now_local

        try:
            fresh_ltp = get_option_symbol_ltp(kite, option_symbol)
        except Exception:
            fresh_ltp = 0.0
        if fresh_ltp <= 0:
            return

        stale_age_text = (
            "unknown"
            if stale_seconds == float("inf") or stale_seconds != stale_seconds
            else str(int(stale_seconds))
        )
        print(
            f"[STALE OPTION PRICE] symbol={option_symbol} no tick for {stale_age_text}s; "
            f"refreshed via REST ltp={fresh_ltp:.2f}"
        )
        minute_ts = floor_to_minute(now_local)
        state = live_state["current"].get(token)
        if state is None or state["minute"] != minute_ts:
            if state is not None:
                key = token_to_key.get(token)
                if key:
                    append_completed_row(key, state["minute"], state)
            live_state["current"][token] = {
                "minute": minute_ts,
                "open": fresh_ltp,
                "high": fresh_ltp,
                "low": fresh_ltp,
                "close": fresh_ltp,
                "volume": 0.0,
            }
        else:
            state["high"] = max(state["high"], fresh_ltp)
            state["low"] = min(state["low"], fresh_ltp)
            state["close"] = fresh_ltp
        live_state["last_token_tick_ts"][token] = now_local
        sync_paper_position_from_tick()

    def sync_paper_position_from_tick():
        if execution_mode != "PAPERTRADE_LIVE" or live_state.get("paper_exit_pending"):
            return
        final_trades = live_state.get("final_trades")
        if not isinstance(final_trades, pd.DataFrame):
            return
        open_position = final_trades.attrs.get("open_position")
        if not open_position:
            return

        side = str(open_position.get("side") or "").upper()
        option_ltp = get_option_ltp_from_live_state(
            live_state,
            side,
            safe_int(
                open_position.get("option_token"),
                safe_int(live_state.get("locked_pe_token" if side == "PE" else "locked_ce_token"), 0),
            ),
        )
        if option_ltp <= 0 and open_position.get("option_symbol"):
            try:
                option_ltp = get_option_symbol_ltp(kite, str(open_position["option_symbol"]))
            except Exception:
                option_ltp = 0.0
        entry_price = safe_float(open_position.get("entry_price"), 0.0)
        if side not in {"CE", "PE"} or option_ltp <= 0 or entry_price <= 0:
            return

        persisted_open_position = live_state.get("paper_open_position") or {}
        previous_best = max(
            safe_float(open_position.get("best_price"), entry_price),
            safe_float(open_position.get("high_during_trade"), entry_price),
            safe_float(persisted_open_position.get("best_price"), entry_price),
            safe_float(persisted_open_position.get("high_during_trade"), entry_price),
        )
        best_price = max(previous_best, option_ltp)
        # Use peak profit (since entry), not current/instantaneous profit, to
        # decide trailing-stop stage activation — matching the day-driven
        # run_single_day_test() loop's pnl_points_high semantics. Using the
        # current price here let the stop silently fail to ratchet up (or
        # even un-trigger) on a pullback after a stage was already earned,
        # which is a genuine bug: the day-driven path and live broker orders
        # both lock in stages based on the peak, not the latest tick.
        current_time = live_state.get("last_tick_exchange_ts") or datetime.now(IST)
        stage2_peak = update_stage2_peak(
            max(
                safe_float(open_position.get("best_price_stage2"), 0.0),
                safe_float(persisted_open_position.get("best_price_stage2"), 0.0),
            ),
            option_ltp,
            current_time,
        )
        pnl_points_high = get_trailing_peak_points(entry_price, best_price, stage2_peak, current_time)
        current_stop = safe_float(
            open_position.get("trailing_stop_price"),
            0.0,
        )
        current_stop = max(
            current_stop,
            safe_float(persisted_open_position.get("trailing_stop_price"), 0.0),
            safe_float(open_position.get("emergency_sl_price"), 0.0),
            safe_float(persisted_open_position.get("emergency_sl_price"), 0.0),
        )
        stage1_triggered = current_stop > safe_float(open_position.get("emergency_sl_price"), 0.0)
        trailing_stop = get_trailing_stop_for_profit(
            entry_price,
            pnl_points_high,
            current_stop,
            current_time=current_time,
            stage1_triggered=stage1_triggered,
        )
        try:
            active_stages = get_active_trailing_stages(
                current_time=current_time,
                stage1_triggered=stage1_triggered,
            )
            min_trigger = float(active_stages[0][0]) if active_stages else None
        except Exception:
            min_trigger = None
        if min_trigger is not None and pnl_points_high < min_trigger and trailing_stop > option_ltp:
            trailing_stop = current_stop

        open_position.update({
            "best_price": round(best_price, 2),
            "high_during_trade": round(best_price, 2),
            "best_price_stage2": round(stage2_peak, 2) if stage2_peak is not None else None,
            "trailing_stop_price": safe_float(trailing_stop, 0.0),
            "trailing_stop_active": bool(
                safe_float(trailing_stop, 0.0)
                > max(
                    safe_float(open_position.get("emergency_sl_price"), 0.0),
                    safe_float(persisted_open_position.get("emergency_sl_price"), 0.0),
                )
            ),
        })
        live_state["paper_open_position"] = dict(open_position)
        emergency_stop = safe_float(open_position.get("emergency_sl_price"), 0.0)
        # A paper position must always have a local protective stop. Older
        # incremental/restarted states may not contain emergency_sl_price, so
        # reconstruct the same fixed stop used by run_single_day_test().
        protective_stop = emergency_stop
        if protective_stop <= 0:
            protective_stop = get_entry_stop_price(entry_price)
        trailing_stop_active = bool(open_position.get("trailing_stop_active"))
        effective_stop = safe_float(trailing_stop, 0.0) if trailing_stop_active else protective_stop
        if effective_stop > 0 and option_ltp <= effective_stop:
            live_state["paper_exit_pending"] = {
                "side": side,
                "ltp": option_ltp,
                "stop": effective_stop,
                "exit_time": current_time,
                "exit_reason": "TRAILING_STOP_HIT" if trailing_stop_active else "EMERGENCY_SL_HIT",
            }
            rebuild_and_print()
        else:
            entry_time = open_position.get("entry_time")

    def on_ticks(ws, ticks):
        nonlocal no_new_entry_after_live_ts, force_exit_ts, live_tracking_start
        global MTF_INTERVAL, MTF_MODE, MTF_STRICTNESS, FIXED_SL_POINTS
        try:
            if live_state.get("stop") or live_state.get("shutdown_cleanup_done"):
                return
            runtime_config = load_editable_config()
            no_new_entry_after_live_ts = pd.Timestamp(
                f"{trade_date} {runtime_config['NO_NEW_ENTRY_AFTER']}", tz=IST
            )
            force_exit_ts = pd.Timestamp(f"{trade_date} {runtime_config['FORCE_EXIT']}", tz=IST)
            configured_tracking_start = pd.Timestamp(
                f"{trade_date} {runtime_config['TRACKING_START']}", tz=IST
            )
            gap_pct = (live_state.get("market_context") or {}).get("gap_pct")
            MTF_MODE = runtime_config["MTF_MODE"]
            if MTF_MODE == "MANUAL" or gap_pct in (None, ""):
                MTF_INTERVAL = runtime_config["MTF_INTERVAL"]
            set_mtf_interval_from_gap(gap_pct, source="live")
            MTF_STRICTNESS = runtime_config["MTF_STRICTNESS"]
            FIXED_SL_POINTS = runtime_config["FIXED_SL_POINTS"]
            live_tracking_start = max(session_start_minute, configured_tracking_start) if (
                PAPERTRADE_FRESH_COUNT_ON_RESTART and ALLOW_ENTRY_ONLY_AFTER_APP_START
            ) else configured_tracking_start
            live_tracking_start = apply_gap_entry_policy(
                gap_pct,
                live_tracking_start,
                trade_date,
                source="live",
            )
            for tick in ticks:
                token = tick["instrument_token"]
                if token not in token_to_key:
                    continue
                ltp = float(tick.get("last_price") or 0.0)
                if ltp <= 0:
                    continue
                exch_ts = tick.get("exchange_timestamp") or tick.get("last_trade_time") or datetime.now(IST)
                update_last_tick_ts(exch_ts)
                live_state["last_token_tick_ts"][token] = datetime.now(IST)
                ts = normalize_tick_ts_to_ist(exch_ts)
                minute_ts = floor_to_minute(ts)
                state = live_state["current"].get(token)
                if state is None or state["minute"] != minute_ts:
                    if state is not None:
                        append_completed_row(token_to_key[token], state["minute"], state)
                    live_state["current"][token] = {
                        "minute": minute_ts,
                        "open": ltp,
                        "high": ltp,
                        "low": ltp,
                        "close": ltp,
                        "volume": float(tick.get("volume_traded") or 0.0),
                    }
                else:
                    state["high"] = max(state["high"], ltp)
                    state["low"] = min(state["low"], ltp)
                    state["close"] = ltp
                    state["volume"] = float(tick.get("volume_traded") or state.get("volume", 0.0) or 0.0)

                sync_paper_position_from_tick()

            # Run once per tick batch (not per tick) so a stalled option leg is
            # checked even when only the spot leg is ticking in this batch.
            refresh_stale_open_position_ltp()

            # Keep broker/manual exit detection responsive even if strategy candle flow stalls.
            if execution_mode == "LIVE_ORDER":
                sync_broker_positions_if_needed()
                forced_exit_events_added = append_broker_forced_exit_trades()
                if EXPORT_EXCEL and forced_exit_events_added:
                    try:
                        excel_file = export_papertrade_events_to_excel(
                            trade_date=trade_date,
                            ce_symbol=ce_symbol,
                            pe_symbol=pe_symbol,
                            live_events=live_state["live_events"],
                            live_trade_events=live_state["live_trade_events"],
                            guard_stop_reason=get_execution_stop_reason(live_state.get("final_trades")),
                            execution_mode=execution_mode,
                            reuse_path=live_state["excel_file_path"],
                            debug_day=live_state.get("last_day_debug_df"),
                        )
                        live_state["excel_file_path"] = excel_file
                        print(f"[Excel updated] {excel_file}")
                    except Exception as _exc:
                        print(f"[Excel write error] {_exc}")

            detect_tick_stall()
            now_local = datetime.now(IST)
            if not live_state.get("force_exit_triggered") and now_local >= force_exit_ts:
                live_state["force_exit_triggered"] = True
                print(
                    f"[FORCE EXIT TRIGGER] now={now_local.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                    f">= configured_force_exit={force_exit_ts.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                )
                if execution_mode == "LIVE_ORDER":
                    force_exit_live_order_positions(exit_reason="FORCE_EXIT")
                    sync_broker_positions_if_needed(force=True)
                    append_broker_forced_exit_trades()
                elif execution_mode == "PAPERTRADE_LIVE":
                    run_shutdown_cleanup("FORCE_EXIT")
                    safe_stop_ws(live_state.get("ticker_ws"))
                    return
                live_state["stop"] = True
                finalize_completed_minutes(force_all=True)
            else:
                finalize_completed_minutes(force_all=False)
            if now_local.time() >= dtime(15, 30) or live_state["stop"]:
                finalize_completed_minutes(force_all=True)
                safe_stop_ws(live_state.get("ticker_ws"))
        except PermissionException as exc:
            live_state["last_exception"] = exc
            print("Live processing halted: Kite API permission error while placing order.")
            print("Action required: add your current public IP in Kite developer console allowlist and retry.")
            safe_stop_ws(live_state.get("ticker_ws"))
        except Exception as exc:
            if is_transient_kite_api_error(exc):
                print(f"[LIVE TICK WARNING] Transient Kite API/network issue during live processing; continuing without fatal shutdown: {exc}")
                return
            live_state["last_exception"] = exc
            print(f"Live processing error: {exc}")
            safe_stop_ws(live_state.get("ticker_ws"))

    def on_connect(ws, response):
        print("KiteTicker connected. Subscribing to live ticks...")
        live_state["ticker_ws"] = ws
        live_state["ws_connected"] = True
        live_state["ws_reconnect_requested"] = False
        live_state["last_tick_stall_logged_ts"] = None
        live_state["last_tick_resubscribe_attempt_ts"] = None
        live_state["last_tick_reconnect_attempt_ts"] = None
        tokens = [UNDERLYING_TOKEN, safe_int(live_state.get("current_ce_token"), ce_token), safe_int(live_state.get("current_pe_token"), pe_token)]
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def on_close(ws, code, reason):
        live_state["ws_connected"] = False
        print(f"Connection closed: {code} - {reason}")
        # Request reconnect on unclean closes (1006 = TCP drop) unless session is stopping.
        if not live_state.get("stop") and code in (1006, None):
            now_local = datetime.now(IST)
            if now_local.time() < dtime(15, 30):
                live_state["ws_reconnect_requested"] = True

    def on_error(ws, code, reason):
        print(f"KiteTicker error. code={code}, reason={reason}")
        # Request reconnect on connection-level errors unless session is stopping.
        if not live_state.get("stop") and code in (1006, None):
            now_local = datetime.now(IST)
            if now_local.time() < dtime(15, 30):
                live_state["ws_reconnect_requested"] = True

    def watchdog_loop():
        stop_event = live_state.get("watchdog_stop_event")
        if stop_event is None:
            return
        while not stop_event.wait(5.0):
            if live_state.get("shutdown_cleanup_done"):
                continue
            try:
                if execution_mode == "PAPERTRADE_LIVE":
                    now_local = datetime.now(IST)
                    if live_state.get("stop") or now_local >= force_exit_ts or now_local.time() >= dtime(15, 30):
                        if not live_state.get("force_exit_triggered"):
                            if now_local >= force_exit_ts or now_local.time() >= dtime(15, 30):
                                live_state["force_exit_triggered"] = True
                            print(
                                f"[SESSION STOP WATCHDOG] Completing paper session shutdown at "
                                f"{now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}."
                            )
                        live_state["stop"] = True
                        safe_stop_ws(live_state.get("ticker_ws"))
                        run_shutdown_cleanup("FORCE_EXIT" if live_state.get("force_exit_triggered") else "SESSION_STOP")
                        continue
                detect_tick_stall()
                # Run the stale-option-price safety net on a fixed 5s cadence
                # too, independent of tick arrival: if the whole feed (not just
                # one leg) stalls, on_ticks never runs and the tick-driven
                # refresh never gets a chance to fire.
                refresh_stale_open_position_ltp()
                # Keep diagnostics alive during full tick freeze.
                if execution_mode != "LIVE_ORDER":
                    trades_snapshot = live_state.get("final_trades")
                    if isinstance(trades_snapshot, pd.DataFrame):
                        emit_open_position_pnl_update(trades_snapshot)
            except Exception as wd_exc:
                print(f"[WATCHDOG WARNING] {wd_exc}")

    def run_shutdown_cleanup(trigger_reason: str) -> None:
        if live_state.get("shutdown_cleanup_done"):
            return
        live_state["shutdown_cleanup_done"] = True
        print(f"\n\n========== SHUTDOWN TRIGGERED: {trigger_reason} ==========")
        print("Checking for open positions before shutdown...")
        try:
            # --- LIVE_ORDER mode: exit real broker positions ---
            if execution_mode == "LIVE_ORDER" and live_state["broker_positions"]:
                for event_id, broker_entry in list(live_state["broker_positions"].items()):
                    broker_entry = sync_broker_position_state(kite, broker_entry)
                    live_state["broker_positions"][event_id] = broker_entry
                    open_qty = safe_int(broker_entry.get("broker_open_qty"), 0)
                    if broker_entry.get("manual_or_broker_exit_detected") or open_qty <= 0:
                        print(f"  Position {event_id} already flat. Skipping.")
                        continue
                    side = broker_entry.get("side", "CE")
                    qty = open_qty
                    print(f"  Exiting open position: {side} qty={qty} ({broker_entry.get('option_symbol', '?')})")
                    try:
                        order_meta = safe_place_live_exit_order(
                            kite,
                            side,
                            live_state.get("current_ce_symbol", ce_symbol),
                            live_state.get("current_pe_symbol", pe_symbol),
                            qty,
                            broker_entry=broker_entry,
                        )
                        if order_meta.get("skipped"):
                            print(f"  Exit skipped for {event_id}: {order_meta.get('skip_reason')}")
                            continue
                        exit_snapshot = wait_for_order_terminal(kite, order_meta["exit_order_id"])
                        exit_status = normalize_order_status(exit_snapshot.get("status"))
                        actual_exit_price = extract_avg_price(exit_snapshot)
                        actual_exit_time = extract_order_time(exit_snapshot)
                        actual_entry_price = safe_float(broker_entry.get("entry_price_actual"), safe_float(broker_entry.get("entry_price_theoretical")))
                        pnl_points = actual_exit_price - actual_entry_price
                        pnl_rs = pnl_points * qty
                        live_state["realized_live_pnl_rs"] = safe_float(live_state.get("realized_live_pnl_rs", 0.0)) + pnl_rs
                        print(f"  EXIT {exit_status}: order_id={order_meta['exit_order_id']}  price={actual_exit_price}  pnl={pnl_points:.2f}pts  pnl_rs={pnl_rs:.2f}")
                        sell_event_row = format_live_event_row(
                            event_type="SELL",
                            run_mode=execution_mode,
                            trade_date=trade_date,
                            underlying=UNDERLYING_NAME,
                            side=side,
                            option_symbol=str(broker_entry.get("option_symbol", "")),
                            event_time=actual_exit_time or datetime.now(IST),
                            event_reason="MANUAL_INTERRUPT_EXIT",
                            spot_price=0.0,
                            option_price=actual_exit_price,
                            qty=qty,
                            realized_mtm=live_state["realized_live_pnl_rs"],
                            mtf_label=str(broker_entry.get("mtf_entry_label", "")),
                        )
                        sell_event_row.update(order_meta)
                        live_state["live_events"].append(sell_event_row)
                        signal_entry_price = safe_float(broker_entry.get("signal_option_price"), safe_float(broker_entry.get("entry_price_theoretical"), actual_entry_price))
                        trade_record = {
                            "run_mode": execution_mode,
                            "trade_date": trade_date,
                            "underlying": UNDERLYING_NAME,
                            "side": side,
                            "option_symbol": str(broker_entry.get("option_symbol", "")),
                            "entry_time": broker_entry.get("entry_time"),
                            "entry_reason": broker_entry.get("entry_reason", ""),
                            "entry_spot": safe_float(broker_entry.get("entry_spot")),
                            "signal_time": broker_entry.get("signal_time", broker_entry.get("entry_time")),
                            "signal_spot_price": safe_float(broker_entry.get("signal_spot_price"), safe_float(broker_entry.get("entry_spot"))),
                            "signal_option_price": signal_entry_price,
                            "entry_price_strategy": signal_entry_price,
                            "entry_price": actual_entry_price,
                            "exit_signal_time": actual_exit_time or datetime.now(IST),
                            "exit_signal_spot_price": 0.0,
                            "exit_signal_option_price": actual_exit_price,
                            "exit_time": actual_exit_time or datetime.now(IST),
                            "exit_reason": "MANUAL_INTERRUPT_EXIT",
                            "exit_spot": 0.0,
                            "exit_price_strategy": actual_exit_price,
                            "exit_price": actual_exit_price,
                            "qty": qty,
                            "pnl_points": pnl_points,
                            "pnl_rs": pnl_rs,
                            "realized_mtm_after_trade": live_state["realized_live_pnl_rs"],
                            "entry_order_id": str(broker_entry.get("entry_order_id", "")),
                            "exit_order_id": str(order_meta.get("exit_order_id", "")),
                            "exit_order_status": exit_status,
                            "slippage_entry_points": actual_entry_price - signal_entry_price,
                            "slippage_exit_points": 0.0,
                            "slippage_total_points": actual_entry_price - signal_entry_price,
                            "emergency_sl_pct_used": safe_float(broker_entry.get("emergency_sl_pct_used"), 0.0),
                            "target_profit_points": safe_float(broker_entry.get("target_profit_points"), np.nan),
                        }
                        live_state["live_trade_events"].append(trade_record)
                        live_state["broker_positions"].pop(event_id, None)
                    except Exception as exit_err:
                        print(f"  ERROR exiting position {event_id}: {exit_err}")
                        print(f"  *** MANUAL ACTION REQUIRED: Flat {broker_entry.get('option_symbol', '?')} via broker terminal! ***")

            # Fallback: flatten any residual CE/PE net quantity even if local state missed it.
            if execution_mode == "LIVE_ORDER":
                for symbol in [live_state.get("current_ce_symbol", ce_symbol), live_state.get("current_pe_symbol", pe_symbol)]:
                    net_qty = get_net_position_qty(kite, symbol)
                    if net_qty == 0:
                        continue
                    tx_label = "SELL" if net_qty > 0 else "BUY"
                    print(f"  Broker fallback square-off: {tx_label} {abs(net_qty)} {symbol} (net_qty={net_qty})")
                    try:
                        sq_meta = place_live_squareoff_order(kite, symbol, net_qty)
                        sq_snapshot = wait_for_order_terminal(kite, sq_meta["squareoff_order_id"])
                        sq_status = normalize_order_status(sq_snapshot.get("status"))
                        sq_price = extract_avg_price(sq_snapshot)
                        print(
                            f"  SQUARE-OFF {sq_status}: order_id={sq_meta['squareoff_order_id']}  "
                            f"symbol={symbol}  price={sq_price}"
                        )
                    except Exception as sq_err:
                        print(f"  ERROR fallback square-off for {symbol}: {sq_err}")
                        print(f"  *** MANUAL ACTION REQUIRED: Flat {symbol} via broker terminal! ***")
            # --- PAPERTRADE_LIVE mode: close simulated open position on shutdown ---
            elif execution_mode == "PAPERTRADE_LIVE":
                final_trades = live_state.get("final_trades")
                open_pos = final_trades.attrs.get("open_position") if isinstance(final_trades, pd.DataFrame) else None
                if not open_pos:
                    open_pos = live_state.get("paper_open_position")
                if not open_pos:
                    # A concurrent candle rebuild can replace final_trades between
                    # the last BUY and shutdown. Reconstruct the still-open paper
                    # position from the event stream before giving up.
                    paper_events = live_state.get("live_events") or []
                    buy_events = [
                        event for event in paper_events
                        if str(event.get("event_type") or "").upper() == "BUY"
                    ]
                    sell_events = [
                        event for event in paper_events
                        if str(event.get("event_type") or "").upper() == "SELL"
                    ]
                    if len(buy_events) > len(sell_events):
                        last_buy = buy_events[-1]
                        open_pos = {
                            "side": str(last_buy.get("side") or ""),
                            "option_symbol": str(last_buy.get("option_symbol") or ""),
                            "option_token": safe_int(last_buy.get("option_token"), 0),
                            "entry_time": last_buy.get("event_time"),
                            "entry_reason": str(last_buy.get("event_reason") or ""),
                            "entry_spot": safe_float(last_buy.get("spot_price"), 0.0),
                            "entry_price": safe_float(last_buy.get("option_price"), 0.0),
                            "best_price": safe_float(last_buy.get("option_price"), 0.0),
                            "high_during_trade": safe_float(last_buy.get("option_price"), 0.0),
                            "qty": safe_int(last_buy.get("qty"), QTY),
                            "mtf_entry_label": str(last_buy.get("mtf_label") or ""),
                            "gap_type": str(last_buy.get("gap_type") or ""),
                            "gap_points": safe_float(last_buy.get("gap_points"), np.nan),
                            "gap_pct": safe_float(last_buy.get("gap_pct"), np.nan),
                            "cpr_type": str(last_buy.get("cpr_type") or ""),
                            "cpr_width_pct": safe_float(last_buy.get("cpr_width_pct"), np.nan),
                            "emergency_sl_pct_used": safe_float(last_buy.get("emergency_sl_pct_used"), 0.0),
                            "emergency_sl_price": safe_float(last_buy.get("emergency_sl_price"), np.nan),
                            "target_profit_points": safe_float(last_buy.get("target_profit_points"), 50.0),
                        }
                if open_pos:
                    side = str(open_pos.get("side") or "")
                    option_symbol = str(open_pos.get("option_symbol") or "")
                    qty = safe_int(open_pos.get("qty"), QTY)
                    entry_price = safe_float(open_pos.get("entry_price"), 0.0)
                    entry_spot = safe_float(open_pos.get("entry_spot"), 0.0)
                    # Guard against entry_price corruption caused by ATM symbol refresh
                    # while the paper position was open: override from the original BUY
                    # live_event (same pattern used in emit_open_position_pnl_update).
                    for _ev in reversed(live_state.get("live_events") or []):
                        if (str(_ev.get("event_type") or "").upper() == "BUY"
                                and str(_ev.get("side") or "") == side):
                            _stored = safe_float(_ev.get("option_price"), 0.0)
                            if _stored > 0:
                                entry_price = _stored
                                option_symbol = str(_ev.get("option_symbol") or option_symbol)
                                entry_spot = safe_float(_ev.get("spot_price"), entry_spot) or entry_spot
                            break
                    now_local = datetime.now(IST)
                    pending_exit = live_state.get("paper_exit_pending") or {}
                    pending_matches = str(pending_exit.get("side") or "").upper() == side.upper()
                    if pending_matches:
                        exit_reason = str(pending_exit.get("exit_reason") or "TRAILING_STOP_HIT")
                        exit_time = pending_exit.get("exit_time") or now_local
                        exit_option_price = safe_float(pending_exit.get("ltp"), 0.0)
                    else:
                        exit_reason = "FORCE_EXIT" if now_local >= force_exit_ts else "SESSION_STOP"
                        exit_time = force_exit_ts if exit_reason == "FORCE_EXIT" else now_local
                        exit_option_price = get_option_ltp_from_live_state(live_state, side)
                    if exit_option_price <= 0 and option_symbol:
                        try:
                            exit_option_price = get_option_symbol_ltp(kite, option_symbol)
                        except Exception:
                            exit_option_price = 0.0
                    if exit_option_price <= 0:
                        exit_option_price = entry_price
                    persisted_open_position = live_state.get("paper_open_position") or {}
                    open_high_during_trade = max(
                        safe_float(open_pos.get("high_during_trade"), entry_price),
                        safe_float(open_pos.get("best_price"), entry_price),
                        safe_float(persisted_open_position.get("high_during_trade"), entry_price),
                        safe_float(persisted_open_position.get("best_price"), entry_price),
                    )
                    if open_high_during_trade <= 0:
                        open_high_during_trade = entry_price
                    shutdown_high_during_trade = max(open_high_during_trade, entry_price, exit_option_price)
                    exit_spot_price = get_spot_ltp_from_live_state(live_state)
                    if exit_spot_price <= 0:
                        try:
                            exit_spot_price = get_spot_ltp(kite, UNDERLYING_NAME)
                        except Exception:
                            exit_spot_price = 0.0
                    if exit_spot_price <= 0:
                        exit_spot_price = entry_spot

                    pnl_points = exit_option_price - entry_price
                    pnl_rs = pnl_points * qty
                    realized_after = safe_float(live_state.get("realized_live_pnl_rs"), 0.0) + pnl_rs

                    sell_event_row = format_live_event_row(
                        event_type="SELL",
                        run_mode=execution_mode,
                        trade_date=trade_date,
                        underlying=UNDERLYING_NAME,
                        side=side,
                        option_symbol=option_symbol,
                        event_time=exit_time,
                        event_reason=exit_reason,
                        spot_price=exit_spot_price,
                        option_price=exit_option_price,
                        qty=qty,
                        realized_mtm=realized_after,
                        mtf_label=str(open_pos.get("mtf_entry_label", "")),
                        gap_type=str(open_pos.get("gap_type", "")),
                        gap_points=safe_float(open_pos.get("gap_points"), np.nan),
                        gap_pct=safe_float(open_pos.get("gap_pct"), np.nan),
                        cpr_type=str(open_pos.get("cpr_type", "")),
                        cpr_width_pct=safe_float(open_pos.get("cpr_width_pct"), np.nan),
                        emergency_sl_pct_used=safe_float(open_pos.get("emergency_sl_pct_used"), 0.0),
                        emergency_sl_price=safe_float(open_pos.get("emergency_sl_price"), np.nan),
                    )
                    live_state["live_events"].append(sell_event_row)

                    trade_record = {
                        "run_mode": execution_mode,
                        "trade_date": trade_date,
                        "underlying": UNDERLYING_NAME,
                        "side": side,
                        "option_symbol": option_symbol,
                        "entry_time": open_pos.get("entry_time"),
                        "exit_time": exit_time,
                        "entry_reason": str(open_pos.get("entry_reason", "")),
                        "exit_reason": exit_reason,
                        "entry_spot": entry_spot,
                        "exit_spot": exit_spot_price,
                        "entry_price": entry_price,
                        "exit_price": exit_option_price,
                        "best_price": round(float(shutdown_high_during_trade), 2),
                        "high_during_trade": round(float(shutdown_high_during_trade), 2),
                        "qty": qty,
                        "pnl_points": pnl_points,
                        "pnl_rs": pnl_rs,
                        "realized_mtm_after_trade": realized_after,
                        "mtf_entry_label": str(open_pos.get("mtf_entry_label", "")),
                        "mtf_exit_label": str(open_pos.get("mtf_entry_label", "")),
                        "emergency_sl_pct_used": safe_float(open_pos.get("emergency_sl_pct_used"), 0.0),
                        "emergency_sl_price": safe_float(open_pos.get("emergency_sl_price"), np.nan),
                        "target_profit_points": safe_float(open_pos.get("target_profit_points"), 50.0),
                    }
                    live_state["live_trade_events"].append(trade_record)
                    live_state["realized_live_pnl_rs"] = realized_after
                    live_state["paper_exit_pending"] = None
                    live_state["paper_open_position"] = None
                    if isinstance(live_state.get("final_trades"), pd.DataFrame):
                        live_state["final_trades"].attrs["open_position"] = None
                        live_state["final_trades"].attrs["realized_mtm_rs"] = realized_after

                    print(
                        f"  Paper open position closed on shutdown: {side} {option_symbol} "
                        f"entry={entry_price:.2f} exit={exit_option_price:.2f} reason={exit_reason}"
                    )

                    if EXPORT_EXCEL:
                        excel_file = export_papertrade_events_to_excel(
                            trade_date=trade_date,
                            ce_symbol=ce_symbol,
                            pe_symbol=pe_symbol,
                            live_events=live_state["live_events"],
                            live_trade_events=live_state["live_trade_events"],
                            guard_stop_reason=exit_reason,
                            execution_mode=execution_mode,
                            reuse_path=live_state["excel_file_path"],
                            debug_day=live_state.get("last_day_debug_df"),
                        )
                        live_state["excel_file_path"] = excel_file
                        print(f"  [Excel updated after shutdown close] {excel_file}")
                else:
                    print("  No open paper position.")
            else:
                print("  No open positions found.")
        except Exception as cleanup_err:
            print(f"  Shutdown cleanup error: {cleanup_err}")

        # Print a compact session summary here so manual stop paths always log it
        # even if the outer main flow exits early.
        if not live_state.get("shutdown_summary_printed"):
            live_state["shutdown_summary_printed"] = True
            print("\n========== SESSION SUMMARY (SHUTDOWN) ==========")
            print(f"Run mode: {execution_mode}")
            print(f"Trigger : {trigger_reason}")
            trade_events = live_state.get("live_trade_events") or []
            if trade_events:
                trade_df = pd.DataFrame(trade_events)
                pnl_series = pd.to_numeric(trade_df.get("pnl_rs"), errors="coerce").fillna(0.0)
                total_trades = int(len(trade_df))
                win_trades = int((pnl_series > 0).sum())
                loss_or_flat = int((pnl_series <= 0).sum())
                total_pnl_rs = float(pnl_series.sum())
                print(f"Total trades       : {total_trades}")
                print(f"Winning trades     : {win_trades}")
                print(f"Losing/flat trades : {loss_or_flat}")
                print(f"Net P&L (Rs)       : {total_pnl_rs:.2f}")
            else:
                print("Total trades       : 0")
                print("Net P&L (Rs)       : 0.00")

            open_pos_active = False
            if execution_mode == "LIVE_ORDER":
                for broker_entry in (live_state.get("broker_positions") or {}).values():
                    if safe_int(broker_entry.get("broker_open_qty"), 0) > 0 and not bool(broker_entry.get("manual_or_broker_exit_detected")):
                        open_pos_active = True
                        break
            elif execution_mode == "PAPERTRADE_LIVE":
                events = live_state.get("live_events") or []
                buy_count = sum(1 for ev in events if str(ev.get("event_type") or "").upper() == "BUY")
                sell_count = sum(1 for ev in events if str(ev.get("event_type") or "").upper() == "SELL")
                open_pos_active = buy_count > sell_count
            else:
                try:
                    open_pos_active = bool(live_state.get("final_trades").attrs.get("open_position"))
                except Exception:
                    open_pos_active = False
            print(f"Open position      : {'YES' if open_pos_active else 'NO'}")
            if live_state.get("excel_file_path"):
                print(f"Excel file         : {live_state.get('excel_file_path')}")
            print("==============================================")

        print("========== SHUTDOWN COMPLETE ==========" )

    ticker = KiteTicker(kite.api_key, kite.access_token)
    ticker.on_ticks = on_ticks
    ticker.on_connect = on_connect
    ticker.on_close = on_close
    ticker.on_error = on_error

    prev_sigint_handler = signal.getsignal(signal.SIGINT)
    prev_sigterm_handler = signal.getsignal(signal.SIGTERM) if hasattr(signal, "SIGTERM") else None
    prev_sigbreak_handler = signal.getsignal(signal.SIGBREAK) if hasattr(signal, "SIGBREAK") else None

    def handle_stop_signal(signum, frame):
        _ = frame
        signal_name = signal.Signals(signum).name if signum in [s.value for s in signal.Signals] else str(signum)
        print(f"Received signal {signal_name}. Stopping ticker — open positions will be closed before exit...")
        live_state["stop"] = True
        # Emit an immediate, minimal shutdown summary so the launcher can detect
        # a clean shutdown even if full cleanup takes time or blocks.
        try:
            total_trades = len(live_state.get("live_trade_events") or [])
            net_pnl = safe_float(live_state.get("realized_live_pnl_rs"), 0.0)
            print("\n========== SESSION SUMMARY (SHUTDOWN) ==========")
            print(f"Run mode: {execution_mode}")
            print(f"Trigger : SIGNAL_{signal_name}")
            print(f"Total trades       : {total_trades}")
            print(f"Net P&L (Rs)       : {net_pnl:.2f}")
            print("==============================================")
        except Exception:
            pass
        # Safety watchdog: force-exit after 60 s if graceful shutdown hangs
        def _shutdown_watchdog():
            import time as _t, os as _os
            _t.sleep(60)
            print("\n[FORCE EXIT] Graceful shutdown timed out (60 s). Force-exiting now.")
            _os._exit(0)
        threading.Thread(target=_shutdown_watchdog, daemon=True, name="shutdown-watchdog").start()
        try:
            ticker.stop()
        except Exception:
            pass
        # Also trigger cleanup proactively in a background thread. The cleanup
        # is idempotent (checks shutdown_cleanup_done) so this is safe and
        # ensures cleanup runs even if the main connect loop hangs.
        try:
            threading.Thread(target=lambda: run_shutdown_cleanup(f"SIGNAL_{signal_name}"), daemon=True, name="signal-shutdown-trigger").start()
        except Exception:
            pass

    signal.signal(signal.SIGINT, handle_stop_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_stop_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handle_stop_signal)

    watchdog_thread = threading.Thread(target=watchdog_loop, name=f"tick-watchdog-{LOG_IDENTIFIER}", daemon=True)
    live_state["watchdog_thread"] = watchdog_thread
    watchdog_thread.start()

    adopt_open_live_positions_from_broker()

    print(f"Starting {execution_mode}... Press Ctrl+C to stop (will auto-exit open positions).")
    keyboard_interrupt_triggered = False
    try:
        ticker.connect(threaded=False)
    except KeyboardInterrupt:
        keyboard_interrupt_triggered = True
        live_state["stop"] = True

    stop_event = live_state.get("watchdog_stop_event")
    if stop_event is not None:
        try:
            stop_event.set()
        except Exception:
            pass
    wt = live_state.get("watchdog_thread")
    if wt is not None:
        try:
            wt.join(timeout=1.0)
        except Exception:
            pass
    try:
        signal.signal(signal.SIGINT, prev_sigint_handler)
        if hasattr(signal, "SIGTERM") and prev_sigterm_handler is not None:
            signal.signal(signal.SIGTERM, prev_sigterm_handler)
        if hasattr(signal, "SIGBREAK") and prev_sigbreak_handler is not None:
            signal.signal(signal.SIGBREAK, prev_sigbreak_handler)
    except Exception:
        pass
    if keyboard_interrupt_triggered:
        run_shutdown_cleanup("KEYBOARD_INTERRUPT")
    else:
        run_shutdown_cleanup("CONNECT_LOOP_EXIT")

    # Final broker reconciliation: capture manual/external exits even if websocket closed immediately after sell.
    if execution_mode == "LIVE_ORDER":
        try:
            sync_broker_positions_if_needed(force=True)
            new_events_added = append_broker_forced_exit_trades()
            if EXPORT_EXCEL and new_events_added:
                excel_file = export_papertrade_events_to_excel(
                    trade_date=trade_date,
                    ce_symbol=ce_symbol,
                    pe_symbol=pe_symbol,
                    live_events=live_state["live_events"],
                    live_trade_events=live_state["live_trade_events"],
                    guard_stop_reason=get_execution_stop_reason(live_state.get("final_trades")),
                    execution_mode=execution_mode,
                    reuse_path=live_state["excel_file_path"],
                    debug_day=live_state.get("last_day_debug_df"),
                )
                live_state["excel_file_path"] = excel_file
                print(f"[Excel updated] {excel_file}")
        except Exception as reconcile_exc:
            print(f"[Final broker reconcile error] {reconcile_exc}")

    if live_state["last_exception"] is not None and not is_transient_kite_api_error(live_state["last_exception"]):
        raise live_state["last_exception"]

    excel_file = export_papertrade_events_to_excel(
        trade_date=trade_date,
        ce_symbol=ce_symbol,
        pe_symbol=pe_symbol,
        live_events=live_state["live_events"],
        live_trade_events=live_state["live_trade_events"],
        guard_stop_reason=get_execution_stop_reason(live_state.get("final_trades")),
        execution_mode=execution_mode,
        reuse_path=live_state["excel_file_path"],
        debug_day=live_state.get("last_day_debug_df"),
    )

    if live_state["live_trade_events"]:
        final_trades = pd.DataFrame(live_state["live_trade_events"]).sort_values("entry_time").reset_index(drop=True)
    else:
        final_trades = live_state["final_trades"].copy()

    final_trades.attrs["papertrade_excel_file"] = excel_file
    final_trades.attrs["live_excel_file"] = excel_file
    final_trades.attrs["guard_stop_reason"] = get_execution_stop_reason(live_state.get("final_trades"))
    final_trades.attrs["papertrade_fresh_count_on_restart"] = PAPERTRADE_FRESH_COUNT_ON_RESTART
    final_trades.attrs["papertrade_restart_tracking_from"] = live_tracking_start
    if not final_trades.empty and "pnl_rs" in final_trades.columns:
        final_trades.attrs["realized_mtm_rs"] = float(final_trades["pnl_rs"].sum())
    else:
        final_trades.attrs["realized_mtm_rs"] = 0.0

    return final_trades


def resample_to_5m(spot: pd.DataFrame) -> pd.DataFrame:
    x = spot.copy().set_index("ts")
    out = pd.DataFrame({
        "open": x["open"].resample(MTF_INTERVAL).first(),
        "high": x["high"].resample(MTF_INTERVAL).max(),
        "low": x["low"].resample(MTF_INTERVAL).min(),
        "close": x["close"].resample(MTF_INTERVAL).last(),
        "volume": x["volume"].resample(MTF_INTERVAL).sum(),
    }).dropna().reset_index()
    interval_minutes = safe_float(str(MTF_INTERVAL).replace("min", ""), 1.0)
    # Label each bar with its last 1-minute candle (09:15 5min bar -> 09:19,
    # 1min bar -> itself) so merge_asof uses only completed data, without lag.
    out["ts"] = out["ts"] + pd.Timedelta(minutes=max(0, int(interval_minutes) - 1))
    return out


def mtf_trend_score(row: pd.Series) -> int:
    bull = 0
    bear = 0

    bull += int(row["mtf_close"] > row["mtf_ema"])
    bull += int(bool(row["mtf_ha_green"]))
    bull += int(bool(row["mtf_ema_slope_up"]))
    bull += int(row["mtf_close"] > row["mtf_vwap"])

    bear += int(row["mtf_close"] < row["mtf_ema"])
    bear += int(bool(row["mtf_ha_red"]))
    bear += int(bool(row["mtf_ema_slope_down"]))
    bear += int(row["mtf_close"] < row["mtf_vwap"])

    threshold_needed = MTF_STRICTNESS + 1
    
    if bull >= threshold_needed and bull > bear:
        result = 1
    elif bear >= threshold_needed and bear > bull:
        result = -1
    else:
        result = 0
    
    # Debug logging - shows EVERY 5-minute candle's trend calculation
    if MTF_STRICTNESS_DEBUG:
        ts = row.get("mtf_ts", "unknown")
        result_label = {1: "BULL", -1: "BEAR", 0: "NEUTRAL"}[result]
        print(f"[MTF CALC] {ts} | bull={bull} bear={bear} (need>={threshold_needed}) | result={result_label}")
    
    return result


def option_premium_confirm(series: pd.Series, idx: int, prev_close: float | None = None) -> bool:
    if idx < 0:
        return False
    current = safe_float(series.iloc[idx], float("nan"))
    if pd.isna(current):
        return False
    if prev_close is None:
        if idx < 1:
            return False
        prev_close = safe_float(series.iloc[idx - 1], float("nan"))
    if pd.isna(prev_close):
        return False
    return bool(current > prev_close and current > MIN_PREMIUM)


def bull_reversal_score(row: pd.Series, prev: pd.Series, swing_low: float) -> int:
    score = 0
    stretched_down = row["spot_close"] < row["spot_vwap"] * (1 - VWAP_DISTANCE_PCT / 100)
    reclaim_ema = row["spot_close"] > row["spot_ema"]
    ha_flip = bool(row["spot_ha_green"])
    vol_ok = pd.notna(row["spot_vol_avg"]) and row["spot_volume"] >= VOL_MULT * row["spot_vol_avg"]
    higher_low = row["spot_low"] >= swing_low
    close_near_high = row["spot_close"] >= row["spot_low"] + 0.55 * (row["spot_high"] - row["spot_low"] + 1e-9)
    green_vs_prev = row["spot_close"] > prev["spot_close"]

    score += int(stretched_down)
    score += int(reclaim_ema)
    score += int(ha_flip)
    score += int(vol_ok)
    score += int(higher_low)
    score += int(close_near_high)
    score += int(green_vs_prev)
    return score


def bear_reversal_score(row: pd.Series, prev: pd.Series, swing_high: float) -> int:
    score = 0
    stretched_up = row["spot_close"] > row["spot_vwap"] * (1 + VWAP_DISTANCE_PCT / 100)
    lose_ema = row["spot_close"] < row["spot_ema"]
    ha_flip = bool(row["spot_ha_red"])
    vol_ok = pd.notna(row["spot_vol_avg"]) and row["spot_volume"] >= VOL_MULT * row["spot_vol_avg"]
    lower_high = row["spot_high"] <= swing_high
    close_near_low = row["spot_close"] <= row["spot_low"] + 0.45 * (row["spot_high"] - row["spot_low"] + 1e-9)
    red_vs_prev = row["spot_close"] < prev["spot_close"]

    score += int(stretched_up)
    score += int(lose_ema)
    score += int(ha_flip)
    score += int(vol_ok)
    score += int(lower_high)
    score += int(close_near_low)
    score += int(red_vs_prev)
    return score


def prepare_merged_day(spot_1m: pd.DataFrame, ce_1m: pd.DataFrame, pe_1m: pd.DataFrame) -> pd.DataFrame:
    spot_i = add_indicators(spot_1m, ema_len=EMA_LEN, vol_lookback=VOL_LOOKBACK).rename(columns={
        "ts": "spot_ts", "open": "spot_open", "high": "spot_high", "low": "spot_low",
        "close": "spot_close", "volume": "spot_volume", "ema": "spot_ema",
        "vwap": "spot_vwap", "vol_avg": "spot_vol_avg", "ha_open": "spot_ha_open",
        "ha_close": "spot_ha_close", "ha_high": "spot_ha_high", "ha_low": "spot_ha_low",
        "ha_green": "spot_ha_green", "ha_red": "spot_ha_red"
    })

    mtf = resample_to_5m(spot_1m)
    mtf_i = add_indicators(mtf, ema_len=MTF_EMA_LEN, vol_lookback=max(3, VOL_LOOKBACK // 2)).rename(columns={
        "ts": "mtf_ts", "open": "mtf_open", "high": "mtf_high", "low": "mtf_low",
        "close": "mtf_close", "volume": "mtf_volume", "ema": "mtf_ema",
        "vwap": "mtf_vwap", "vol_avg": "mtf_vol_avg", "ha_open": "mtf_ha_open",
        "ha_close": "mtf_ha_close", "ha_high": "mtf_ha_high", "ha_low": "mtf_ha_low",
        "ha_green": "mtf_ha_green", "ha_red": "mtf_ha_red", "ema_prev": "mtf_ema_prev",
        "ema_slope_up": "mtf_ema_slope_up", "ema_slope_down": "mtf_ema_slope_down"
    })

    ce_i = ce_1m.rename(columns={
        "ts": "ce_ts", "open": "ce_open", "high": "ce_high", "low": "ce_low",
        "close": "ce_close", "volume": "ce_volume"
    })
    pe_i = pe_1m.rename(columns={
        "ts": "pe_ts", "open": "pe_open", "high": "pe_high", "low": "pe_low",
        "close": "pe_close", "volume": "pe_volume"
    })

    day = pd.merge_asof(
        spot_i.sort_values("spot_ts"),
        mtf_i.sort_values("mtf_ts"),
        left_on="spot_ts",
        right_on="mtf_ts",
        direction="backward",
        tolerance=pd.Timedelta("10min")
    )
    day = pd.merge_asof(
        day.sort_values("spot_ts"),
        ce_i.sort_values("ce_ts"),
        left_on="spot_ts",
        right_on="ce_ts",
        direction="backward",
        tolerance=pd.Timedelta("2min")
    )
    day = pd.merge_asof(
        day.sort_values("spot_ts"),
        pe_i.sort_values("pe_ts"),
        left_on="spot_ts",
        right_on="pe_ts",
        direction="backward",
        tolerance=pd.Timedelta("2min")
    )

    # A brief no-trade gap (>2min) in one option leg must not blank out the whole
    # row via dropna below: that would hide the *other* leg's real (and possibly
    # SL-breaching) price from the exit-evaluation loop for as long as the quiet
    # leg stays stale. Forward-fill each leg's OHLC independently so a stale CE
    # quote, for example, can never suppress a live PE stop-loss/target check.
    ce_cols = ["ce_ts", "ce_open", "ce_high", "ce_low", "ce_close", "ce_volume"]
    pe_cols = ["pe_ts", "pe_open", "pe_high", "pe_low", "pe_close", "pe_volume"]
    day[ce_cols] = day[ce_cols].ffill()
    day[pe_cols] = day[pe_cols].ffill()

    day = day.dropna(subset=["ce_close", "pe_close", "mtf_close"]).reset_index(drop=True)
    day["mtf_trend"] = day.apply(mtf_trend_score, axis=1)
    day["mtf_label"] = day["mtf_trend"].map({1: "BULLISH_5M", -1: "BEARISH_5M", 0: "NEUTRAL_5M"})
    day["signal"] = ""
    day["position"] = ""
    return day


def run_single_day_test(day: pd.DataFrame, run_mode: str, previous_realized_mtm: float = 0.0, execution_entry_count: int = 0, tracking_start_override=None, ce_symbol_override: Optional[str] = None, pe_symbol_override: Optional[str] = None, ce_token_override: int = 0, pe_token_override: int = 0, lock_symbols_during_open: bool = False) -> pd.DataFrame:
    run_mode = normalize_run_mode(run_mode)
    session_trade_date = str(TRADE_DATE)
    if day is not None and not day.empty and "spot_ts" in day.columns:
        try:
            _session_ts = pd.Timestamp(day.iloc[-1]["spot_ts"])
            if _session_ts.tzinfo is None:
                _session_ts = _session_ts.tz_localize(IST)
            else:
                _session_ts = _session_ts.tz_convert(IST)
            session_trade_date = _session_ts.strftime("%Y-%m-%d")
        except Exception:
            session_trade_date = str(TRADE_DATE)

    tracking_start = pd.Timestamp(f"{session_trade_date} {TRACKING_START}", tz=IST)
    effective_tracking_start = tracking_start
    if tracking_start_override is not None:
        override_ts = pd.Timestamp(tracking_start_override)
        if override_ts.tzinfo is None:
            override_ts = override_ts.tz_localize(IST)
        else:
            override_ts = override_ts.tz_convert(IST)
        effective_tracking_start = max(tracking_start, override_ts)
    # Resolve CE/PE symbols for this run (allow caller to override)
    local_ce_symbol = ce_symbol_override if ce_symbol_override is not None else CE_SYMBOL
    local_pe_symbol = pe_symbol_override if pe_symbol_override is not None else PE_SYMBOL
    no_new_entry_after_ts = pd.Timestamp(f"{session_trade_date} {NO_NEW_ENTRY_AFTER}", tz=IST)
    force_exit_ts = pd.Timestamp(f"{session_trade_date} {FORCE_EXIT}", tz=IST)

    trades: List[Dict] = []

    in_position = False
    side = None
    entry_price = None
    entry_ts = None
    entry_spot = None
    entry_option_symbol = None
    entry_option_token = 0
    sl = None
    fixed_sl_price = None
    trailing_stop_active = False
    emergency_sl_price = None
    emergency_sl_pct_used = float(FIXED_SL_POINTS)
    best_price = None
    stage2_peak = None
    entry_reason = None
    trade_count = 0
    realized_mtm_rs = previous_realized_mtm
    global completed_trades_for_profit_lock, profit_lock_active, profit_lock_level
    if run_mode in {"LIVE_ORDER", "PAPERTRADE_LIVE"}:
        # Live rebuilds start after the last exit, so earlier trades are not replayed
        # here, and paper records its exits outside this function. Count them so the
        # profit lock can arm during the next trade exactly as in the backtest.
        completed_trades_for_profit_lock = max(
            safe_int(completed_trades_for_profit_lock, 0), safe_int(execution_entry_count, 0)
        )
    guard_stop_reason = None
    consecutive_losses = 0
    # Track last completed trade to avoid immediate re-entry on same symbol
    last_completed_option_symbol = None
    last_completed_exit_time = None
    last_completed_exit_price = None  # for SAME_SYMBOL_MIN_REENTRY_DROP_POINTS check
    # Track last ATM symbol refresh (candle timestamp, for backtest)
    last_symbol_refresh_ts = None
    # After first completed trade, keep strike fixed and only allow CE/PE side switch.
    strike_locked_after_first_trade = False

    day["guard_status"] = ""
    day["realized_mtm_rs"] = 0.0

    market_ctx = build_market_context(
        day,
        prev_high=safe_float(day.attrs.get("nifty_prev_high", 0.0), 0.0),
        prev_low=safe_float(day.attrs.get("nifty_prev_low", 0.0), 0.0),
        prev_close=safe_float(day.attrs.get("nifty_prev_close", 0.0), 0.0),
    )
    effective_tracking_start = apply_gap_entry_policy(
        market_ctx.get("gap_pct"),
        effective_tracking_start,
        session_trade_date,
        source="backtest",
    )

    # Per-side entry switches: when configured as 0, disable fresh entries for that side.
    allow_ce_entries = safe_int(MAX_CONCURRENT_TRADES_CE, 1) > 0
    allow_pe_entries = safe_int(MAX_CONCURRENT_TRADES_PE, 1) > 0
    last_ce_symbol = None
    last_pe_symbol = None
    last_ce_prev_close = None
    last_pe_prev_close = None

    for i in range(1, len(day)):
        row = day.iloc[i]
        prev = day.iloc[i - 1]
        ts = row["spot_ts"]

        if local_ce_symbol != last_ce_symbol:
            last_ce_symbol = local_ce_symbol
            last_ce_prev_close = None
        if local_pe_symbol != last_pe_symbol:
            last_pe_symbol = local_pe_symbol
            last_pe_prev_close = None

        if ts < effective_tracking_start:
            continue
        # Apply the same MTF candle-eligibility guard in every mode.
        # resample_to_5m() timestamps MTF candles at their completed-candle time;
        # do not evaluate a row before that timestamp.
        mtf_ts = row.get("mtf_ts")
        if pd.notna(mtf_ts) and ts < mtf_ts:
            continue

        day.loc[i, "realized_mtm_rs"] = realized_mtm_rs

        if not in_position:
            # Always block fresh entries after the configured cutoff time,
            # regardless of whether MTM guard or MAX_TRADES mode is active.
            # print(f"Checking entry conditions at {ts}... (realized MTM={realized_mtm_rs:.2f} rs, trade_count={trade_count})")

            # Periodic ATM symbol refresh in backtest: re-select every N minutes when flat.
            # PAPERTRADE_LIVE/LIVE_ORDER must not switch here: refresh_entry_symbols()
            # owns their strike, and `day` holds candles for exactly that strike, so a
            # switch here would price the signal on one strike and trade another.
            if (
                run_mode == "BACKTEST"
                and AUTO_SELECT_OPTION_SYMBOLS
                and not AUTO_SELECT_SESSION_LOCKED
                and (not strike_locked_after_first_trade)
                and (not lock_symbols_during_open)
            ):
                _refresh_interval = timedelta(minutes=max(1, safe_int(AUTO_SELECT_REFRESH_INTERVAL_MINUTES, 5)))
                _refresh_due = (
                    last_symbol_refresh_ts is None
                    or (ts - last_symbol_refresh_ts) >= _refresh_interval
                )
                if _refresh_due:
                    try:
                        _kite = globals().get("kite")
                        if _kite is not None:
                            _spot = float(row["spot_close"])
                            if run_mode == "BACKTEST" and BACKTEST_OPTION_DATA_SOURCE == "DOWNLOADED":
                                _new_ce, _new_pe = select_downloaded_option_symbols(
                                    day.rename(columns={"spot_ts": "ts", "spot_close": "close"}),
                                    session_trade_date,
                                    spot_price_override=_spot,
                                    reference_ts_override=ts,
                                )
                                _new_ce_token = _new_pe_token = 0
                            else:
                                _new_ce, _new_pe, _new_ce_token, _new_pe_token = auto_select_ce_pe_symbols(
                                    _kite, _spot, OPTION_EXCHANGE, selection_time=ts
                                )
                            # Same hysteresis as refresh_entry_symbols(): move to an adjacent
                            # strike only once spot is past the midpoint by the buffer.
                            _old_strike = extract_option_strike(local_ce_symbol or local_pe_symbol)
                            _cand_strike = extract_option_strike(_new_ce or _new_pe)
                            _step = max(1, safe_int(AUTO_SELECT_STRIKE_STEP, 50))
                            _buffer = max(1.0, safe_float(AUTO_SELECT_STRIKE_SWITCH_BUFFER_POINTS, 12.5))
                            if _old_strike > 0 and _cand_strike > 0 and abs(_cand_strike - _old_strike) == _step:
                                _direction = 1.0 if _cand_strike > _old_strike else -1.0
                                if (_spot - (_old_strike + _cand_strike) / 2.0) * _direction < _buffer:
                                    _new_ce, _new_pe = local_ce_symbol, local_pe_symbol
                            global _SIGNAL_CLOSE_STRIKE_LOG_KEYS
                            _signal_close_log_key = (str(run_mode), str(ts), str(_new_ce), str(_new_pe))
                            if _signal_close_log_key not in _SIGNAL_CLOSE_STRIKE_LOG_KEYS:
                                print(f"[SIGNAL-CLOSE STRIKE] mode={run_mode} candle={ts} spot_close={_spot:.2f} CE={_new_ce} PE={_new_pe}")
                                _SIGNAL_CLOSE_STRIKE_LOG_KEYS.add(_signal_close_log_key)
                            if _new_ce != local_ce_symbol or _new_pe != local_pe_symbol:
                                _old_ce = local_ce_symbol
                                _old_pe = local_pe_symbol
                                local_ce_symbol = _new_ce
                                local_pe_symbol = _new_pe
                                # In BACKTEST mode the day dataframe still holds data for the
                                # originally-fetched option.  Re-fetch candle data for the newly
                                # selected symbols and overwrite the remaining rows in `day` so
                                # that entry/exit prices stay consistent with the active symbol.
                                if run_mode == "BACKTEST":
                                    # Include the previous candle: premium confirmation compares
                                    # against it, and live mode reloads the new strike's history.
                                    _remaining_idx = day.index[i - 1:]
                                    _remaining_spot = day.loc[_remaining_idx, ["spot_ts"]].copy()
                                    if _new_ce != _old_ce and (_new_ce_token or (run_mode == "BACKTEST" and BACKTEST_OPTION_DATA_SOURCE == "DOWNLOADED")):
                                        try:
                                            _ce_hist = (load_downloaded_option_history(_new_ce, session_trade_date) if BACKTEST_OPTION_DATA_SOURCE == "DOWNLOADED" else fetch_history(_kite, _new_ce_token, session_trade_date, "minute"))
                                            if not _ce_hist.empty:
                                                _ce_new = _ce_hist.rename(columns={"ts": "ce_ts", "open": "ce_open", "high": "ce_high", "low": "ce_low", "close": "ce_close", "volume": "ce_volume"})
                                                _ce_merged = pd.merge_asof(
                                                    _remaining_spot.sort_values("spot_ts"),
                                                    _ce_new.sort_values("ce_ts"),
                                                    left_on="spot_ts", right_on="ce_ts",
                                                    direction="backward", tolerance=pd.Timedelta("2min"),
                                                )
                                                _ce_merged = _ce_merged.set_index(_remaining_spot.index)
                                                for _c in ["ce_ts", "ce_open", "ce_high", "ce_low", "ce_close", "ce_volume"]:
                                                    if _c in _ce_merged.columns and _c in day.columns:
                                                        day.loc[_remaining_idx, _c] = _ce_merged[_c]
                                        except Exception:
                                            pass
                                    if _new_pe != _old_pe and (_new_pe_token or (run_mode == "BACKTEST" and BACKTEST_OPTION_DATA_SOURCE == "DOWNLOADED")):
                                        try:
                                            _pe_hist = (load_downloaded_option_history(_new_pe, session_trade_date) if BACKTEST_OPTION_DATA_SOURCE == "DOWNLOADED" else fetch_history(_kite, _new_pe_token, session_trade_date, "minute"))
                                            if not _pe_hist.empty:
                                                _pe_new = _pe_hist.rename(columns={"ts": "pe_ts", "open": "pe_open", "high": "pe_high", "low": "pe_low", "close": "pe_close", "volume": "pe_volume"})
                                                _pe_merged = pd.merge_asof(
                                                    _remaining_spot.sort_values("spot_ts"),
                                                    _pe_new.sort_values("pe_ts"),
                                                    left_on="spot_ts", right_on="pe_ts",
                                                    direction="backward", tolerance=pd.Timedelta("2min"),
                                                )
                                                _pe_merged = _pe_merged.set_index(_remaining_spot.index)
                                                for _c in ["pe_ts", "pe_open", "pe_high", "pe_low", "pe_close", "pe_volume"]:
                                                    if _c in _pe_merged.columns and _c in day.columns:
                                                        day.loc[_remaining_idx, _c] = _pe_merged[_c]
                                        except Exception:
                                            pass
                                    last_ce_prev_close = safe_float(day.iloc[i - 1].get("ce_close"), float("nan"))
                                    last_pe_prev_close = safe_float(day.iloc[i - 1].get("pe_close"), float("nan"))
                    except Exception as _ref_exc:
                        pass
                    last_symbol_refresh_ts = ts
                    if run_mode == "BACKTEST" and BACKTEST_OPTION_DATA_SOURCE == "DOWNLOADED":
                        print(f"[STRIKE AUDIT] mode=BACKTEST source=DOWNLOADED reference=SIGNAL_CLOSE time={ts} spot={_spot:.2f} CE={local_ce_symbol} PE={local_pe_symbol}")

                    # The refresh can overwrite the current row with candles
                    # for the newly selected option symbols. Re-read it so the
                    # signal uses the active symbol's price, not the stale row.
                    row = day.iloc[i]

            if ts >= no_new_entry_after_ts:
                last_ce_prev_close = safe_float(row.get("ce_close"), float("nan"))
                last_pe_prev_close = safe_float(row.get("pe_close"), float("nan"))
                day.loc[i, "guard_status"] = "NO_NEW_ENTRY_CUTOFF"
                continue

            mtm_hit, mtm_reason = mtm_guard_status(realized_mtm_rs) if ENABLE_MTM_GUARD else (False, '')

            if mtm_hit:
                guard_stop_reason = mtm_reason
                day.loc[i, "guard_status"] = guard_stop_reason
                break

            # trade_count resets on every incremental rebuild. Use the
            # persistent session counter in live/paper modes.
            if run_mode in {"PAPERTRADE_LIVE", "LIVE_ORDER"}:
                max_trades_cfg = safe_int(MAX_TRADES, 0)
                trade_guard_hit = max_trades_cfg > 0 and safe_int(execution_entry_count, 0) >= max_trades_cfg
            else:
                trade_guard_hit = (
                    MAX_TRADES is not None
                    and MAX_TRADES > 0
                    and trade_count >= MAX_TRADES
                )
            if trade_guard_hit:
                guard_stop_reason = "MAX_TRADES_REACHED"
                day.loc[i, "guard_status"] = guard_stop_reason
                break

            swing_slice = day.iloc[max(0, i - SWING_LOOKBACK):i]
            swing_low = float(swing_slice["spot_low"].min()) if len(swing_slice) else float(prev["spot_low"])
            swing_high = float(swing_slice["spot_high"].max()) if len(swing_slice) else float(prev["spot_high"])

            bull_score = bull_reversal_score(row, prev, swing_low)
            bear_score = bear_reversal_score(row, prev, swing_high)
            ce_prev_close = last_ce_prev_close
            pe_prev_close = last_pe_prev_close
            ce_confirm = option_premium_confirm(day["ce_close"], i, prev_close=ce_prev_close)
            pe_confirm = option_premium_confirm(day["pe_close"], i, prev_close=pe_prev_close)
            mtf_trend = int(row["mtf_trend"])

            ce_log_prev = safe_float(ce_prev_close, 0.0)
            pe_log_prev = safe_float(pe_prev_close, 0.0)

            # Log entry signal analysis for diagnostics. Include source timestamps
            # and prices so live/backtest parity can be compared row by row.
            if ENTRY_DIAGNOSTICS and (
                bull_score >= max(ENTRY_SCORE_THRESHOLD - 1, 0)
                or bear_score >= max(ENTRY_SCORE_THRESHOLD - 1, 0)
                or mtf_trend == 1
                or mtf_trend == -1
            ):
                mtf_label = {1: "BULLISH_5M", -1: "BEARISH_5M", 0: "NEUTRAL_5M"}[mtf_trend]
                print(
                    f"[ENTRY PARITY] mode={run_mode} spot_ts={ts} mtf_ts={row.get('mtf_ts')} "
                    f"ce_ts={row.get('ce_ts')} pe_ts={row.get('pe_ts')} "
                    f"spot={safe_float(row.get('spot_close'), 0.0):.2f} "
                    f"ce={safe_float(row.get('ce_close'), 0.0):.2f} "
                    f"prev_ce={ce_log_prev:.2f} "
                    f"bull_score={bull_score} bear_score={bear_score} "
                    f"ce_confirm={ce_confirm} pe_confirm={pe_confirm} mtf={mtf_label} "
                    f"ce_symbol={local_ce_symbol} pe_symbol={local_pe_symbol} "
                    f"threshold={ENTRY_SCORE_THRESHOLD}"
                )
            if ENTRY_DIAGNOSTICS and mtf_trend == 1 and bull_score >= ENTRY_SCORE_THRESHOLD and not (ce_confirm and allow_ce_entries):
                print(
                    f"[ENTRY BLOCK] mode={run_mode} spot_ts={ts} side=CE "
                    f"reason={'CE_CONFIRM_FALSE' if not ce_confirm else 'CE_DISABLED_BY_CONFIG'} "
                    f"bull_score={bull_score} ce_confirm={ce_confirm} allow_ce={allow_ce_entries} "
                    f"ce={safe_float(row.get('ce_close'), 0.0):.2f} "
                    f"prev_ce={ce_log_prev:.2f}"
                )

            if run_mode == "BACKTEST" and last_completed_exit_time is not None:
                _backtest_cooldown_mins = max(0, safe_int(REENTRY_COOLDOWN_MINUTES, 0))
                if _backtest_cooldown_mins > 0 and ts <= last_completed_exit_time + timedelta(minutes=_backtest_cooldown_mins):
                    last_ce_prev_close = safe_float(row.get("ce_close"), float("nan"))
                    last_pe_prev_close = safe_float(row.get("pe_close"), float("nan"))
                    day.loc[i, "guard_status"] = "REENTRY_COOLDOWN"
                    continue

            # Prevent re-entry on the same option symbol shortly after a completed trade
            candidate_symbol = None
            if mtf_trend == 1:
                candidate_symbol = local_ce_symbol
            elif mtf_trend == -1:
                candidate_symbol = local_pe_symbol

            cooldown_mins = safe_int(BLOCK_SAME_SYMBOL_REENTRY_WITHIN_MINUTES, 0)
            if candidate_symbol and last_completed_option_symbol == candidate_symbol and last_completed_exit_time is not None and ts <= (last_completed_exit_time + timedelta(minutes=cooldown_mins)):
                last_ce_prev_close = safe_float(row.get("ce_close"), float("nan"))
                last_pe_prev_close = safe_float(row.get("pe_close"), float("nan"))
                day.loc[i, "guard_status"] = "SAME_SYMBOL_COOLDOWN"
                continue

            # Price-drop gate: after a same-symbol exit, only re-enter once the option
            # has fallen at least SAME_SYMBOL_MIN_REENTRY_DROP_POINTS below the exit price.
            _min_drop = safe_float(SAME_SYMBOL_MIN_REENTRY_DROP_POINTS, 0.0)
            if _min_drop > 0 and candidate_symbol and candidate_symbol == last_completed_option_symbol and last_completed_exit_price is not None:
                _cur_opt_price = float(row["pe_close"] if candidate_symbol == local_pe_symbol else row["ce_close"])
                if _cur_opt_price >= last_completed_exit_price - _min_drop:
                    last_ce_prev_close = safe_float(row.get("ce_close"), float("nan"))
                    last_pe_prev_close = safe_float(row.get("pe_close"), float("nan"))
                    day.loc[i, "guard_status"] = "SAME_SYMBOL_MIN_DROP_NOT_MET"
                    continue

            # Do not enter options whose signal premium is above the configured
            # ceiling. This is evaluated before either CE or PE entry branch so
            # all execution modes follow the same filter.
            candidate_premium = (
                safe_float(row["ce_close"], 0.0)
                if mtf_trend == 1
                else safe_float(row["pe_close"], 0.0)
                if mtf_trend == -1
                else 0.0
            )
            if candidate_premium > safe_float(MAX_ENTRY_PREMIUM, 0.0):
                last_ce_prev_close = safe_float(row.get("ce_close"), float("nan"))
                last_pe_prev_close = safe_float(row.get("pe_close"), float("nan"))
                day.loc[i, "guard_status"] = "ENTRY_PREMIUM_TOO_HIGH"
                if ENTRY_DIAGNOSTICS:
                    print(
                        f"[ENTRY BLOCK] premium={candidate_premium:.2f} "
                        f"limit={safe_float(MAX_ENTRY_PREMIUM, 0.0):.2f} "
                        f"side={'CE' if mtf_trend == 1 else 'PE'} time={ts}"
                    )
                continue

            if mtf_trend == 1 and bull_score >= ENTRY_SCORE_THRESHOLD and ce_confirm and allow_ce_entries:
                side = "CE"
                entry_option_symbol = local_ce_symbol
                entry_option_token = safe_int(ce_token_override, 0)
                entry_price = float(row["ce_close"])  # signal fires at candle close; enter at close price
                if SIMULATE_LIVE_EXECUTION_IN_BACKTEST and run_mode == "BACKTEST":
                    entry_price = apply_paper_entry_slippage(entry_price, side="BUY")
                entry_ts = ts
                entry_spot = float(row["spot_close"])
                entry_number = safe_int(execution_entry_count, 0) + trade_count + 1
                fixed_sl_price = get_entry_stop_price(entry_price, entry_number)
                sl = fixed_sl_price
                trailing_stop_active = False
                emergency_sl_points = get_dynamic_emergency_sl_points(entry_price, entry_number)
                emergency_sl_price = max(OPTION_TICK_SIZE, entry_price - emergency_sl_points)
                emergency_sl_pct_used = emergency_sl_points
                best_price = entry_price
                stage2_peak = None
                entry_reason = f"BUY_CE_MTF_BULL_S{bull_score}"
                in_position = True
                trade_count += 1
                day.loc[i, "signal"] = entry_reason
                day.loc[i, "position"] = "LONG_CE"
                if run_mode == "BACKTEST":
                    print(f"[DEBUG] CE ENTRY SET in_position=True at {ts}", flush=True)
                    print(
                        f"Entered position at {entry_ts} | side={side} symbol={local_ce_symbol} "
                        f"reason={entry_reason} entry={entry_price:.2f} spot={entry_spot:.2f} "
                        f"sl={safe_float(sl, 0.0):.2f} emergency_sl={safe_float(emergency_sl_price, 0.0):.2f}"
                    )
            elif bull_score >= ENTRY_SCORE_THRESHOLD and ce_confirm and mtf_trend != 1:
                # Entry signal present but MTF trend is not bullish
                if run_mode == "BACKTEST" and ENTRY_DIAGNOSTICS:
                    day.loc[i, "guard_status"] = f"MTF_NOT_BULL(mtf={mtf_trend},bull_s={bull_score})"
            elif mtf_trend == 1 and bull_score >= ENTRY_SCORE_THRESHOLD and ce_confirm and not allow_ce_entries:
                day.loc[i, "guard_status"] = "CE_DISABLED_BY_CONFIG"

            elif mtf_trend == -1 and bear_score >= ENTRY_SCORE_THRESHOLD and pe_confirm and allow_pe_entries:
                side = "PE"
                entry_option_symbol = local_pe_symbol
                entry_option_token = safe_int(pe_token_override, 0)
                entry_price = float(row["pe_close"])  # signal fires at candle close; enter at close price
                if SIMULATE_LIVE_EXECUTION_IN_BACKTEST and run_mode == "BACKTEST":
                    entry_price = apply_paper_entry_slippage(entry_price, side="BUY")
                entry_ts = ts
                entry_spot = float(row["spot_close"])
                entry_number = safe_int(execution_entry_count, 0) + trade_count + 1
                fixed_sl_price = get_entry_stop_price(entry_price, entry_number)
                sl = fixed_sl_price
                trailing_stop_active = False
                emergency_sl_points = get_dynamic_emergency_sl_points(entry_price, entry_number)
                emergency_sl_price = max(OPTION_TICK_SIZE, entry_price - emergency_sl_points)
                emergency_sl_pct_used = emergency_sl_points
                best_price = entry_price
                stage2_peak = None
                entry_reason = f"BUY_PE_MTF_BEAR_S{bear_score}"
                in_position = True
                trade_count += 1
                day.loc[i, "signal"] = entry_reason
                day.loc[i, "position"] = "LONG_PE"
                if run_mode == "BACKTEST":
                    print(
                        f"Entered position at {entry_ts} | side={side} symbol={local_pe_symbol} "
                        f"reason={entry_reason} entry={entry_price:.2f} spot={entry_spot:.2f} "
                        f"sl={safe_float(sl, 0.0):.2f} emergency_sl={safe_float(emergency_sl_price, 0.0):.2f}"
                    )
            elif bear_score >= ENTRY_SCORE_THRESHOLD and pe_confirm and mtf_trend != -1:
                # Entry signal present but MTF trend is not bearish
                if run_mode == "BACKTEST" and ENTRY_DIAGNOSTICS:
                    day.loc[i, "guard_status"] = f"MTF_NOT_BEAR(mtf={mtf_trend},bear_s={bear_score})"
            elif mtf_trend == -1 and bear_score >= ENTRY_SCORE_THRESHOLD and pe_confirm and not allow_pe_entries:
                day.loc[i, "guard_status"] = "PE_DISABLED_BY_CONFIG"

            last_ce_prev_close = safe_float(row.get("ce_close"), float("nan"))
            last_pe_prev_close = safe_float(row.get("pe_close"), float("nan"))

        else:
            last_ce_prev_close = safe_float(row.get("ce_close"), float("nan"))
            last_pe_prev_close = safe_float(row.get("pe_close"), float("nan"))
            ltp = float(row["ce_close"] if side == "CE" else row["pe_close"])
            # A long option stop can be hit intraminute even when the candle
            # closes back above the stop. Use the traded candle low for the
            # trigger, while keeping the close as the mark-to-market price.
            option_high = safe_float(row["ce_high"] if side == "CE" else row["pe_high"], ltp)
            option_low = safe_float(row["ce_low"] if side == "CE" else row["pe_low"], ltp)
            # Live websocket/rebuild rows can contain a valid close while
            # high/low is missing. Never let that data-quality gap disable a
            # protective stop: the current traded price is itself sufficient
            # evidence that a long-option stop was breached.
            option_high = max(option_high, ltp)
            option_low = min(option_low, ltp)
            best_price = max(best_price, ltp, option_high)
            stage2_peak = update_stage2_peak(stage2_peak, max(ltp, option_high), ts)
            option_open = safe_float(row.get("ce_open") if side == "CE" else row.get("pe_open"), ltp)
            pnl_pct = (ltp - entry_price) / entry_price
            open_pnl_rs = (ltp - entry_price) * QTY
            total_mtm_rs = realized_mtm_rs + open_pnl_rs
            day.loc[i, "realized_mtm_rs"] = realized_mtm_rs

            pnl_points_current = ltp - entry_price
            pnl_points_high = best_price - entry_price
            trailing_peak_points = get_trailing_peak_points(entry_price, best_price, stage2_peak, ts)
            sl_before_candle = safe_float(sl, 0.0)
            trailing_active_before_candle = trailing_stop_active
            trade_age = ts - entry_ts if entry_ts is not None else timedelta(0)
            
            # Log 1-minute PNL update for backtest mode
            if run_mode == "BACKTEST":
                symbol_for_log = local_ce_symbol if side == "CE" else local_pe_symbol
                unrealized_points = ltp - entry_price
                unrealized_rs = unrealized_points * QTY
                trailing_status_str = get_trailing_profit_status(
                    unrealized_points,
                    current_time=ts,
                    stage1_triggered=trailing_stop_active,
                )
                trailing_suffix = f" | trailing={trailing_status_str}" if trailing_status_str else ""
                print(
                    f"[OPEN PNL] {ts.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                    f"entry_time={entry_ts} | trade_count={trade_count} | symbol={symbol_for_log} | side={side} | ltp={ltp:.2f} | "
                    f"entry={entry_price:.2f} | qty={QTY} | unrealized={unrealized_points:.2f} pts ({unrealized_rs:.2f} rs) | "
                    f"realized={realized_mtm_rs:.2f} rs | total_mtm={total_mtm_rs:.2f} rs{trailing_suffix}",
                    flush=True,
                )
            
            if ENABLE_TRAILING_PROFIT_EXIT:
                updated_sl = get_trailing_stop_for_profit(
                    entry_price,
                    trailing_peak_points,
                    sl,
                    current_time=ts,
                    stage1_triggered=trailing_stop_active,
                )
                if fixed_sl_price is not None and updated_sl > fixed_sl_price:
                    trailing_stop_active = True
                sl = updated_sl

            exit_reason = None
            exit_price = None

            mtm_hit, mtm_reason = mtm_guard_status(total_mtm_rs) if ENABLE_MTM_GUARD else (False, '')

            if mtm_hit and mtm_reason == "DAILY_PROFIT_TARGET" and open_pnl_rs > 0:
                # Daily profit target reached while the current trade is in profit.
                # Activate trailing stop and let the trade run — do NOT exit immediately.
                trailing_stop_active = True
                updated_sl = get_trailing_stop_for_profit(
                    entry_price,
                    trailing_peak_points,
                    sl,
                    current_time=ts,
                    stage1_triggered=trailing_stop_active,
                )
                sl = max(sl, updated_sl)
                guard_stop_reason = "DAILY_PROFIT_TARGET"  # blocks new entries after this trade closes
                day.loc[i, "guard_status"] = "PROFIT_TARGET_TRAILING"
                mtm_hit = False  # suppress immediate MTM exit; trailing SL will manage the exit

            if (
                ENABLE_EMERGENCY_SL_ORDER
                and emergency_sl_price is not None
                and (option_low <= emergency_sl_price or ltp <= emergency_sl_price)
            ):
                exit_reason = "EMERGENCY_SL_HIT"
                # Fill at the stop level, or at the open if the candle gapped below it.
                exit_price = min(emergency_sl_price, option_open)
            elif mtm_hit:
                exit_reason = mtm_reason
                exit_price = ltp
                guard_stop_reason = mtm_reason
                day.loc[i, "guard_status"] = mtm_reason
            elif option_low <= sl_before_candle or ltp <= sl_before_candle:
                # Check the candle against the stop in place when it opened: a 1-minute
                # candle does not say whether its high or its low came first, so a stop
                # raised from this candle's high only applies from the next candle.
                if ENABLE_TRAILING_PROFIT_EXIT and trailing_active_before_candle:
                    exit_reason = "TRAILING_STOP_HIT"
                else:
                    exit_reason = "SL_HIT"
                # A gap through the stop fills at the open, otherwise at the stop level.
                exit_price = min(sl_before_candle, option_open)
            elif (
                ENABLE_MFE_EXIT
                and trade_age >= timedelta(minutes=max(1, safe_int(MFE_EXIT_AFTER_MINUTES, 45)))
                and pnl_points_high < safe_float(MFE_MIN_POINTS, 10.0)
                and pnl_points_current < 0
            ):
                exit_reason = "LOW_MFE_EXIT"
                exit_price = ltp
            elif False:  # Fixed 50-point target disabled; trailing-profit engine manages exits after 50 points
                exit_reason = "TARGET_HIT"
                exit_price = entry_price + 50
            elif ts >= force_exit_ts:
                exit_reason = "FORCE_EXIT"
                exit_price = ltp
            else:
                if side == "CE":
                    recent_high = day.iloc[max(0, i - SWING_LOOKBACK):i]["spot_high"].max()
                    opp_score = bear_reversal_score(row, prev, float(recent_high))
                    if False:
                        pass
                else:
                    recent_low = day.iloc[max(0, i - SWING_LOOKBACK):i]["spot_low"].min()
                    opp_score = bull_reversal_score(row, prev, float(recent_low))
                    if False:
                        pass

            if exit_reason is not None:
                if SIMULATE_LIVE_EXECUTION_IN_BACKTEST and run_mode == "BACKTEST":
                    exit_price = apply_paper_exit_slippage(exit_price, side="SELL")
                pnl_points = exit_price - entry_price
                pnl_rs = pnl_points * QTY
                realized_mtm_rs += pnl_rs

                # Profit-lock fix: count completed trades immediately after every exit
                completed_trades_for_profit_lock += 1

                # Arm profit lock after trade completion but do not stop trading immediately.
                if (
                    ENABLE_PROFIT_LOCK
                    and not profit_lock_active
                    and completed_trades_for_profit_lock >= PROFIT_LOCK_AFTER_TRADE_NO
                    and realized_mtm_rs >= PROFIT_LOCK_TRIGGER_RS
                ):
                    profit_lock_active = True
                    profit_lock_level = realized_mtm_rs * PROFIT_LOCK_PERCENT
                    print(f"[PROFIT LOCK ARMED] MTM={realized_mtm_rs:.2f} Lock Level={profit_lock_level:.2f}")

                if ENABLE_CONSECUTIVE_LOSS_GUARD:
                    if pnl_rs < 0:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0
                    if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                        guard_stop_reason = f"CONSECUTIVE_LOSSES_{MAX_CONSECUTIVE_LOSSES}"

                # Calculate cumulative realized P&L: sum of all closed trades' P&L
                # realized_mtm_rs starts from previous_realized_mtm, so live rebuilds
                # keep the session's earlier trades in the running total.
                cumulative_pnl = realized_mtm_rs
                _active_stage = "stage2" if ts.strftime("%H:%M") >= TRAILING_PROFIT_STAGE2_TIME else "stage1"
                _trigger_points = float(TRAILING_PROFIT_STAGE2[0][0]) if _active_stage == "stage2" and TRAILING_PROFIT_STAGE2 else float(TRAILING_PROFIT_STAGE1[0][0]) if TRAILING_PROFIT_STAGE1 else 50.0
                _max_profit_points = (best_price - entry_price) if best_price is not None and entry_price is not None else 0.0
                _trailing_status = (
                    f"{_active_stage}_hit"
                    if exit_reason in {"TRAILING_SL_HIT", "TRAILING_STOP_HIT"}
                    else f"{_active_stage}_armed"
                    if _max_profit_points >= _trigger_points
                    else "not_reached_target"
                )

                trades.append({
                    "run_mode": run_mode,
                    "trade_date": session_trade_date,
                    "underlying": UNDERLYING_NAME,
                    "side": side,
                    "option_symbol": entry_option_symbol or (local_ce_symbol if side == "CE" else local_pe_symbol),
                    "option_token": entry_option_token,
                    "entry_time": entry_ts,
                    "exit_time": ts,
                    "entry_reason": entry_reason,
                    "exit_reason": exit_reason,
                    "entry_spot": entry_spot,
                    "exit_spot": float(row["spot_close"]),
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "high_during_trade": round(float(best_price), 2) if best_price is not None else None,
                    "qty": QTY,
                    "pnl_points": pnl_points,
                    "pnl_rs": pnl_rs,
                    "realized_mtm_after_trade": cumulative_pnl,  # Cumulative P&L including this trade
                    "mtf_entry_label": day.loc[day["spot_ts"] == entry_ts, "mtf_label"].iloc[0],
                    "mtf_exit_label": row["mtf_label"],
                    "emergency_sl_pct_used": emergency_sl_pct_used,
                    "emergency_sl_price": emergency_sl_price,
                    "target_profit_points": next((float(pt) for pt, _ in get_active_trailing_stages(ts) if pnl_points < float(pt)), float(get_active_trailing_stages(ts)[-1][0]) if get_active_trailing_stages(ts) else 50.0),
                    "trailing_status": _trailing_status,
                })

                # record last completed trade symbol/time/price to enforce same-symbol cooldown and price-drop gate
                last_completed_option_symbol = local_ce_symbol if side == "CE" else local_pe_symbol
                last_completed_exit_time = ts
                last_completed_exit_price = exit_price

                # Next entry opportunity selects from its own completed spot candle close.

                day.loc[i, "signal"] = exit_reason
                day.loc[i, "position"] = "FLAT"
                day.loc[i, "realized_mtm_rs"] = cumulative_pnl
                
                # Sync the variable with cumulative P&L for next iteration
                realized_mtm_rs = cumulative_pnl

                _high_str = f"{best_price:.2f}" if best_price is not None else "N/A"
                _entry_str = f"{entry_price:.2f}" if entry_price is not None else "N/A"
                if run_mode == "BACKTEST":
                    print(f"Exited position at {ts} with reason {exit_reason}. Entry={_entry_str} High={_high_str} Exit={exit_price:.2f} | P&L: {pnl_rs:.2f} Rs ({pnl_points:.2f} points). Cumulative MTM: {cumulative_pnl:.2f} Rs.")

                in_position = False
                side = None
                entry_price = None
                entry_ts = None
                entry_spot = None
                sl = None
                fixed_sl_price = None
                trailing_stop_active = False
                emergency_sl_price = None
                emergency_sl_pct_used = float(FIXED_SL_POINTS)
                best_price = None
                stage2_peak = None
                entry_reason = None
                entry_option_symbol = None
                entry_option_token = 0
                # try:
                #     spot_ltp_now = get_spot_ltp(kite, UNDERLYING_NAME)
                #     ce_symbol_override, pe_symbol_override, new_ce_token, new_pe_token = auto_select_ce_pe_symbols(kite, spot_ltp_now, OPTION_EXCHANGE)
                # except Exception as exc:
                #     print(f"[SYMBOL REFRESH FAILED] reason={reason} error={exc}")
                #     return

                if exit_reason in {"DAILY_PROFIT_TARGET", "DAILY_LOSS_LIMIT"} or (guard_stop_reason and ("CONSECUTIVE_LOSSES" in guard_stop_reason or guard_stop_reason == "DAILY_PROFIT_TARGET")):
                    break
    
    result = pd.DataFrame(trades)
    if not guard_stop_reason and len(trades) > 0:
        last_exit_reason = trades[-1].get("exit_reason") if isinstance(trades, list) else trades.iloc[-1].get("exit_reason")
        if last_exit_reason:
            guard_stop_reason = str(last_exit_reason)
    result.attrs["guard_stop_reason"] = guard_stop_reason
    result.attrs["trade_count"] = trade_count
    result.attrs["realized_mtm_rs"] = realized_mtm_rs
    if in_position:
        entry_label = ""
        if entry_ts is not None and "mtf_label" in day.columns:
            entry_label_series = day.loc[day["spot_ts"] == entry_ts, "mtf_label"]
            if not entry_label_series.empty:
                entry_label = str(entry_label_series.iloc[0])
        result.attrs["open_position"] = {
            "side": side,
            "option_symbol": entry_option_symbol or (local_ce_symbol if side == "CE" else local_pe_symbol),
            "option_token": entry_option_token,
            "entry_time": entry_ts,
            "entry_reason": entry_reason,
            "entry_spot": entry_spot,
            "entry_price": entry_price,
            "best_price": round(float(best_price), 2) if best_price is not None else round(float(entry_price), 2),
            "high_during_trade": round(float(best_price), 2) if best_price is not None else round(float(entry_price), 2),
            "best_price_stage2": round(float(stage2_peak), 2) if stage2_peak is not None else None,
            "qty": QTY,
            "mtf_entry_label": entry_label,
            "gap_type": market_ctx.get("gap_type", ""),
            "gap_points": market_ctx.get("gap_points", ""),
            "gap_pct": market_ctx.get("gap_pct", ""),
            "cpr_type": market_ctx.get("cpr_type", ""),
            "cpr_width_pct": market_ctx.get("cpr_width_pct", ""),
            "emergency_sl_pct_used": emergency_sl_pct_used,
            "emergency_sl_price": emergency_sl_price,
            "trailing_stop_price": safe_float(sl, 0.0),
            "trailing_stop_active": bool(trailing_stop_active),
            "target_profit_points": next((float(pt) for pt, _ in get_active_trailing_stages(ts) if (best_price - entry_price if best_price is not None and entry_price is not None else 0.0) < float(pt)), float(get_active_trailing_stages(ts)[-1][0]) if get_active_trailing_stages(ts) else 50.0),
            "trailing_status": "stage2_pending" if entry_ts.strftime("%H:%M") >= TRAILING_PROFIT_STAGE2_TIME else "stage1_pending",
        }
    else:
        result.attrs["open_position"] = None
    return result


def _build_chart_bytes(trades: pd.DataFrame, day: pd.DataFrame, trade_date: str):
    """Render backtest chart into an in-memory PNG buffer. Returns BytesIO or None.

    Layout:
      Row 0 (tall)  : Nifty spot + EMA + VWAP + MTF-EMA; volume sub-bar; summary box
      Row 1, 2, …   : One panel per trade in chronological order showing option
                      price line + entry ▲ / exit ▼ + SL dashed line + shaded
                      trade region + exit-reason annotation.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import matplotlib.patches as mpatches
        from matplotlib.gridspec import GridSpec, GridSpecFromSubplotSpec

        def _to_ist_naive(ts_series):
            s = pd.to_datetime(ts_series, errors="coerce", utc=True)
            s = s.dt.tz_convert(IST).dt.tz_localize(None)
            return s

        def _ts_to_ist_naive(t):
            ts = pd.Timestamp(t)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            return ts.tz_convert(IST).tz_localize(None)

        day_ts = _to_ist_naive(day["spot_ts"]) if "spot_ts" in day.columns else pd.Series(dtype="datetime64[ns]")

        def _col(col):
            """Return (ist_naive_times_array, values_array) or (None, None)."""
            if col in day.columns:
                vals = pd.to_numeric(day[col], errors="coerce")
                valid = day_ts.notna() & vals.notna()
                return day_ts[valid].values, vals[valid].values
            return None, None

        spot_t,   spot_p   = _col("spot_close")
        ema_t,    ema_p    = _col("spot_ema")
        vwap_t,   vwap_p   = _col("spot_vwap")
        vol_t,    vol_v    = _col("spot_volume")
        volavg_t, volavg_v = _col("spot_vol_avg")
        mtf_t,    mtf_p    = _col("mtf_ema")      # 5-min EMA

        ce_t, ce_p = _col("ce_close")
        pe_t, pe_p = _col("pe_close")
        ce_series = pd.Series(ce_p, index=pd.DatetimeIndex(ce_t)) if ce_t is not None and len(ce_t) else None
        pe_series = pd.Series(pe_p, index=pd.DatetimeIndex(pe_t)) if pe_t is not None and len(pe_t) else None

        n_trades = len(trades) if not trades.empty else 0

        # ── Summary stats ────────────────────────────────────────────────────
        if not trades.empty and "pnl_rs" in trades.columns:
            pnl_vals = pd.to_numeric(trades["pnl_rs"], errors="coerce").fillna(0)
            net_pnl  = float(pnl_vals.sum())
            wins     = int((pnl_vals > 0).sum())
            losses   = int((pnl_vals <= 0).sum())
            win_pct  = 100 * wins / len(trades) if len(trades) else 0
        else:
            net_pnl = 0.0; wins = 0; losses = 0; win_pct = 0.0

        # ── Figure layout ────────────────────────────────────────────────────
        # Spot panel = 3 units tall (price 2.5 + volume 0.5), trade panels = 2 each
        n_rows = 1 + n_trades
        height_ratios = [3] + [2] * n_trades
        fig = plt.figure(figsize=(16, 4 + 4 * n_rows), facecolor="#1a1a2e")
        outer_gs = GridSpec(n_rows, 1, figure=fig, height_ratios=height_ratios, hspace=0.55)

        def _style_ax(ax):
            ax.set_facecolor("#16213e")
            ax.tick_params(colors="#cccccc", labelsize=8)
            ax.xaxis.label.set_color("#cccccc")
            ax.yaxis.label.set_color("#cccccc")
            ax.title.set_color("#ffffff")
            for spine in ax.spines.values():
                spine.set_edgecolor("#444466")

        def _fmt_xaxis(ax):
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=range(0, 60, 15)))
            plt.setp(ax.xaxis.get_majorticklabels(), rotation=0, ha="center")

        # ── Row 0: Nifty spot + volume sub-panel ────────────────────────────
        inner_gs0 = GridSpecFromSubplotSpec(
            2, 1, subplot_spec=outer_gs[0],
            height_ratios=[4, 1], hspace=0.05
        )
        ax_spot = fig.add_subplot(inner_gs0[0])
        ax_vol  = fig.add_subplot(inner_gs0[1], sharex=ax_spot)
        _style_ax(ax_spot); _style_ax(ax_vol)

        if spot_t is not None and len(spot_t):
            ax_spot.plot(spot_t, spot_p, color="#e0e0e0", linewidth=1.1, label=UNDERLYING_NAME, zorder=3)
        if ema_t is not None and len(ema_t):
            ax_spot.plot(ema_t, ema_p, color="#ffd54f", linewidth=1.0, linestyle="--", label=f"EMA({EMA_LEN})", zorder=2)
        if vwap_t is not None and len(vwap_t):
            ax_spot.plot(vwap_t, vwap_p, color="#4db6ac", linewidth=1.0, linestyle="-.", label="VWAP", zorder=2)
        if mtf_t is not None and len(mtf_t):
            ax_spot.plot(mtf_t, mtf_p, color="#ff8a65", linewidth=1.2, linestyle="-", label=f"MTF EMA({MTF_EMA_LEN})", zorder=2)

        # Summary text box
        summary_txt = (
            f"Net P&L: ₹{net_pnl:+.0f}   |   "
            f"Trades: {n_trades}   W: {wins}  L: {losses}   "
            f"Win%: {win_pct:.0f}%"
        )
        ax_spot.text(
            0.01, 0.97, summary_txt,
            transform=ax_spot.transAxes,
            fontsize=8, color="#ffffff", va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#0d0d2b", edgecolor="#666699", alpha=0.85),
        )

        ax_spot.set_title(f"{UNDERLYING_NAME}  |  {trade_date}", fontsize=10, pad=5)
        ax_spot.set_ylabel("Nifty", fontsize=8)
        ax_spot.legend(fontsize=7, labelcolor="#cccccc", facecolor="#16213e",
                       edgecolor="#444466", loc="upper right")
        plt.setp(ax_spot.get_xticklabels(), visible=False)

        # Volume bars
        if vol_t is not None and len(vol_t):
            bar_colors_vol = ["#66bb6a"] * len(vol_t)
            ax_vol.bar(vol_t, vol_v, width=0.0006, color="#4488aa", alpha=0.7)
        if volavg_t is not None and len(volavg_t):
            ax_vol.plot(volavg_t, volavg_v, color="#ffd54f", linewidth=0.8, label="Avg Vol")
        ax_vol.set_ylabel("Vol", fontsize=7)
        _fmt_xaxis(ax_vol)

        # ── Rows 1+: one panel per trade ────────────────────────────────────
        for trade_num, (_, row) in enumerate(trades.iterrows(), start=1):
            side        = str(row.get("side") or "").upper()
            opt_symbol  = str(row.get("option_symbol") or (CE_SYMBOL if side == "CE" else PE_SYMBOL))
            win         = float(row.get("pnl_rs", 0) or 0) >= 0
            e_color     = "#66bb6a" if win else "#ef5350"
            bg_color    = "#0a2010" if win else "#200a0a"
            result_lbl  = f"{'WIN' if win else 'LOSS'}  ₹{float(row.get('pnl_rs', 0) or 0):+.0f}"
            exit_reason = str(row.get("exit_reason") or "").replace("_", " ")
            entry_p_val = row.get("entry_price")
            exit_p_val  = row.get("exit_price")
            sl_p_val    = row.get("emergency_sl_price")
            entry_t_raw = row.get("entry_time")
            exit_t_raw  = row.get("exit_time")

            price_series = ce_series if side == "CE" else pe_series
            line_color   = "#4fc3f7" if side == "CE" else "#ce93d8"

            ax = fig.add_subplot(outer_gs[trade_num])
            ax.set_facecolor(bg_color)
            ax.tick_params(colors="#cccccc", labelsize=8)
            ax.xaxis.label.set_color("#cccccc")
            ax.yaxis.label.set_color("#cccccc")
            ax.title.set_color("#ffffff")
            for spine in ax.spines.values():
                spine.set_edgecolor("#444466")

            if price_series is not None:
                ax.plot(price_series.index, price_series.values,
                        color=line_color, linewidth=1.0, label=opt_symbol, zorder=3)

            ax.set_title(
                f"Trade {trade_num}  |  {trade_date}  |  {opt_symbol}  |  {result_lbl}"
                + (f"  |  {exit_reason}" if exit_reason else ""),
                fontsize=9, pad=5
            )
            ax.set_ylabel("Price", fontsize=8)
            _fmt_xaxis(ax)

            # Shaded trade region
            try:
                if entry_t_raw is not None and exit_t_raw is not None:
                    t_entry = _ts_to_ist_naive(entry_t_raw)
                    t_exit  = _ts_to_ist_naive(exit_t_raw)
                    ax.axvspan(t_entry, t_exit,
                               color=("#1a4a1a" if win else "#4a1a1a"), alpha=0.35, zorder=1)
            except Exception:
                pass

            # Horizontal line at entry price
            if entry_p_val is not None:
                try:
                    ep_val = float(entry_p_val)
                    if not pd.isna(ep_val) and ep_val > 0:
                        ax.axhline(ep_val, color="#66bb6a", linewidth=1.2,
                                   linestyle="-", alpha=0.7, zorder=4)
                        ax.text(0.01, ep_val, f" Entry {ep_val:.1f}",
                                transform=ax.get_yaxis_transform(),
                                fontsize=7, color="#66bb6a", va="bottom")
                except Exception:
                    pass

            def _mark(t_raw, p_val, marker, _ec=e_color, _ps=price_series, _ax=ax):
                if t_raw is None or _ps is None:
                    return
                try:
                    ts_ist = _ts_to_ist_naive(t_raw)
                    if p_val is not None and not pd.isna(float(p_val)):
                        price_y = float(p_val)
                    else:
                        idx = _ps.index.get_indexer([ts_ist], method="nearest")[0]
                        price_y = float(_ps.iloc[idx])
                    _ax.scatter(ts_ist, price_y, marker=marker, color=_ec, s=120, zorder=6)
                    _ax.annotate(
                        f"{price_y:.1f}",
                        xy=(ts_ist, price_y),
                        xytext=(0, 13 if marker == "^" else -16),
                        textcoords="offset points",
                        ha="center", fontsize=7.5, color=_ec,
                        fontweight="bold",
                    )
                    return price_y
                except Exception:
                    return None

            ep = _mark(entry_t_raw, entry_p_val, "^")
            _mark(exit_t_raw, exit_p_val, "v")

            # Exit reason annotation at exit point
            if exit_t_raw is not None and exit_reason and price_series is not None:
                try:
                    t_exit_ist = _ts_to_ist_naive(exit_t_raw)
                    if exit_p_val is not None and not pd.isna(float(exit_p_val)):
                        ey = float(exit_p_val)
                    else:
                        idx = price_series.index.get_indexer([t_exit_ist], method="nearest")[0]
                        ey = float(price_series.iloc[idx])
                    ax.annotate(
                        exit_reason,
                        xy=(t_exit_ist, ey),
                        xytext=(8, 0),
                        textcoords="offset points",
                        fontsize=6.5, color="#ffcc80",
                        va="center",
                    )
                except Exception:
                    pass

            # SL full-width horizontal dashed line with text
            if sl_p_val is not None:
                try:
                    sl_val = float(sl_p_val)
                    if not pd.isna(sl_val) and sl_val > 0:
                        ax.axhline(sl_val, color="#ff5252", linewidth=1.2,
                                   linestyle="dashed", zorder=4)
                        ax.text(0.01, sl_val, f" SL {sl_val:.1f}",
                                transform=ax.get_yaxis_transform(),
                                fontsize=7, color="#ff5252", va="top")
                except Exception:
                    pass

            ax.legend(fontsize=7, labelcolor="#cccccc", facecolor="#16213e",
                      edgecolor="#444466", loc="upper right")

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as chart_exc:
        print(f"[Chart warning] Could not generate chart: {chart_exc}")
        return None


def export_outputs(trades: pd.DataFrame, day: pd.DataFrame, ce_symbol: str, pe_symbol: str, run_mode: str) -> None:
    trades.attrs["market_context"] = build_market_context(
        day,
        prev_high=safe_float(day.attrs.get("nifty_prev_high", 0.0), 0.0),
        prev_low=safe_float(day.attrs.get("nifty_prev_low", 0.0), 0.0),
        prev_close=safe_float(day.attrs.get("nifty_prev_close", 0.0), 0.0),
    )

    if EXPORT_EXCEL:
        excel_file = build_backtest_excel_file_path(TRADE_DATE, ce_symbol, pe_symbol)
        ensure_parent_dir(excel_file)

        # Convert timezone-aware datetimes to naive for Excel compatibility
        trades_excel = trades.copy()
        day_excel = day.copy()
        summary_df = build_backtest_summary_df(trades_excel, run_mode, day_excel)
        
        for df in [trades_excel, day_excel]:
            for col in df.columns:
                if pd.api.types.is_datetime64_any_dtype(df[col]):
                    if df[col].dt.tz is not None:
                        df[col] = df[col].dt.tz_localize(None)
        
        chart_buf = _build_chart_bytes(trades, day, str(TRADE_DATE))
        with pd.ExcelWriter(excel_file, engine="openpyxl") as writer:
            summary_df.to_excel(writer, sheet_name="summary", index=False)
            trades_excel.to_excel(writer, sheet_name="trades", index=False)
            day_excel.to_excel(writer, sheet_name="debug_merged", index=False)
            if chart_buf is not None:
                try:
                    from openpyxl.drawing.image import Image as XlImage
                    chart_ws = writer.book.create_sheet("chart")
                    chart_ws.add_image(XlImage(chart_buf), "A1")
                except Exception as embed_exc:
                    print(f"[Chart warning] Could not embed chart in Excel: {embed_exc}")


def print_summary(trades: pd.DataFrame, run_mode: str, ce_symbol: str, pe_symbol: str) -> None:
    print("========== RESULT ==========")
    print(f"Run mode: {run_mode}")
    if run_mode in {"PAPERTRADE_LIVE", "LIVE_ORDER"}:
        print(f"Fresh count on restart: {PAPERTRADE_FRESH_COUNT_ON_RESTART}")
        if run_mode == "LIVE_ORDER":
            print(f"Live orders enabled  : {ENABLE_LIVE_ORDERS}")
            print(f"Order product        : {LIVE_ORDER_PRODUCT}")
            print(f"Cover order entry    : {ENABLE_COVER_ORDER_ENTRY}")
            print(f"Emergency SL order   : {ENABLE_EMERGENCY_SL_ORDER}")
            print(f"Emergency SL points  : {FIXED_SL_POINTS}")
    print(f"MTM guard enabled   : {ENABLE_MTM_GUARD}")
    print(f"Max concurrent trades : {MAX_CONCURRENT_TRADES}")
    print(f"Max concurrent CE trades : {MAX_CONCURRENT_TRADES_CE}")
    print(f"Max concurrent PE trades : {MAX_CONCURRENT_TRADES_PE}")
    print(f"No new entry after  : {NO_NEW_ENTRY_AFTER} (applies in both MTM and MAX_TRADES modes)")
    print(f"Daily profit target : {DAILY_PROFIT_TARGET_RS:.2f}")
    print(f"Daily loss limit    : {DAILY_LOSS_LIMIT_RS:.2f}")
    if ENABLE_MTM_GUARD:
        print("Trade stop basis    : MTM_ONLY")
    else:
        print(f"Max trades allowed  : {MAX_TRADES}")
        print("Trade stop basis    : MTM_AND_MAX_TRADES")

    if trades.empty:
        print("No trades found for the day with current filters.")
        print("Try reducing ENTRY_SCORE_THRESHOLD, VWAP_DISTANCE_PCT, or MTF_STRICTNESS.")
        print("Total P&L (points): 0.00")
        print("Total P&L (Rs): 0.00")
    else:
        total_points = float(trades["pnl_points"].sum())
        total_rs = float(trades["pnl_rs"].sum())
        win_trades = int((trades["pnl_rs"] > 0).sum())
        loss_trades = int((trades["pnl_rs"] <= 0).sum())
        guard_stop_reason = trades.attrs.get("guard_stop_reason")
        if not guard_stop_reason and not trades.empty and "exit_reason" in trades.columns:
            last_exit_reason = trades.iloc[-1].get("exit_reason")
            if last_exit_reason:
                guard_stop_reason = str(last_exit_reason)

        print(trades.to_string(index=False))
        print("---------- SUMMARY ----------")
        print(f"Total trades       : {len(trades)}")
        print(f"Winning trades     : {win_trades}")
        print(f"Losing/flat trades : {loss_trades}")
        print(f"Total P&L (points) : {total_points:.2f}")
        print(f"Total P&L (Rs)     : {total_rs:.2f}")
        print(f"Net P&L (Rs)       : {total_rs:.2f}")
        if guard_stop_reason:
            print(f"Trading stopped by : {guard_stop_reason}")

    market_ctx = trades.attrs.get("market_context", {}) if hasattr(trades, "attrs") else {}
    if market_ctx:
        print("----- MARKET CONTEXT -----")
        print(
            f"Gap: {market_ctx.get('gap_type', '')} "
            f"({market_ctx.get('gap_points', '')} pts, {market_ctx.get('gap_pct', '')}%) | "
            f"CPR: {market_ctx.get('cpr_type', '')} "
            f"(Pivot={market_ctx.get('cpr_pivot', '')}, BC={market_ctx.get('cpr_bc', '')}, "
            f"TC={market_ctx.get('cpr_tc', '')}, Width%={market_ctx.get('cpr_width_pct', '')})"
        )

    print("Saved files:")
    if EXPORT_EXCEL:
        if run_mode == "PAPERTRADE_LIVE":
            papertrade_excel_file = trades.attrs.get("papertrade_excel_file")
            if not papertrade_excel_file:
                # Fallback for early exits (e.g., outside market hours) where export may not run.
                papertrade_excel_file = build_papertrade_excel_file_path(
                    datetime.now(IST).strftime("%Y-%m-%d"),
                    ce_symbol,
                    pe_symbol,
                )
            print(f"- {papertrade_excel_file}")
        elif run_mode == "LIVE_ORDER":
            live_excel_file = trades.attrs.get("live_excel_file")
            if not live_excel_file:
                # Fallback for early exits (e.g., outside market hours) where export may not run.
                live_excel_file = build_livetrade_excel_file_path(
                    datetime.now(IST).strftime("%Y-%m-%d"),
                    ce_symbol,
                    pe_symbol,
                )
            print(f"- {live_excel_file}")
        else:
            print(f"- {build_backtest_excel_file_path(TRADE_DATE, ce_symbol, pe_symbol)}")


def get_open_positions_by_side(live_state: Dict) -> Dict:
    counts = {"CE": 0, "PE": 0}
    for _, broker_entry in (live_state.get("broker_positions") or {}).items():
        qty = safe_int(broker_entry.get("broker_open_qty"), 0)
        side = str(broker_entry.get("side") or "").upper()
        if qty > 0 and side in counts:
            counts[side] += 1
    return counts


def get_running_script_name() -> str:
    # PyInstaller or similar frozen executables
    if getattr(sys, "frozen", False):
        return os.path.basename(sys.executable)
    # Normal script invocation: sys.argv[0] usually contains the script path
    argv0 = sys.argv[0] if len(sys.argv) > 0 and sys.argv[0] else ""
    if argv0 and argv0 not in ("", "-c", "<string>"):
        return os.path.basename(argv0)
    # Fallback to __file__ if available (not in interactive shells)
    try:
        return os.path.basename(__file__)
    except NameError:
        return ""


def emit_startup_banner() -> None:
    run_id = f"{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
    host = os.getenv("HOSTNAME") or os.getenv("COMPUTERNAME") or "unknown-host"
    python_ver = sys.version.split()[0]
    script_name = get_running_script_name() or "unknown-script"
    started_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")
    banner_trade_date = datetime.now(IST).strftime("%Y-%m-%d") if RUN_MODE in {"PAPERTRADE_LIVE", "LIVE_ORDER"} else TRADE_DATE
    print(
        "[RUN BANNER] "
        f"run_id={run_id} "
        f"script={script_name} "
        f"version={LOG_IDENTIFIER or 'default'} "
        f"mode={RUN_MODE} "
        f"trade_date={banner_trade_date} "
        f"host={host} "
        f"pid={os.getpid()} "
        f"python={python_ver} "
        f"cwd={os.getcwd()} "
        f"started_ist={started_ist}"
    )

# MAIN EXECUTION
# =========================
RUN_MODE = normalize_run_mode(RUN_MODE)

def main() -> None:
    # These are read as module globals elsewhere: CE_SYMBOL/PE_SYMBOL by
    # run_single_day_test and _build_chart_bytes, kite via globals().get("kite")
    # in run_single_day_test's strike switching.
    global CE_SYMBOL, PE_SYMBOL, kite
    try:
        if not acquire_live_process_lock(RUN_MODE):
            raise SystemExit(0)
        emit_startup_banner()
        print("Connecting to Kite...")
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(ACCESS_TOKEN)
        validate_kite_login_on_start(kite, RUN_MODE)

        print(f"Running mode: {RUN_MODE}")
        print_live_order_preflight()

        if AUTO_SELECT_OPTION_SYMBOLS:
            if RUN_MODE in {"PAPERTRADE_LIVE", "LIVE_ORDER"}:
                now_ist = datetime.now(IST)
                if now_ist.weekday() < 5:
                    market_open_today = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
                    market_close_today = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
                    if now_ist < market_open_today:
                        print("Live mode + auto-select: waiting for market open before resolving ATM symbols...")
                        now_ist = wait_until_market_open(check_interval_seconds=30)
                    elif now_ist > market_close_today:
                        print("Live mode + auto-select: market already closed; resolving symbols with current spot snapshot.")

                print("Auto-selecting ATM CE/PE symbols...")
                print(f"Using underlying token: {UNDERLYING_TOKEN}")
                live_ref_trade_date = str(_cli_args.date or now_ist.strftime("%Y-%m-%d"))

                spot_ltp = 0.0
                if RUN_MODE in {"PAPERTRADE_LIVE", "LIVE_ORDER"}:
                    try:
                        spot_ref_df = fetch_history(kite, UNDERLYING_TOKEN, live_ref_trade_date, BASE_INTERVAL)
                        if not spot_ref_df.empty:
                            ref = str(AUTO_SELECT_SPOT_REF or "").lower()
                            if ref == "open" and "open" in spot_ref_df.columns:
                                spot_ltp = float(spot_ref_df["open"].dropna().iloc[0])
                            elif ref == "last":
                                if "last_price" in spot_ref_df.columns and not spot_ref_df["last_price"].dropna().empty:
                                    spot_ltp = float(spot_ref_df["last_price"].dropna().iloc[-1])
                                elif "close" in spot_ref_df.columns and not spot_ref_df["close"].dropna().empty:
                                    spot_ltp = float(spot_ref_df["close"].dropna().iloc[-1])
                            elif len(ref) == 5 and ref.count(":") == 1:
                                _hh, _mm = [int(x) for x in ref.split(":")]
                                _ts = pd.to_datetime(spot_ref_df["ts"], errors="coerce") if "ts" in spot_ref_df.columns else pd.Series([], dtype="datetime64[ns]")
                                if not _ts.empty:
                                    if getattr(_ts.dt, "tz", None) is None:
                                        _ts = _ts.dt.tz_localize(IST)
                                    else:
                                        _ts = _ts.dt.tz_convert(IST)
                                    _mask = (_ts.dt.hour == _hh) & (_ts.dt.minute == _mm)
                                else:
                                    _mask = pd.Series(False, index=spot_ref_df.index)
                                if _mask.any():
                                    _idx = _mask[_mask].index[0]
                                    spot_ltp = float(spot_ref_df.at[_idx, "close"])
                                elif "close" in spot_ref_df.columns and not spot_ref_df["close"].dropna().empty:
                                    spot_ltp = float(spot_ref_df["close"].dropna().iloc[0])
                            elif ref == "signal_close":
                                completed_spot = spot_ref_df
                                if "ts" in spot_ref_df.columns:
                                    spot_ts = pd.to_datetime(spot_ref_df["ts"], errors="coerce")
                                    if getattr(spot_ts.dt, "tz", None) is None:
                                        spot_ts = spot_ts.dt.tz_localize(IST)
                                    else:
                                        spot_ts = spot_ts.dt.tz_convert(IST)
                                    completed_spot = spot_ref_df.loc[
                                        spot_ts < pd.Timestamp(floor_to_minute(now_ist))
                                    ]
                                valid_spot = completed_spot.dropna(subset=["close"]).sort_values("ts")
                                if not valid_spot.empty:
                                    latest_spot = valid_spot.iloc[-1]
                                    spot_ltp = safe_float(latest_spot.get("close"), 0.0)
                            else:
                                if "close" in spot_ref_df.columns and not spot_ref_df["close"].dropna().empty:
                                    spot_ltp = float(spot_ref_df["close"].dropna().iloc[0])
                        if spot_ltp > 0:
                            print(
                                f"Spot LTP resolved from historical ref '{AUTO_SELECT_SPOT_REF}' "
                                f"for {live_ref_trade_date}: {spot_ltp}"
                            )
                    except Exception as _hist_ref_exc:
                        print(f"[LIVE AUTO-SELECT REF WARNING] {_hist_ref_exc}")

                if spot_ltp <= 0:
                    spot_ltp = get_spot_ltp(kite, UNDERLYING_NAME)
                    print(f"Spot LTP resolved from live quote: {spot_ltp}")

                CE_SYMBOL, PE_SYMBOL, ce_token, pe_token = auto_select_ce_pe_symbols(kite, spot_ltp, OPTION_EXCHANGE, selection_time=None)
                print(f"Auto-selected CE: {CE_SYMBOL} ({ce_token})")
                print(f"Auto-selected PE: {PE_SYMBOL} ({pe_token})")
            else:
                # For backtests we defer auto-selection until historical spot data is fetched
                print("Auto-selecting ATM CE/PE symbols deferred until historical spot data is loaded for backtest.")
        else:
            print("Resolving option tokens...")
            ce_token = resolve_token_from_instruments(kite, CE_SYMBOL, OPTION_EXCHANGE)
            pe_token = resolve_token_from_instruments(kite, PE_SYMBOL, OPTION_EXCHANGE)

        if RUN_MODE in {"PAPERTRADE_LIVE", "LIVE_ORDER"}:
            if RUN_MODE == "LIVE_ORDER" and not ENABLE_LIVE_ORDERS:
                raise ValueError("For RUN_MODE = LIVE_ORDER, set ENABLE_LIVE_ORDERS = True after validating paper trades.")
            trades = run_live_session(kite, CE_SYMBOL, PE_SYMBOL, ce_token, pe_token, execution_mode=RUN_MODE)
            print_summary(trades, RUN_MODE, CE_SYMBOL, PE_SYMBOL)
        else:
            print(f"Fetching 1-minute underlying history for {UNDERLYING_NAME} ({UNDERLYING_TOKEN}) on {TRADE_DATE}...")
            try:
                spot = fetch_history(kite, UNDERLYING_TOKEN, TRADE_DATE, BASE_INTERVAL)

                # If AUTO_SELECT_OPTION_SYMBOLS is enabled for backtest, pick ATM using historical spot close
                if AUTO_SELECT_OPTION_SYMBOLS and not (
                    RUN_MODE == "BACKTEST"
                    and BACKTEST_OPTION_DATA_SOURCE == "DOWNLOADED"
                ):
                    try:
                        hist_spot_ts = None
                        # Determine historical spot LTP based on configured reference: open/close/last or specific minute "HH:MM"
                        if not spot.empty:
                            ref = str(AUTO_SELECT_SPOT_REF or "").lower()
                            # Specific minute format like "09:16"
                            if len(ref) == 5 and ref.count(":") == 1 and ref.split(":")[0].isdigit():
                                try:
                                    hh, mm = [int(x) for x in ref.split(":")]
                                    # Ensure spot['ts'] is datetime aware in IST
                                    s_ts = pd.to_datetime(spot["ts"], errors="coerce")
                                    if s_ts.dt.tz is None:
                                        try:
                                            s_ts = s_ts.dt.tz_localize(IST)
                                        except Exception:
                                            s_ts = s_ts
                                    else:
                                        try:
                                            s_ts = s_ts.dt.tz_convert(IST)
                                        except Exception:
                                            s_ts = s_ts
                                    target_ts = pd.Timestamp(f"{TRADE_DATE} {hh:02d}:{mm:02d}", tz=IST)
                                    # find exact match
                                    mask = s_ts.dt.time == target_ts.time()
                                    if mask.any():
                                        idx = mask[mask].index[-1]
                                        if "close" in spot.columns and not pd.isna(spot.at[idx, "close"]):
                                            hist_spot_ltp = float(spot.at[idx, "close"])
                                            hist_spot_ts = s_ts.iloc[idx]
                                        elif "last_price" in spot.columns and not pd.isna(spot.at[idx, "last_price"]):
                                            hist_spot_ltp = float(spot.at[idx, "last_price"])
                                            hist_spot_ts = s_ts.iloc[idx]
                                        else:
                                            hist_spot_ltp = float(spot["close"].dropna().iloc[-1]) if "close" in spot.columns else get_spot_ltp(kite, UNDERLYING_NAME)
                                            hist_spot_ts = s_ts.dropna().iloc[-1] if not s_ts.dropna().empty else None
                                    else:
                                        # nearest timestamp fallback
                                        diffs = (s_ts - target_ts).abs()
                                        nearest_idx = diffs.idxmin()
                                        if "close" in spot.columns and not pd.isna(spot.at[nearest_idx, "close"]):
                                            hist_spot_ltp = float(spot.at[nearest_idx, "close"])
                                            hist_spot_ts = s_ts.iloc[nearest_idx]
                                        elif "last_price" in spot.columns and not pd.isna(spot.at[nearest_idx, "last_price"]):
                                            hist_spot_ltp = float(spot.at[nearest_idx, "last_price"])
                                            hist_spot_ts = s_ts.iloc[nearest_idx]
                                        else:
                                            hist_spot_ltp = float(spot["close"].dropna().iloc[-1]) if "close" in spot.columns else get_spot_ltp(kite, UNDERLYING_NAME)
                                            hist_spot_ts = s_ts.dropna().iloc[-1] if not s_ts.dropna().empty else None
                                except Exception:
                                    hist_spot_ltp = get_spot_ltp(kite, UNDERLYING_NAME)
                                    hist_spot_ts = None
                            else:
                                try:
                                    if ref == "open" and "open" in spot.columns:
                                        hist_spot_ltp = float(spot["open"].dropna().iloc[0])
                                        hist_spot_ts = spot.iloc[0].get("ts")
                                    elif ref == "last":
                                        if "last_price" in spot.columns and not spot["last_price"].dropna().empty:
                                            hist_spot_ltp = float(spot["last_price"].dropna().iloc[-1])
                                            hist_spot_ts = spot.iloc[-1].get("ts")
                                        elif "close" in spot.columns and not spot["close"].dropna().empty:
                                            hist_spot_ltp = float(spot["close"].dropna().iloc[-1])
                                            hist_spot_ts = spot.iloc[-1].get("ts")
                                        else:
                                            hist_spot_ltp = get_spot_ltp(kite, UNDERLYING_NAME)
                                            hist_spot_ts = None
                                    else:  # default to close
                                        hist_spot_ltp = float(spot["close"].dropna().iloc[-1]) if "close" in spot.columns else get_spot_ltp(kite, UNDERLYING_NAME)
                                        hist_spot_ts = spot.iloc[-1].get("ts") if "close" in spot.columns and not spot["close"].dropna().empty else None
                                except Exception:
                                    hist_spot_ltp = get_spot_ltp(kite, UNDERLYING_NAME)
                                    hist_spot_ts = None
                        else:
                            hist_spot_ltp = get_spot_ltp(kite, UNDERLYING_NAME)
                            hist_spot_ts = None
                        print(f"Auto-selecting ATM CE/PE symbols using historical spot ({AUTO_SELECT_SPOT_REF}): {hist_spot_ltp}")
                        CE_SYMBOL, PE_SYMBOL, ce_token, pe_token = auto_select_ce_pe_symbols(
                            kite,
                            hist_spot_ltp,
                            OPTION_EXCHANGE,
                            selection_time=hist_spot_ts,
                        )
                        print(f"Auto-selected CE: {CE_SYMBOL} ({ce_token})")
                        print(f"Auto-selected PE: {PE_SYMBOL} ({pe_token})")
                    except Exception as auto_exc:
                        print(f"Auto-select failed for backtest: {auto_exc}. Falling back to configured CE/PE.")

                if RUN_MODE == "BACKTEST" and BACKTEST_OPTION_DATA_SOURCE == "DOWNLOADED":
                    CE_SYMBOL, PE_SYMBOL = select_downloaded_option_symbols(spot, TRADE_DATE)
                    print(
                        f"Using downloaded option contracts from {OPTION_DAILY_DIR}: "
                        f"CE={CE_SYMBOL}, PE={PE_SYMBOL}"
                    )
                    ce = load_downloaded_option_history(CE_SYMBOL, TRADE_DATE)
                    pe = load_downloaded_option_history(PE_SYMBOL, TRADE_DATE)
                else:
                    if RUN_MODE == "BACKTEST":
                        print("Using Kite historical option data for backtest.")
                    print(f"Fetching CE history for {CE_SYMBOL} ({ce_token})...")
                    ce = fetch_history(kite, ce_token, TRADE_DATE, BASE_INTERVAL)

                    print(f"Fetching PE history for {PE_SYMBOL} ({pe_token})...")
                    pe = fetch_history(kite, pe_token, TRADE_DATE, BASE_INTERVAL)
            except ValueError as exc:
                if "No history returned for token" in str(exc):
                    print(f"Market has not opened on {TRADE_DATE}. No historical data available yet.")
                    raise SystemExit(0)
                raise

            # Fetch previous trading day OHLC for CPR + gap context in logs/summary
            try:
                prev_ohlc = fetch_prev_trading_day_ohlc(kite, UNDERLYING_TOKEN, TRADE_DATE)
            except Exception:
                prev_ohlc = {}
            today_open = safe_float(spot.iloc[0].get("open"), 0.0) if spot is not None and not spot.empty else 0.0
            set_mtf_interval_from_gap(
                calculate_gap_pct(today_open, prev_ohlc.get("close")),
                source="backtest",
            )

            print(f"Preparing 1-minute + {MTF_INTERVAL} merged dataset...")
            day = prepare_merged_day(spot, ce, pe)

            day.attrs["nifty_prev_close"] = safe_float(prev_ohlc.get("close"), 0.0)
            day.attrs["nifty_prev_high"] = safe_float(prev_ohlc.get("high"), 0.0)
            day.attrs["nifty_prev_low"] = safe_float(prev_ohlc.get("low"), 0.0)

            trades = run_single_day_test(day, RUN_MODE, ce_symbol_override=CE_SYMBOL, pe_symbol_override=PE_SYMBOL)
            export_outputs(trades, day, CE_SYMBOL, PE_SYMBOL, RUN_MODE)
            print_summary(trades, RUN_MODE, CE_SYMBOL, PE_SYMBOL)
    except PermissionException as exc:
        msg = str(exc)
        print("Kite order placement blocked by API permission settings.")
        print(f"Details: {msg}")
        print("Fix: add this machine's current public IP in Kite developer console allowlist and retry.")
        print("Kite console: https://developers.kite.trade")
        raise SystemExit(2)
    except InputException as exc:
        msg = str(exc)
        if "market protection" in msg.lower():
            print("Kite rejected MARKET order due to market-protection policy.")
            print(f"Details: {msg}")
            print("This script now retries with aggressive LIMIT fallback when enabled.")
            print("If still failing, set LIVE_ORDER_ORDER_TYPE = 'LIMIT' and retry.")
        else:
            print("Kite input validation error.")
            print(f"Details: {msg}")
        raise SystemExit(3)


# Started by the launchers (scripts/v28_strategy_main.py, scripts/v30_strategy_main.py),
# which choose the variant and then call main().
