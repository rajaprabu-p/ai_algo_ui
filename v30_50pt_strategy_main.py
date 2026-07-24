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
import sys
import os
import io

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
# - TRAILING_SL_HIT exit reason added to differentiate between regular SL hits and trailing SL hits.
# - autoselect option symbols logic now selects the nearest OTM strikes based on the spot price at app start, rounded to the nearest configured step (e.g. 50 points), for better adaptability across different market conditions and expiries.

def load_env_file(env_path: str = None) -> None:
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
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
            if key:
                os.environ[key] = value


load_env_file()

UTC = pytz.UTC
# =========================
# COMMAND-LINE OVERRIDES
# (parsed before USER INPUTS so they can override config values)
# Usage examples:
#   py v30_50pt_strategy_main.py --date 2026-06-09
#   py v30_50pt_strategy_main.py --date 2026-06-09 --mode BACKTEST
#   py v30_50pt_strategy_main.py --mode PAPERTRADE_LIVE --after-hours-replay --date 2026-07-23
# =========================
import argparse as _argparse
_cli = _argparse.ArgumentParser(add_help=False)
_cli.add_argument("date_pos", nargs="?", default=None, help="Trade date YYYY-MM-DD (positional)")
_cli.add_argument("--date", default=None, help="Trade date YYYY-MM-DD (overrides TRADE_DATE)")
_cli.add_argument("--mode", default=None, help="Run mode: BACKTEST | PAPERTRADE_LIVE | LIVE_ORDER")
_cli.add_argument("--after-hours-replay", action="store_true", help="Allow PAPERTRADE_LIVE to replay the completed day after market close")
_cli_args, _ = _cli.parse_known_args()
# positional arg wins if --date not supplied
if _cli_args.date is None and _cli_args.date_pos is not None:
    _cli_args.date = _cli_args.date_pos

# =========================
# USER INPUTS
# =========================

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
AUTO_SELECT_SPOT_REF = "09:15"  # Use 09:15 candle close for backtest ATM selection (matches live market open; avoids live-quote fallback drift)
AUTO_SELECT_REFRESH_INTERVAL_MINUTES = 2  # In PAPERTRADE_LIVE/LIVE_ORDER, re-select ATM symbols every N minutes when no trade is running

# Execution mode
RUN_MODE = _cli_args.mode if _cli_args.mode else "PAPERTRADE_LIVE"       # BACKTEST, PAPERTRADE_LIVE, or LIVE_ORDER
AFTER_HOURS_PAPER_REPLAY = bool(_cli_args.after_hours_replay)
LOG_IDENTIFIER = "v30_main"  # Optional identifier to append to log filenames for easy identification (e.g. hostname, run-id)
EXPORT_EXCEL = True
PAPERTRADE_FRESH_COUNT_ON_RESTART = True  # True: allow fresh entries only from app start time when PAPERTRADE_LIVE starts mid-session
READ_HISTORY_FROM_DAY_START = True          # Keep full-day candles for indicator warmup even if app starts mid-session
ALLOW_ENTRY_ONLY_AFTER_APP_START = True     # In PAPERTRADE_LIVE/LIVE_ORDER, do not allow a fresh entry before the app start minute

# Auto-select diagnostics dedupe keys to avoid repetitive logs on each refresh cycle.
_LAST_AUTOSELECT_EXPIRY_LOG_KEY = None
_LAST_AUTOSELECT_SYMBOLS_LOG_KEY = None

# Guard mode
# If ENABLE_MTM_GUARD = True  -> use daily profit/loss MTM guard + MAX_TRADES, whichever hits first
# If ENABLE_MTM_GUARD = False -> use only MAX_TRADES
ENABLE_MTM_GUARD = True
MAX_TRADES = 2   # Used only when ENABLE_MTM_GUARD = False, along with MTM guard

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
DAILY_LOSS_LIMIT_RS = -3500 * LOTS
ENABLE_PROFIT_LOCK = True
PROFIT_LOCK_TRIGGER_RS = 2500 * LOTS
PROFIT_LOCK_PERCENT = 0.70
PROFIT_LOCK_AFTER_TRADE_NO = 1  # activate after first completed trade
ENABLE_TRAILING_PROFIT_EXIT = True  # Enable trailing profit after first 50-point profit
#sym:TRAILING_PROFIT_STAGE1 - used before TRAILING_PROFIT_STAGE2_TIME
TRAILING_PROFIT_STAGE1 = [(50,45),(60,55),(70,65),(80,75),(90,85),(100,95)]
#sym:TRAILING_PROFIT_STAGE2 - used at/after TRAILING_PROFIT_STAGE2_TIME (can be tighter to lock gains later in day)
TRAILING_PROFIT_STAGE2 = [(30,25),(40,35),(50,45),(60,55),(70,65),(80,75),(90,85),(100,95)]
TRAILING_PROFIT_STAGE2_TIME = "11:00"  # HH:MM (IST); at or after this time, switch to TRAILING_PROFIT_STAGE2
LIVE_REENTRY_COOLDOWN_MINUTES = 1  # In LIVE_ORDER mode, wait this many minutes after an exit before allowing the next fresh entry

# Smart entry engine / execution safety
SMART_ENTRY_ENGINE = False
MAX_ENTRY_SLIPPAGE_POINTS_ALLOWED = 8.0
SKIP_ENTRY_IF_TOO_MUCH_SLIPPAGE = False
MAX_CANDLE_MOVE_PCT = 0.12
BLOCK_SAME_SYMBOL_REENTRY_WITHIN_MINUTES = 5
SAME_SYMBOL_MIN_REENTRY_DROP_POINTS = 20.0  # After a same-symbol exit, only re-enter if option price is at least this many points below the previous exit price (0 = disabled)
ENTRY_CONFIRM_DELAY_SECONDS = 2
SMART_ENTRY_USE_LTP = True
SIMULATE_LIVE_EXECUTION_IN_PAPERTRADE = True
PAPER_ENTRY_SLIPPAGE_MODE = "FIXED"   # FIXED / PCT / NONE
PAPER_ENTRY_SLIPPAGE_POINTS = 2.0
PAPER_ENTRY_SLIPPAGE_PCT = 0.005
PAPER_EXIT_SLIPPAGE_MODE = "FIXED"    # FIXED / PCT / NONE
PAPER_EXIT_SLIPPAGE_POINTS = 1.5
PAPER_EXIT_SLIPPAGE_PCT = 0.004
SIMULATE_INTRAMINUTE_EMERGENCY_SL = True
SIMULATE_INTRAMINUTE_TARGET_HIT = True
MAX_ENTRY_SIGNAL_AGE_SECONDS = 300

# Runtime visibility / heartbeat
HEARTBEAT_ENABLED = False
HEARTBEAT_INTERVAL_SECONDS = 30
TICK_STALL_WARN_SECONDS = 20
TICK_STALL_RESUBSCRIBE_SECONDS = 60
TICK_STALL_RESUBSCRIBE_COOLDOWN_SECONDS = 45
TICK_STALL_RECONNECT_SECONDS = 150
TICK_STALL_RECONNECT_COOLDOWN_SECONDS = 120
IDLE_STATUS_INTERVAL_SECONDS = 30
ENTRY_DIAGNOSTICS = False

# Live order execution
ENABLE_LIVE_ORDERS = True   # Safety switch: must be True and RUN_MODE = "LIVE_ORDER" to place real orders
LIVE_ORDER_PRODUCT = "MIS"  # MIS for intraday, NRML for carry
LIVE_ORDER_VARIETY = "regular"
LIVE_ORDER_ORDER_TYPE = "MARKET"
ENABLE_MARKET_PROTECTION = True
LIVE_ORDER_MARKET_PROTECTION = -1  # 0=no protection, 1-100=custom %, -1=automatic protection by Kite
LIVE_ORDER_TRIGGER_PRICE_BUFFER = 0.05  # trigger buffer for broker-side emergency SL order
LIVE_ORDER_TAG = "MTF_LIVE"
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
TRACKING_START = "09:15"  # Start tracking for entries only after this time (to avoid opening volatility)   
NO_NEW_ENTRY_AFTER = "12:00"
FORCE_EXIT = "15:00"

# 1-minute entry model
EMA_LEN = 20
VOL_LOOKBACK = 10
SWING_LOOKBACK = 5
OPTION_CONFIRM_BARS = 1
VWAP_DISTANCE_PCT = 0.10
VOL_MULT = 1.05
ENTRY_SCORE_THRESHOLD = 4
OPPOSITE_EXIT_SCORE = 5

# 5-minute trend filter
MTF_INTERVAL = "5min"
MTF_EMA_LEN = 20
MTF_STRICTNESS = 2   # 1 = looser, 2 = balanced, 3 = stricter

# Risk / trade management
FIXED_SL_POINTS = 50
BREAKEVEN_TRIGGER = 0.20
TRAIL_TRIGGER = 0.40
TRAIL_PCT = 0.10
MIN_PREMIUM = 10.0

IST = pytz.timezone("Asia/Kolkata")


# =========================
# HELPERS
# =========================
def normalize_run_mode(mode: str) -> str:
    mode = str(mode).strip().upper()
    valid = {"BACKTEST", "PAPERTRADE_LIVE", "LIVE_ORDER"}
    if mode not in valid:
        raise ValueError(f"RUN_MODE must be one of {valid}, got: {mode}")
    return mode


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
        if value is None or (isinstance(value, float) and np.isnan(value)):
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


def get_entry_stop_price(entry_price: float) -> float:
    """Calculate the initial entry stop price using fixed points only."""
    entry_price = safe_float(entry_price, 0.0)
    if entry_price <= 0:
        return 0.0

    fixed_sl_points = safe_float(FIXED_SL_POINTS, 0.0)
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


def get_order_history(kite: KiteConnect, order_id: str) -> List[Dict]:
    try:
        hist = kite.order_history(order_id)
        return hist if isinstance(hist, list) else []
    except Exception:
        return []


def get_order_snapshot(kite: KiteConnect, order_id: str) -> Dict:
    orders = kite.orders()
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


def cancel_exit_orders(kite: KiteConnect, broker_entry: Dict) -> None:
    if not broker_entry:
        return
    for key in ('emergency_sl_order_id', 'exit_order_id'):
        oid = str(broker_entry.get(key) or '').strip()
        if oid:
            cancel_order_if_open(kite, oid)


def get_net_position_qty(kite: KiteConnect, tradingsymbol: str, exchange: str = OPTION_EXCHANGE, product: str = LIVE_ORDER_PRODUCT) -> int:
    try:
        positions = kite.positions() or {}
    except Exception:
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


def get_dynamic_emergency_sl_points(entry_price: float) -> float:
    return float(FIXED_SL_POINTS)

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


def place_emergency_sl_order(kite: KiteConnect, tradingsymbol: str, qty: int, entry_price: float) -> Dict:
    if (not ENABLE_LIVE_ORDERS) or (not ENABLE_EMERGENCY_SL_ORDER):
        return {}
    sl_points = get_dynamic_emergency_sl_points(entry_price)
    sl_price = round(max(0.05, entry_price - sl_points), 1)
    trigger_price = round(max(0.05, sl_price + LIVE_ORDER_TRIGGER_PRICE_BUFFER), 1)
    order_id = kite.place_order(
        variety=LIVE_ORDER_VARIETY,
        exchange=OPTION_EXCHANGE,
        tradingsymbol=tradingsymbol,
        transaction_type=kite.TRANSACTION_TYPE_SELL,
        quantity=int(qty),
        product=LIVE_ORDER_PRODUCT,
        order_type='SL',
        price=sl_price,
        trigger_price=trigger_price,
        validity='DAY',
        tag=LIVE_ORDER_TAG,
    )
    return {
        'emergency_sl_order_id': order_id,
        'emergency_sl_price': sl_price,
        'emergency_sl_trigger_price': trigger_price,
        'emergency_sl_pct_used': sl_points,
    }


def sync_broker_position_state(kite: KiteConnect, broker_entry: Dict) -> Dict:
    if not broker_entry:
        return {}
    out = dict(broker_entry)
    option_symbol = str(out.get('option_symbol') or out.get('entry_order_symbol') or '')
    qty = safe_int(out.get('qty') or QTY, QTY)
    open_qty = get_net_position_qty(kite, option_symbol)
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
            orders = kite.orders()
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
        ctx["gap_pct"] = round(gap_pct, 2)
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
            cpr_type = "MODERATE"
        else:
            cpr_type = "WIDE"

        ctx["cpr_pivot"] = round(pivot, 2)
        ctx["cpr_bc"] = round(bc, 2)
        ctx["cpr_tc"] = round(tc, 2)
        ctx["cpr_width"] = round(cpr_width, 2)
        ctx["cpr_width_pct"] = round(cpr_width_pct, 2)
        ctx["cpr_type"] = cpr_type

    return ctx


def resolve_token_from_instruments(kite: KiteConnect, tradingsymbol: str, exchange: str = "NFO") -> int:
    instruments = kite.instruments(exchange)
    for inst in instruments:
        if inst.get("tradingsymbol") == tradingsymbol:
            return int(inst["instrument_token"])
    raise ValueError(f"Could not resolve token for {tradingsymbol} on {exchange}")


_instruments_cache: Dict[str, pd.DataFrame] = {}

def get_instruments_df(kite: KiteConnect, exchange: str = "NFO") -> pd.DataFrame:
    global _instruments_cache
    if exchange not in _instruments_cache:
        instruments = pd.DataFrame(kite.instruments(exchange))
        if "expiry" in instruments.columns:
            instruments["expiry"] = pd.to_datetime(instruments["expiry"], errors="coerce")
        _instruments_cache[exchange] = instruments
    return _instruments_cache[exchange].copy()


def round_to_step(value: float, step: int) -> int:
    return int(round(float(value) / step) * step)


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

    # 1) Try LTP using exchange:tradingsymbol keys only.
    for key in candidate_keys:
        try:
            ltp_response = kite.ltp(key)
            if isinstance(ltp_response, dict) and ltp_response:
                row = ltp_response.get(key) or next(iter(ltp_response.values()), None)
                if isinstance(row, dict) and row.get("last_price") not in (None, ""):
                    price = float(row["last_price"])
                    if price > 0:
                        return price
        except Exception as exc:
            print(f"[get_spot_ltp] ltp failed for {key}: {exc}")

    # 2) Try quote() with the same keys.
    for key in candidate_keys:
        try:
            quote_response = kite.quote(key)
            if isinstance(quote_response, dict) and quote_response:
                row = quote_response.get(key) or next(iter(quote_response.values()), None)
                if isinstance(row, dict) and row.get("last_price") not in (None, ""):
                    price = float(row["last_price"])
                    if price > 0:
                        return price
        except Exception as exc:
            print(f"[get_spot_ltp] quote failed for {key}: {exc}")

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


def auto_select_ce_pe_symbols(kite: KiteConnect, spot_ltp: float, exchange: str = "NFO", selection_time=None) -> tuple[str, str, int, int]:
    global _LAST_AUTOSELECT_EXPIRY_LOG_KEY, _LAST_AUTOSELECT_SYMBOLS_LOG_KEY
    instruments = get_instruments_df(kite, exchange)
    opts = instruments[(instruments["name"] == UNDERLYING_NAME) & (instruments["segment"].str.contains("NFO-OPT", na=False))].copy()
    if opts.empty:
        raise ValueError(f"No option instruments found for {UNDERLYING_NAME}")

    opts = opts.dropna(subset=["expiry", "strike", "tradingsymbol", "instrument_token"]).copy()
    opts["strike"] = pd.to_numeric(opts["strike"], errors="coerce")
    opts = opts.dropna(subset=["strike"])

    # Keep live expiry selection anchored to today's IST date so PAPERTRADE_LIVE
    # and LIVE_ORDER pick the same contracts even when --date is not supplied.
    if RUN_MODE in {"PAPERTRADE_LIVE", "LIVE_ORDER"}:
        reference_date = datetime.now(IST).date()
    else:
        # Backtests should keep using TRADE_DATE for historical consistency.
        try:
            reference_date = pd.to_datetime(TRADE_DATE).date()
        except Exception:
            reference_date = datetime.now(IST).date()
    opts = opts[opts["expiry"].dt.date >= reference_date].copy()

    def _is_today_or_tomorrow(dt_date, ref_date):
        return dt_date == ref_date or dt_date == (ref_date + timedelta(days=1))

    selected_expiry = None
    expiry_shifted_to_next = False
    if AUTO_SELECT_EXPIRY:
        expiry_dt = pd.to_datetime(AUTO_SELECT_EXPIRY).date()
        # If the configured expiry is today or tomorrow, use the next available expiry instead
        if _is_today_or_tomorrow(expiry_dt, reference_date):
            later = opts[opts["expiry"].dt.date > expiry_dt]["expiry"].dt.date
            if later.empty:
                raise ValueError(f"No later expiry available after {expiry_dt}")
            expiry_dt = later.min()
            expiry_shifted_to_next = True
        selected_expiry = expiry_dt
        opts = opts[opts["expiry"].dt.date == expiry_dt].copy()
        if opts.empty:
            raise ValueError(f"No option contracts found for expiry {expiry_dt}")
    else:
        nearest_expiry = opts["expiry"].dt.date.min()
        # If nearest expiry is today or tomorrow, pick the next available expiry
        if _is_today_or_tomorrow(nearest_expiry, reference_date):
            later = opts[opts["expiry"].dt.date > nearest_expiry]["expiry"].dt.date
            if not later.empty:
                expiry_shifted_to_next = True
                nearest_expiry = later.min()
        selected_expiry = nearest_expiry
        opts = opts[opts["expiry"].dt.date == nearest_expiry].copy()

    requested_expiry = str(AUTO_SELECT_EXPIRY).strip() if str(AUTO_SELECT_EXPIRY).strip() else "nearest"
    expiry_log_key = (str(RUN_MODE), str(reference_date), str(requested_expiry), str(selected_expiry), bool(expiry_shifted_to_next))
    if expiry_log_key != _LAST_AUTOSELECT_EXPIRY_LOG_KEY:
        print(
            f"[AUTO-SELECT EXPIRY] mode={RUN_MODE} reference_date={reference_date} "
            f"requested={requested_expiry} selected={selected_expiry} shifted_to_next={expiry_shifted_to_next}"
        )
        _LAST_AUTOSELECT_EXPIRY_LOG_KEY = expiry_log_key

    atm_strike = round_to_step(spot_ltp, AUTO_SELECT_STRIKE_STEP)
    opts["strike_diff"] = (opts["strike"] - atm_strike).abs()
    nearest_strike = opts.sort_values(["strike_diff", "strike"]).iloc[0]["strike"]
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
    symbols_log_key = (str(ce_symbol), int(ce_token), str(ce_expiry), str(pe_symbol), int(pe_token), str(pe_expiry))
    if symbols_log_key != _LAST_AUTOSELECT_SYMBOLS_LOG_KEY:
        _LAST_AUTOSELECT_SYMBOLS_LOG_KEY = symbols_log_key
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


def place_live_entry_order(kite: KiteConnect, side: str, ce_symbol: str, pe_symbol: str, qty: int, entry_price_hint: float = 0.0) -> Dict:
    if not ENABLE_LIVE_ORDERS:
        raise RuntimeError("ENABLE_LIVE_ORDERS must be True for LIVE_ORDER mode")
    tradingsymbol = ce_symbol if side == "CE" else pe_symbol

    if ENABLE_COVER_ORDER_ENTRY:
        if str(LIVE_ORDER_PRODUCT).upper() != "MIS":
            raise RuntimeError("Cover Order entry requires LIVE_ORDER_PRODUCT = 'MIS'")
        ref_entry_price = safe_float(entry_price_hint, 0.0)
        if ref_entry_price <= 0:
            raise RuntimeError("Cover Order entry requires a positive entry_price_hint")

        sl_points = get_dynamic_emergency_sl_points(ref_entry_price)
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


def register_symbol_exit_cooldown(live_state: Dict, option_symbol: str, exit_time) -> None:
    symbol = str(option_symbol or "").strip().upper()
    if not symbol or exit_time is None:
        return
    ts = pd.Timestamp(exit_time)
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    if ts.tzinfo is None:
        ts = IST.localize(ts)
    live_state.setdefault("symbol_last_exit_time", {})[symbol] = ts


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


def get_option_ltp_from_live_state(live_state: Dict, side: str) -> float:
    try:
        side_up = str(side or "").upper()
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


def should_block_smart_entry(live_state: Dict, pending_buy: Dict, execution_mode: str) -> tuple[bool, str, float]:
    signal_price = safe_float(pending_buy.get("entry_price"), 0.0)
    side = str(pending_buy.get("side") or "").upper()
    option_symbol = str(pending_buy.get("option_symbol") or "")
    signal_ts = pending_buy.get("entry_time")
    now_ts = datetime.now(IST)

    # Signal-age blocking is part of smart-entry filtering and should not
    # suppress baseline strategy entries when SMART_ENTRY_ENGINE is disabled.
    if SMART_ENTRY_ENGINE and is_signal_too_old(signal_ts, now_ts):
        return True, f"stale signal older than {safe_int(MAX_ENTRY_SIGNAL_AGE_SECONDS, 20)} second(s)", signal_price

    if is_same_symbol_reentry_blocked(live_state, option_symbol, now_ts):
        return True, f"same symbol cooldown active for {option_symbol}", signal_price

    actual_price = signal_price
    if SMART_ENTRY_ENGINE and SMART_ENTRY_USE_LTP:
        ltp = get_option_ltp_from_live_state(live_state, side)
        if ltp > 0:
            actual_price = ltp

    if signal_price > 0 and should_skip_entry_for_slippage(signal_price, actual_price):
        return True, f"entry slippage too high signal={signal_price:.2f} actual={actual_price:.2f}", actual_price

    spot_signal = safe_float(pending_buy.get("entry_spot"), 0.0)
    spot_now = get_spot_ltp_from_live_state(live_state)
    ref_spot = spot_now if spot_now > 0 else spot_signal
    if SMART_ENTRY_ENGINE and spot_signal > 0 and ref_spot > 0:
        move_pct = abs(ref_spot - spot_signal) / max(1.0, spot_signal)
        if move_pct > safe_float(MAX_CANDLE_MOVE_PCT, 0.12):
            return True, f"spot moved too much after signal ({move_pct * 100:.2f}%)", actual_price

    return False, "", actual_price


def safe_place_live_exit_order(kite: KiteConnect, side: str, ce_symbol: str, pe_symbol: str, qty: int, broker_entry: Optional[Dict] = None) -> Dict:
    broker_entry = broker_entry or {}
    if broker_entry.get("exit_in_progress"):
        return {"skipped": True, "skip_reason": "EXIT_ALREADY_IN_PROGRESS"}
    existing_exit_order_id = str(broker_entry.get("exit_order_id") or broker_entry.get("broker_exit_order_id") or "").strip()
    if existing_exit_order_id:
        snap = get_order_snapshot(kite, existing_exit_order_id)
        status = normalize_order_status(snap.get("status"))
        if status in {"OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED", "VALIDATION PENDING"}:
            broker_entry["exit_in_progress"] = True
            return {"skipped": True, "skip_reason": f"EXIT_ALREADY_PENDING:{status}", "exit_order_id": existing_exit_order_id}
        if status == "COMPLETE":
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

    tradingsymbol = str(broker_entry.get("option_symbol") or (ce_symbol if side == "CE" else pe_symbol))
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

    broker_entry["exit_in_progress"] = True
    try:
        order_meta = place_live_exit_order(kite, side, ce_symbol, pe_symbol, min(int(qty), abs(int(net_qty))), broker_entry=broker_entry)
        broker_entry.update(order_meta)
        return order_meta
    except Exception:
        broker_entry["exit_in_progress"] = False
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
    cooldown_minutes = max(0, safe_int(LIVE_REENTRY_COOLDOWN_MINUTES, 0))
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
    suffix = f"_{sanitize_filename_part(LOG_IDENTIFIER)}" if LOG_IDENTIFIER else ""
    file_name = f"backtest_{date_part}{suffix}.xlsx"
    return os.path.join("logs", file_name)


def build_papertrade_excel_file_path(trade_date: str, ce_symbol: str, pe_symbol: str) -> str:
    timestamp = get_timestamp_str()
    suffix = f"_{sanitize_filename_part(LOG_IDENTIFIER)}" if LOG_IDENTIFIER else ""
    file_name = f"papertrade_{timestamp}{suffix}.xlsx"
    return os.path.join("logs", file_name)


def build_livetrade_excel_file_path(trade_date: str, ce_symbol: str, pe_symbol: str) -> str:
    timestamp = get_timestamp_str()
    suffix = f"_{sanitize_filename_part(LOG_IDENTIFIER)}" if LOG_IDENTIFIER else ""
    file_name = f"livetrade_{timestamp}{suffix}.xlsx"
    return os.path.join("logs", file_name)


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


def build_papertrade_summary_df(live_trade_events: List[Dict], guard_stop_reason: str = "", run_mode: str = "PAPERTRADE_LIVE") -> pd.DataFrame:
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

    summary_rows = [
        {"metric": "run_mode", "value": str(run_mode)},
        {"metric": "script_name", "value": get_running_script_name()},
        {"metric": "gap_type", "value": latest_context.get("gap_type", "")},
        {"metric": "gap_points", "value": latest_context.get("gap_points", "")},
        {"metric": "gap_pct", "value": latest_context.get("gap_pct", "")},
        {"metric": "cpr_pivot", "value": latest_context.get("cpr_pivot", "")},
        {"metric": "cpr_bc", "value": latest_context.get("cpr_bc", "")},
        {"metric": "cpr_tc", "value": latest_context.get("cpr_tc", "")},
        {"metric": "cpr_width", "value": latest_context.get("cpr_width", "")},
        {"metric": "cpr_width_pct", "value": latest_context.get("cpr_width_pct", "")},
        {"metric": "cpr_type", "value": latest_context.get("cpr_type", "")},
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
    if reuse_path:
        excel_file = reuse_path
    else:
        excel_file = build_livetrade_excel_file_path(trade_date, ce_symbol, pe_symbol) if str(execution_mode).upper() == "LIVE_ORDER" else build_papertrade_excel_file_path(trade_date, ce_symbol, pe_symbol)
    ensure_parent_dir(excel_file)

    events_df = to_excel_safe_df(live_events)
    if not events_df.empty and "event_time" in events_df.columns:
        events_df = events_df.sort_values("event_time").reset_index(drop=True)

    trades_df = to_excel_safe_df(live_trade_events)
    if not trades_df.empty and "entry_time" in trades_df.columns:
        trades_df = trades_df.sort_values("entry_time").reset_index(drop=True)

    # Align trade `entry_time` with actual BUY event times when available.
    try:
        if not events_df.empty and not trades_df.empty and "option_symbol" in events_df.columns and "option_symbol" in trades_df.columns:
            buy_events = events_df[events_df.get("event_type", "").astype(str).str.upper() == "BUY"][["option_symbol", "event_time"]].copy()
            # Build a queue of buy times per option symbol (preserve chronological order)
            buy_queues = {}
            for _, r in buy_events.iterrows():
                sym = str(r.get("option_symbol") or "")
                buy_queues.setdefault(sym, []).append(r.get("event_time"))

            # Replace trades_df.entry_time with the first unused buy event time for the same symbol
            for idx, row in trades_df.iterrows():
                sym = str(row.get("option_symbol") or "")
                if sym in buy_queues and buy_queues[sym]:
                    candidate = buy_queues[sym].pop(0)
                    trades_df.at[idx, "entry_time"] = candidate
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

    summary_df = build_papertrade_summary_df(live_trade_events, guard_stop_reason, execution_mode)

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
) -> Dict:
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
            if ref == "close":
                hist_spot_ltp = safe_float(spot_hist.iloc[-1].get("close"), 0.0)
                hist_spot_ts = spot_hist.iloc[-1].get("ts")
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
        "last_exception": None,
        "active_order_event_id": None,
        "printed_buy_event_ids": set(),
        "printed_sell_event_ids": set(),
        "printed_trade_ids": set(),
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
        "force_exit_triggered": False,
        "current_ce_symbol": ce_symbol,
        "current_pe_symbol": pe_symbol,
        "current_ce_token": ce_token,
        "current_pe_token": pe_token,
        "ticker_ws": None,
        "ws_connected": False,
        "last_tick_ts": None,
        "last_tick_exchange_ts": None,
        "last_heartbeat_ts": None,
        "last_idle_status_ts": None,
        "last_idle_status_reason": None,
        "last_tick_stall_logged_ts": None,
        "last_tick_resubscribe_attempt_ts": None,
        "last_tick_reconnect_attempt_ts": None,
        "ws_reconnect_requested": False,
        "last_symbol_refresh_ts": None,
        "locked_trade_strike_active": False,
        "locked_ce_symbol": "",
        "locked_pe_symbol": "",
        "locked_ce_token": 0,
        "locked_pe_token": 0,
        "last_symbol_lock_skip_reason": "",
        "last_open_pnl_log_minute": None,
        "last_open_pnl_event_id": None,
        "open_pnl_log_lock": threading.Lock(),
        "market_context": {},
        "last_day_debug_df": pd.DataFrame(),
        "last_entry_diag_key": None,
        "entry_diag_file_path": os.path.join(
            "logs",
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

    token_to_key = {UNDERLYING_TOKEN: "spot", ce_token: "ce", pe_token: "pe"}

    def refresh_entry_symbols(reason: str = "POST_EXIT", forced_pair: Optional[tuple] = None) -> None:
        if not AUTO_SELECT_OPTION_SYMBOLS:
            return

        if live_state.get("locked_trade_strike_active") and forced_pair is None:
            skip_key = f"LOCKED:{reason}"
            if live_state.get("last_symbol_lock_skip_reason") != skip_key:
                print(
                    f"[SYMBOL REFRESH SKIPPED] reason={reason} "
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
                spot_ltp_now = get_spot_ltp(kite, UNDERLYING_NAME)
                selection_ts = live_state.get("last_tick_exchange_ts") or live_state.get("last_tick_ts")
                new_ce_symbol, new_pe_symbol, new_ce_token, new_pe_token = auto_select_ce_pe_symbols(
                    kite,
                    spot_ltp_now,
                    OPTION_EXCHANGE,
                    selection_time=selection_ts,
                )
        except Exception as exc:
            print(f"[SYMBOL REFRESH FAILED] reason={reason} error={exc}")
            return

        old_ce_symbol = str(live_state.get("current_ce_symbol") or "")
        old_pe_symbol = str(live_state.get("current_pe_symbol") or "")
        old_ce_token = safe_int(live_state.get("current_ce_token"), 0)
        old_pe_token = safe_int(live_state.get("current_pe_token"), 0)

        # Commit new symbols immediately so subsequent rebuilds use them.
        live_state["current_ce_symbol"] = new_ce_symbol
        live_state["current_pe_symbol"] = new_pe_symbol
        live_state["current_ce_token"] = new_ce_token
        live_state["current_pe_token"] = new_pe_token

        if old_ce_token == new_ce_token and old_pe_token == new_pe_token:
            unchanged_key = f"{reason}:{new_ce_symbol}:{new_pe_symbol}"
            if live_state.get("last_symbol_refresh_unchanged_key") != unchanged_key:
                print(
                    f"[SYMBOL REFRESH] reason={reason} spot={spot_ltp_now:.2f} -> "
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
            f"[SYMBOL REFRESH] reason={reason} spot={spot_ltp_now:.2f} "
            f"{old_ce_symbol}/{old_pe_symbol} -> {new_ce_symbol}/{new_pe_symbol}"
        )

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
            event_id = f"{side}|{signal_ts}|{str(pending_buy.get('entry_reason') or '')}"
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

    def get_completed_execution_trade_count() -> int:
        """Count only completed execution trades for MAX_TRADES."""
        return len(live_state.get("live_trade_events") or [])

    def max_trades_reached_in_execution() -> bool:
        if ENABLE_MTM_GUARD:
            return False
        max_trades_cfg = safe_int(MAX_TRADES, 0)
        if max_trades_cfg <= 0:
            return False
        return get_completed_execution_trade_count() >= max_trades_cfg

    def get_execution_stop_reason(trades: pd.DataFrame = None) -> str:
        """Use real execution state for LIVE_ORDER stops, not rebuilt strategy attrs."""
        now_local = datetime.now(IST)
        if now_local >= no_new_entry_after_live_ts:
            if execution_mode == "LIVE_ORDER":
                has_open_trade = get_open_local_position_count(live_state) > 0
            else:
                open_pos = trades.attrs.get("open_position") if (trades is not None and hasattr(trades, "attrs")) else None
                has_open_trade = bool(open_pos)
            if not has_open_trade:
                return "NO_NEW_ENTRY_AFTER_NO_OPEN_TRADE"

        if execution_mode == "LIVE_ORDER":
            live_realized_pnl = safe_float(live_state.get("realized_live_pnl_rs"), 0.0)
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
            if max_trades_reached_in_execution():
                return "MAX_TRADES_REACHED"
            return ""
        if trades is not None and hasattr(trades, "attrs"):
            reason = str(trades.attrs.get("guard_stop_reason") or "")
            # Only treat as a session-ending stop for explicit session-level reasons.
            # Per-trade exit reasons (TRAILING_SL_HIT, SL_HIT, TARGET_HIT, FORCE_EXIT, etc.)
            # are stored as a reporting fallback and must NOT stop the live session.
            _SESSION_STOP = {"DAILY_PROFIT_TARGET", "DAILY_LOSS_LIMIT", "MAX_TRADES_REACHED", "PROFIT_LOCK_HIT"}
            if reason in _SESSION_STOP or "CONSECUTIVE_LOSSES" in reason:
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

    def emit_heartbeat(force: bool = False):
        if not HEARTBEAT_ENABLED:
            return
        now_local = datetime.now(IST)
        last_hb = live_state.get("last_heartbeat_ts")
        should_print = force or (last_hb is None) or ((now_local - last_hb).total_seconds() >= safe_int(HEARTBEAT_INTERVAL_SECONDS, 30))
        if not should_print:
            return
        print(
            f"[HEARTBEAT] {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
            f"ws_connected={live_state.get('ws_connected')} | "
            f"open_local_positions={get_open_local_position_count(live_state)} | "
            f"broker_positions={len(live_state.get('broker_positions') or {})} | "
            f"current_ce={live_state.get('current_ce_symbol')} | "
            f"current_pe={live_state.get('current_pe_symbol')} | "
            f"last_live_exit_time={live_state.get('last_live_exit_time')} | "
            f"last_tick_ts={live_state.get('last_tick_ts')} | "
            f"last_tick_exchange_ts={live_state.get('last_tick_exchange_ts')}"
        )
        live_state["last_heartbeat_ts"] = now_local

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
            now_minute = floor_to_minute(now_local)
            last_logged_minute = live_state.get("last_open_pnl_log_minute")
            if last_logged_minute is not None and now_minute <= last_logged_minute:
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

                    current_option_price = get_option_ltp_from_live_state(live_state, side)
                    if current_option_price <= 0:
                        try:
                            current_option_price = get_option_symbol_ltp(kite, option_symbol)
                        except Exception:
                            current_option_price = 0.0
                    if current_option_price <= 0:
                        continue

                    unrealized_points = current_option_price - entry_price
                    unrealized_rs = unrealized_points * qty
                    realized_rs = safe_float(live_state.get("realized_live_pnl_rs"), 0.0)
                    total_mtm_rs = realized_rs + unrealized_rs
                    trailing_status = get_trailing_profit_status(unrealized_points)
                    trailing_suffix = f" | trailing={trailing_status}" if trailing_status else ""

                    print(
                        f"[OPEN PNL] {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                        f"symbol={option_symbol} | side={side} | ltp={current_option_price:.2f} | "
                        f"entry={entry_price:.2f} | qty={qty} | unrealized={unrealized_points:.2f} pts ({unrealized_rs:.2f} rs) | "
                        f"realized={realized_rs:.2f} rs | total_mtm={total_mtm_rs:.2f} rs{trailing_suffix}",
                        flush=True,
                    )
                    printed = True

                if printed:
                    live_state["last_open_pnl_log_minute"] = now_minute
                return

            open_pos = trades.attrs.get("open_position") if hasattr(trades, "attrs") else None
            if not open_pos:
                live_state["last_open_pnl_event_id"] = None
                return

            side = str(open_pos.get("side") or "")
            entry_reason = str(open_pos.get("entry_reason") or "")
            entry_time = open_pos.get("entry_time")
            open_event_id = f"{side}|{entry_time}|{entry_reason}"
            if live_state.get("last_open_pnl_event_id") != open_event_id:
                live_state["last_open_pnl_event_id"] = open_event_id
                live_state["last_open_pnl_log_minute"] = None

            # Determine entry price from the recorded BUY event (prefer stored BUY event price)
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
            trailing_status = get_trailing_profit_status(
                unrealized_points,
                current_time=now_local,
                stage1_triggered=safe_float(open_pos.get("trailing_stop_price"), 0.0) > safe_float(open_pos.get("emergency_sl_price"), 0.0),
            )
            trailing_suffix = f" | trailing={trailing_status}" if trailing_status else ""

            print(
                f"[OPEN PNL] {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')} | "
                f"symbol={open_pos.get('option_symbol')} | side={side} | ltp={current_option_price:.2f} | "
                f"entry={entry_price:.2f} | qty={qty} | unrealized={unrealized_points:.2f} pts ({unrealized_rs:.2f} rs) | "
                f"realized={realized_rs:.2f} rs | total_mtm={total_mtm_rs:.2f} rs{trailing_suffix}",
                flush=True,
            )
            live_state["last_open_pnl_log_minute"] = now_minute

    def print_execution_buy_summary(event_row: Dict) -> None:
        event_time = event_row.get("event_time")
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
        print(
            f"Entered position at {event_time} | mode={execution_mode} | side={side_value} symbol={symbol_value} "
            f"reason={reason_value} entry={option_price:.2f} spot={spot_price:.2f} qty={qty_value} "
            f"emergency_sl={emergency_sl:.2f}{target_suffix} trailing={pending_stage}"
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
            f"Exited position at {event_time} with reason {reason_value}. "
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
        live_state["ws_reconnect_requested"] = True
        print(
            f"[TICK STALL RECONNECT] No ticks for {int(stall_seconds)}s. "
            "Restarting websocket connection..."
        )
        safe_stop_ws(ws)

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

                # Prefer websocket ticks to avoid REST LTP latency/rate-limit misses.
                if side == "CE":
                    ce_tick = live_state.get("current", {}).get(safe_int(live_state.get("current_ce_token"), 0)) or {}
                    option_ltp = safe_float(ce_tick.get("close"), 0.0)
                elif side == "PE":
                    pe_tick = live_state.get("current", {}).get(safe_int(live_state.get("current_pe_token"), 0)) or {}
                    option_ltp = safe_float(pe_tick.get("close"), 0.0)

                if option_ltp <= 0 and option_symbol:
                    option_ltp = get_option_symbol_ltp(kite, option_symbol)

                if entry_price_actual > 0 and option_ltp > 0:
                    pnl_points_live = option_ltp - entry_price_actual
                    synced["live_pnl_points"] = pnl_points_live
                    current_stop = safe_float(synced.get("trailing_stop_price"), safe_float(synced.get("emergency_sl_price"), 0.0))
                    _stage1_hit = safe_float(synced.get("trailing_stop_price"), 0.0) > safe_float(synced.get("emergency_sl_price"), 0.0)
                    trailing_stop_price = get_trailing_stop_for_profit(
                        entry_price_actual,
                        pnl_points_live,
                        current_stop,
                        current_time=datetime.now(IST),
                        stage1_triggered=_stage1_hit,
                    )
                    # Sanity-check the computed trailing stop before applying it.
                    # Only adopt a raised trailing stop (which is entry_price + stop_profit)
                    # as actionable if the live PnL has reached at least the first
                    # configured trigger level; otherwise keep the previous stop to
                    # avoid an immediate accidental exit.
                    min_trigger = None
                    try:
                        active = get_active_trailing_stages(current_time=datetime.now(IST), stage1_triggered=_stage1_hit)
                        if active and len(active) > 0:
                            min_trigger = float(active[0][0])
                    except Exception:
                        min_trigger = None

                    # If trailing_stop_price looks like it was raised above current
                    # market LTP but the PnL hasn't reached the trigger, ignore the
                    # raised value to avoid immediate exits.
                    if (min_trigger is not None) and (pnl_points_live < float(min_trigger)) and (trailing_stop_price > option_ltp):
                        print(
                            f"[TRAIL SANITY] Ignoring raised trailing_stop_price={trailing_stop_price:.2f} "
                            f"because pnl_pts={pnl_points_live:.2f} < trigger={min_trigger:.2f}. Keeping current_stop={current_stop:.2f}"
                        )
                        synced["trailing_stop_price"] = current_stop
                    else:
                        synced["trailing_stop_price"] = trailing_stop_price

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
                                    cancel_exit_orders(kite, synced)
                                    order_meta = place_live_exit_order(kite, side, ce_symbol, pe_symbol, exit_qty, broker_entry=synced)
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

            exit_reason = str(broker_entry.get("broker_exit_reason") or "MANUAL_OR_EXTERNAL_EXIT")
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
            if len(live_state["live_trade_events"]) == 1:
                lock_entry_symbols_to_first_trade_strike(str(broker_trade.get("option_symbol", "")))
            register_symbol_exit_cooldown(live_state, broker_entry.get("option_symbol", ""), exit_time)
            live_state["printed_sell_event_ids"].add(event_id)
            live_state["printed_trade_ids"].add(event_id)
            live_state["broker_positions"].pop(event_id, None)

    def force_exit_live_order_positions(exit_reason: str = "#sym:FORCE_EXIT"):
        """Immediately flatten all tracked broker positions in LIVE_ORDER mode."""
        if execution_mode != "LIVE_ORDER":
            return

        print(f"[FORCE EXIT ACTION] Flattening open LIVE_ORDER positions with reason={exit_reason}")

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

                cancel_exit_orders(kite, synced)
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
                        "broker_exit_reason": exit_reason,
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

        # Periodic ATM symbol refresh: re-select every N minutes when no position is open
        if AUTO_SELECT_OPTION_SYMBOLS:
            refresh_interval = max(1, safe_int(AUTO_SELECT_REFRESH_INTERVAL_MINUTES, 5))
            now_for_refresh = datetime.now(IST)
            last_refresh = live_state.get("last_symbol_refresh_ts")
            no_open_positions = (
                get_open_local_position_count(live_state) == 0
                and live_state.get("active_order_event_id") is None
            )
            refresh_due = (
                last_refresh is None
                or (now_for_refresh - last_refresh).total_seconds() >= refresh_interval * 60
            )
            if no_open_positions and refresh_due:
                try:
                    refresh_entry_symbols(reason="PERIODIC_NO_TRADE")
                except Exception as _ref_exc:
                    print(f"[PERIODIC SYMBOL REFRESH ERROR] {_ref_exc}")
                live_state["last_symbol_refresh_ts"] = now_for_refresh

        spot_df = rows_to_df(live_state["spot_rows"])
        ce_df = rows_to_df(live_state["ce_rows"])
        pe_df = rows_to_df(live_state["pe_rows"])

        if spot_df.empty or ce_df.empty or pe_df.empty:
            emit_heartbeat()
            emit_idle_status("Waiting for enough completed candles to evaluate signals")
            return

        day = prepare_merged_day(spot_df, ce_df, pe_df)
        for k, v in (live_state.get("market_context") or {}).items():
            day.attrs[k] = v
        live_state["last_day_debug_df"] = day.copy()
        trades = run_single_day_test(
            day,
            execution_mode,
            tracking_start_override=live_tracking_start,
            ce_symbol_override=live_state.get("current_ce_symbol") or ce_symbol,
            pe_symbol_override=live_state.get("current_pe_symbol") or pe_symbol,
        )
        live_state["final_trades"] = trades

        new_events_added = append_broker_forced_exit_trades()

        open_position = trades.attrs.get("open_position")
        current_open_event_id = None
        pending_buy = None  # defer BUY printing until after closed trades
        if open_position:
            current_open_event_id = f"{open_position['side']}|{open_position['entry_time']}|{open_position['entry_reason']}"

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
                if current_open_event_id not in live_state["printed_buy_event_ids"]:
                    pending_buy = open_position
                    log_entry_diag("PENDING_BUY_CREATED", pending_buy)
        elif live_state["active_order_event_id"] is not None:
            if execution_mode != "LIVE_ORDER":
                # Paper mode unlock is handled only when a SELL is emitted for the
                # active event. This avoids transient rebuild states from releasing
                # the BUY lock and allowing an opposite-side BUY without a logged SELL.
                pass

        # --- Process closed trades first (chronological order) ---
        if not trades.empty:
            for _, trade in trades.iterrows():
                trade_id = f"{trade['side']}|{trade['entry_time']}|{trade['entry_reason']}|{trade['option_symbol']}"
                if trade_id in live_state["printed_trade_ids"]:
                    continue

                closed_order_event_id = f"{trade['side']}|{trade['entry_time']}|{trade['entry_reason']}"
                if execution_mode == "LIVE_ORDER":
                    # Do not replay historical strategy-only trades in LIVE_ORDER mode.
                    # Only process trades that are backed by a tracked broker entry.
                    if closed_order_event_id not in live_state["broker_positions"]:
                        continue

                sell_event_id = trade_id
                # Emit BUY event for this trade if not already printed
                buy_event_id = f"{trade['side']}|{trade['entry_time']}|{trade['entry_reason']}"
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
                        cancel_exit_orders(kite, broker_entry)
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
                            "broker_exit_reason": str(trade_record.get("exit_reason", "STRATEGY_EXIT")),
                            "manual_or_broker_exit_detected": True,
                            "broker_open_qty": 0,
                            "exit_in_progress": False,
                        })
                        live_state["broker_positions"].pop(closed_order_event_id, None)
                        register_symbol_exit_cooldown(live_state, broker_entry.get("option_symbol", ""), actual_exit_time)
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
                    if live_state["active_order_event_id"] == closed_order_event_id:
                        live_state["active_order_event_id"] = None
                    register_symbol_exit_cooldown(live_state, trade_record.get("option_symbol", ""), trade_record.get("exit_time") or sell_event_row.get("event_time"))
                    live_state["last_live_exit_time"] = trade_record.get("exit_time") or sell_event_row.get("event_time")
                    live_state["cooldown_block_logged_for"] = None
                    # After the first completed trade, lock strike and keep only CE/PE side switching.
                    if len(live_state.get("live_trade_events") or []) == 0:
                        lock_entry_symbols_to_first_trade_strike(str(trade_record.get("option_symbol", "")))
                    else:
                        try:
                            refresh_entry_symbols(reason="POST_EXIT")
                        except Exception:
                            pass
                    new_events_added = True

                print(f"\n========== NEW {execution_mode} TRADE(S) ==========")
                trade_df = pd.DataFrame([trade_record])
                print(trade_df.to_string(index=False))
                live_state["live_trade_events"].append(trade_record)
                if len(live_state["live_trade_events"]) == 1:
                    lock_entry_symbols_to_first_trade_strike(str(trade_record.get("option_symbol", "")))
                live_state["printed_trade_ids"].add(trade_id)
                new_events_added = True
                # If running backtest and the last two trades used the same entry signal
                # and the most recent trade was a loss, search for the latest signal
                # and commit updated entry symbols immediately so the next BUY uses it.
                if execution_mode != "LIVE_ORDER":
                    try:
                        events = live_state.get("live_trade_events") or []
                        if len(events) >= 2:
                            last = events[-1]
                            prev = events[-2]
                            last_pnl = safe_float(last.get("pnl_rs", 0.0))
                            if str(last.get("entry_reason") or "") == str(prev.get("entry_reason") or "") and last_pnl < 0:
                                print(f"[POST_EXIT BACKTEST] consecutive same-signal loss detected; searching latest signal and committing entry symbols")
                                try:
                                    refresh_entry_symbols(reason="PRE_ENTRY")
                                except Exception as _:
                                    pass
                    except Exception:
                        pass

        # Hard execution-level cap: once completed execution trades hit MAX_TRADES,
        # do not allow any fresh BUY to be placed from pending state.
        if pending_buy is not None and max_trades_reached_in_execution():
            log_entry_diag(
                "BLOCK_MAX_TRADES_REACHED",
                pending_buy,
                extra=f"completed={get_completed_execution_trade_count()} cfg={safe_int(MAX_TRADES, 0)}",
            )
            pending_buy = None
            # In LIVE_ORDER, do not write MAX_TRADES into strategy attrs; live stop is decided from completed execution trades only.
            if execution_mode != "LIVE_ORDER" and not trades.attrs.get("guard_stop_reason"):
                trades.attrs["guard_stop_reason"] = "MAX_TRADES_REACHED"
            if not live_state.get("max_trades_block_logged"):
                print(
                    f"[MAX_TRADES BLOCK] Fresh entry skipped because completed execution trade count "
                    f"{get_completed_execution_trade_count()} reached MAX_TRADES={safe_int(MAX_TRADES, 0)}"
                )
                live_state["max_trades_block_logged"] = True

        # --- Now print deferred BUY (after closed trades, so chronological order is preserved) ---
        if pending_buy is not None:
            blocked, block_reason, smart_actual_price = should_block_smart_entry(live_state, pending_buy, execution_mode)
            if blocked:
                log_entry_diag("BLOCK_SMART_ENTRY", pending_buy, extra=block_reason)
                print(f"[SMART ENTRY BLOCK] {pending_buy['option_symbol']} skipped: {block_reason}")
                pending_buy = None

        if pending_buy is not None:
            # Ensure symbols are refreshed right before any entry in all modes (BACKTEST/PAPERTEST/PAPERTRADE_LIVE/LIVE_ORDER)
            try:
                refresh_entry_symbols(reason="PRE_ENTRY")
            except Exception:
                pass
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
            )
            if execution_mode == "LIVE_ORDER":
                sync_broker_positions_if_needed(force=True)

                current_ts_for_entry = datetime.now(IST)
                last_exit_time = live_state.get("last_live_exit_time")
                if is_live_reentry_cooldown_active(last_exit_time, current_ts_for_entry):
                    log_entry_diag("BLOCK_LIVE_REENTRY_COOLDOWN", pending_buy, extra=f"last_exit={last_exit_time}")
                    block_key = f"{current_open_event_id}|{last_exit_time}"
                    if live_state.get("cooldown_block_logged_for") != block_key:
                        cooldown_until = pd.Timestamp(last_exit_time) + timedelta(minutes=safe_int(LIVE_REENTRY_COOLDOWN_MINUTES, 0))
                        print(
                            f"[LIVE ENTRY COOLDOWN] Fresh entry skipped for {pending_buy['option_symbol']} because "
                            f"re-entry cooldown of {safe_int(LIVE_REENTRY_COOLDOWN_MINUTES, 0)} minute(s) is still active "
                            f"after exit at {last_exit_time}. Cooldown until {cooldown_until}"
                        )
                        live_state["cooldown_block_logged_for"] = block_key
                    emit_heartbeat()
                    emit_idle_status(f"Cooldown active for {pending_buy['option_symbol']}")
                    return
                live_state["cooldown_block_logged_for"] = None

                # Ensure we resolve ATM symbols at the moment of a fresh entry
                try:
                    refresh_entry_symbols(reason="PRE_ENTRY")
                except Exception:
                    pass

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
                    if not live_state.get("max_trades_block_logged"):
                        print(
                            f"[MAX CONCURRENT BLOCK] Fresh entry skipped because open position count reached "
                            f"MAX_CONCURRENT_TRADES={safe_int(MAX_CONCURRENT_TRADES, 1)}"
                        )
                        live_state["max_trades_block_logged"] = True
                        emit_heartbeat()
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
                    emit_heartbeat()
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
                    emit_heartbeat()
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
                    emit_heartbeat()
                    emit_idle_status("PE per-side concurrent cap reached")
                    return

                if safe_int(ENTRY_CONFIRM_DELAY_SECONDS, 0) > 0:
                    time.sleep(safe_int(ENTRY_CONFIRM_DELAY_SECONDS, 0))
                order_meta = place_live_entry_order(
                    kite,
                    str(pending_buy["side"]),
                    entry_ce_symbol,
                    entry_pe_symbol,
                    int(pending_buy.get("qty", QTY)),
                    entry_price_hint=float(pending_buy["entry_price"]),
                )
                entry_snapshot = wait_for_order_terminal(kite, order_meta["entry_order_id"])
                entry_status = normalize_order_status(entry_snapshot.get("status"))
                if entry_status != "COMPLETE":
                    raise RuntimeError(f"Entry order not completed. order_id={order_meta['entry_order_id']} status={entry_status} message={entry_snapshot.get('status_message') or entry_snapshot.get('status_message_raw') or ''}")
                actual_entry_price = extract_avg_price(entry_snapshot)
                actual_entry_time = extract_order_time(entry_snapshot)
                option_symbol = str(entry_snapshot.get("tradingsymbol") or (entry_ce_symbol if str(pending_buy["side"]) == "CE" else entry_pe_symbol))
                if should_skip_entry_for_slippage(float(pending_buy["entry_price"]), actual_entry_price):
                    print(f"[SMART ENTRY EXIT] Immediate protective exit because fill slippage is too high for {option_symbol}: signal={float(pending_buy['entry_price']):.2f} fill={actual_entry_price:.2f}")
                    exit_meta = safe_place_live_exit_order(kite, str(pending_buy["side"]), entry_ce_symbol, entry_pe_symbol, int(pending_buy.get("qty", QTY)), broker_entry={"option_symbol": option_symbol, "qty": int(pending_buy.get("qty", QTY))})
                    if not exit_meta.get("skipped"):
                        wait_for_order_terminal(kite, exit_meta["exit_order_id"])
                    register_symbol_exit_cooldown(live_state, option_symbol, actual_entry_time)
                    return
                if order_meta.get("cover_order_used"):
                    sl_meta = {
                        "emergency_sl_order_id": "",
                        "emergency_sl_price": np.nan,
                        "emergency_sl_trigger_price": safe_float(order_meta.get("cover_order_trigger_price"), 0.0),
                        "emergency_sl_pct_used": safe_float(order_meta.get("emergency_sl_pct_used"), 0.0),
                    }
                else:
                    sl_meta = place_emergency_sl_order(kite, option_symbol, int(pending_buy.get("qty", QTY)), actual_entry_price)
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
                live_state["broker_positions"][current_open_event_id] = {
                    "side": str(pending_buy["side"]),
                    "option_symbol": option_symbol,
                    "qty": int(pending_buy.get("qty", QTY)),
                    "entry_reason": str(pending_buy["entry_reason"]),
                    "entry_time": pending_buy["entry_time"],
                    "entry_spot": float(pending_buy["entry_spot"]),
                    "signal_time": pending_buy["entry_time"],
                    "signal_spot_price": float(pending_buy["entry_spot"]),
                    "signal_option_price": float(pending_buy["entry_price"]),
                    "entry_price_theoretical": float(pending_buy["entry_price"]),
                    "entry_price_actual": actual_entry_price,
                    "mtf_entry_label": str(pending_buy.get("mtf_entry_label", "")),
                    "emergency_sl_pct_used": safe_float(order_meta.get("emergency_sl_pct_used"), 0.0),
                    "exit_in_progress": False,
                    "best_price": actual_entry_price,
                    "trailing_stop_price": safe_float(order_meta.get("emergency_sl_price"), 0.0),
                    "target_profit_points": float(TRAILING_PROFIT_STAGE1[0][0]) if TRAILING_PROFIT_STAGE1 else 50.0,
                    **order_meta,
                }
            else:
                if SIMULATE_LIVE_EXECUTION_IN_PAPERTRADE:
                    signal_entry_price = float(pending_buy["entry_price"])
                    simulated_entry_price = apply_paper_entry_slippage(signal_entry_price, side="BUY")
                    event_row["signal_option_price"] = signal_entry_price
                    event_row["entry_price_strategy"] = signal_entry_price
                    event_row["option_price"] = simulated_entry_price
                else:
                    simulated_entry_price = float(pending_buy["entry_price"])
            print_execution_buy_summary(event_row)
            print(f"\n========== {execution_mode} BUY ==========")
            print(pd.DataFrame([event_row]).to_string(index=False))
            live_state["live_events"].append(event_row)
            live_state["printed_buy_event_ids"].add(current_open_event_id)
            log_entry_diag("BUY_EMITTED", pending_buy)
            new_events_added = True

        if pending_buy is None and not new_events_added:
            emit_open_position_pnl_update(trades)
            emit_heartbeat()
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

    def on_ticks(ws, ticks):
        try:
            for tick in ticks:
                token = tick["instrument_token"]
                if token not in token_to_key:
                    continue
                ltp = float(tick.get("last_price") or 0.0)
                if ltp <= 0:
                    continue
                exch_ts = tick.get("exchange_timestamp") or tick.get("last_trade_time") or datetime.now(IST)
                update_last_tick_ts(exch_ts)
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

            emit_heartbeat()
            detect_tick_stall()
            now_local = datetime.now(IST)
            if not live_state.get("force_exit_triggered") and now_local >= force_exit_ts:
                live_state["force_exit_triggered"] = True
                print(
                    f"[FORCE EXIT TRIGGER] now={now_local.strftime('%Y-%m-%d %H:%M:%S %Z')} "
                    f">= configured_force_exit={force_exit_ts.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                )
                if execution_mode == "LIVE_ORDER":
                    force_exit_live_order_positions(exit_reason="#sym:FORCE_EXIT")
                    sync_broker_positions_if_needed(force=True)
                    append_broker_forced_exit_trades()
                live_state["stop"] = True
                finalize_completed_minutes(force_all=True)
            else:
                finalize_completed_minutes(force_all=False)
            if now_local.time() >= dtime(15, 30) or live_state["stop"]:
                finalize_completed_minutes(force_all=True)
                safe_stop_ws(ws)
        except PermissionException as exc:
            live_state["last_exception"] = exc
            print("Live processing halted: Kite API permission error while placing order.")
            print("Action required: add your current public IP in Kite developer console allowlist and retry.")
            safe_stop_ws(ws)
        except Exception as exc:
            live_state["last_exception"] = exc
            print(f"Live processing error: {exc}")
            safe_stop_ws(ws)

    def on_connect(ws, response):
        print("KiteTicker connected. Subscribing to live ticks...")
        live_state["ticker_ws"] = ws
        live_state["ws_connected"] = True
        live_state["ws_reconnect_requested"] = False
        live_state["last_tick_stall_logged_ts"] = None
        live_state["last_tick_resubscribe_attempt_ts"] = None
        live_state["last_tick_reconnect_attempt_ts"] = None
        emit_heartbeat(force=True)
        tokens = [UNDERLYING_TOKEN, safe_int(live_state.get("current_ce_token"), ce_token), safe_int(live_state.get("current_pe_token"), pe_token)]
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def on_close(ws, code, reason):
        live_state["ws_connected"] = False
        print(f"Connection closed: {code} - {reason}")
        emit_heartbeat(force=True)
        # Request reconnect on unclean closes (1006 = TCP drop) unless session is stopping.
        if not live_state.get("stop") and code in (1006, None):
            now_local = datetime.now(IST)
            if now_local.time() < dtime(15, 30):
                live_state["ws_reconnect_requested"] = True

    def on_error(ws, code, reason):
        print(f"KiteTicker error. code={code}, reason={reason}")
        emit_heartbeat(force=True)
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
                detect_tick_stall()
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
                    qty = safe_int(broker_entry.get("qty"), QTY)
                    print(f"  Exiting open position: {side} qty={qty} ({broker_entry.get('option_symbol', '?')})")
                    try:
                        cancel_exit_orders(kite, broker_entry)
                        order_meta = place_live_exit_order(kite, side, live_state.get("current_ce_symbol", ce_symbol), live_state.get("current_pe_symbol", pe_symbol), qty, broker_entry=broker_entry)
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
                open_pos = live_state["final_trades"].attrs.get("open_position") if not live_state["final_trades"].empty or hasattr(live_state["final_trades"], "attrs") else None
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
                    exit_reason = "FORCE_EXIT" if now_local >= force_exit_ts else "SESSION_STOP"
                    exit_option_price = get_option_ltp_from_live_state(live_state, side)
                    if exit_option_price <= 0:
                        exit_option_price = entry_price
                    exit_spot_price = get_spot_ltp_from_live_state(live_state)
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
                        event_time=now_local,
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
                        "exit_time": now_local,
                        "entry_reason": str(open_pos.get("entry_reason", "")),
                        "exit_reason": exit_reason,
                        "entry_spot": entry_spot,
                        "exit_spot": exit_spot_price,
                        "entry_price": entry_price,
                        "exit_price": exit_option_price,
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

            open_pos = None
            try:
                open_pos = live_state.get("final_trades").attrs.get("open_position")
            except Exception:
                open_pos = None
            print(f"Open position      : {'YES' if open_pos else 'NO'}")
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

    watchdog_thread = threading.Thread(target=watchdog_loop, name="tick-watchdog-v30", daemon=True)
    live_state["watchdog_thread"] = watchdog_thread
    watchdog_thread.start()

    print(f"Starting {execution_mode}... Press Ctrl+C to stop (will auto-exit open positions).")
    MAX_WS_RECONNECTS = 10
    reconnect_count = 0
    keyboard_interrupt_triggered = False
    while True:
        try:
            ticker.connect(threaded=False)
        except KeyboardInterrupt:
            keyboard_interrupt_triggered = True
            live_state["stop"] = True
            break

        if live_state.get("ws_reconnect_requested") and not live_state.get("stop") and live_state.get("last_exception") is None:
            if reconnect_count >= MAX_WS_RECONNECTS:
                print(f"[WS RECONNECT] Max reconnect attempts ({MAX_WS_RECONNECTS}) reached. Stopping.")
                break
            reconnect_count += 1
            live_state["ws_reconnect_requested"] = False
            print(f"[WS RECONNECT] Reconnecting websocket (attempt #{reconnect_count}/{MAX_WS_RECONNECTS})...")
            time.sleep(2)
            continue
        break

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

    if live_state["last_exception"] is not None:
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

    if bull >= MTF_STRICTNESS + 1 and bull > bear:
        return 1
    if bear >= MTF_STRICTNESS + 1 and bear > bull:
        return -1
    return 0


def option_premium_confirm(series: pd.Series, idx: int) -> bool:
    if idx < 1:
        return False
    return bool(series.iloc[idx] > series.iloc[idx - 1] and series.iloc[idx] > MIN_PREMIUM)


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
        direction="nearest",
        tolerance=pd.Timedelta("2min")
    )
    day = pd.merge_asof(
        day.sort_values("spot_ts"),
        pe_i.sort_values("pe_ts"),
        left_on="spot_ts",
        right_on="pe_ts",
        direction="nearest",
        tolerance=pd.Timedelta("2min")
    )

    day = day.dropna(subset=["ce_close", "pe_close", "mtf_close"]).reset_index(drop=True)
    day["mtf_trend"] = day.apply(mtf_trend_score, axis=1)
    day["mtf_label"] = day["mtf_trend"].map({1: "BULLISH_5M", -1: "BEARISH_5M", 0: "NEUTRAL_5M"})
    day["signal"] = ""
    day["position"] = ""
    return day


def run_single_day_test(day: pd.DataFrame, run_mode: str, previous_realized_mtm: float = 0.0, tracking_start_override=None, ce_symbol_override: Optional[str] = None, pe_symbol_override: Optional[str] = None) -> pd.DataFrame:
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
    sl = None
    fixed_sl_price = None
    trailing_stop_active = False
    emergency_sl_price = None
    emergency_sl_pct_used = float(FIXED_SL_POINTS)
    best_price = None
    entry_reason = None
    trade_count = 0
    realized_mtm_rs = previous_realized_mtm
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

    # Per-side entry switches: when configured as 0, disable fresh entries for that side.
    allow_ce_entries = safe_int(MAX_CONCURRENT_TRADES_CE, 1) > 0
    allow_pe_entries = safe_int(MAX_CONCURRENT_TRADES_PE, 1) > 0

    for i in range(1, len(day)):
        row = day.iloc[i]
        prev = day.iloc[i - 1]
        ts = row["spot_ts"]

        if ts < effective_tracking_start:
            continue

        day.loc[i, "realized_mtm_rs"] = realized_mtm_rs

        if not in_position:
            # Always block fresh entries after the configured cutoff time,
            # regardless of whether MTM guard or MAX_TRADES mode is active.
            # print(f"Checking entry conditions at {ts}... (realized MTM={realized_mtm_rs:.2f} rs, trade_count={trade_count})")

            # Periodic ATM symbol refresh in backtest: re-select every N minutes when flat
            if AUTO_SELECT_OPTION_SYMBOLS and (not strike_locked_after_first_trade):
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
                            _new_ce, _new_pe, _new_ce_token, _new_pe_token = auto_select_ce_pe_symbols(_kite, _spot, OPTION_EXCHANGE, selection_time=ts)
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
                                    _remaining_idx = day.index[i:]
                                    _remaining_spot = day.loc[_remaining_idx, ["spot_ts"]].copy()
                                    if _new_ce != _old_ce and _new_ce_token:
                                        try:
                                            _ce_hist = fetch_history(_kite, _new_ce_token, session_trade_date, "minute")
                                            if not _ce_hist.empty:
                                                _ce_new = _ce_hist.rename(columns={"ts": "ce_ts", "open": "ce_open", "high": "ce_high", "low": "ce_low", "close": "ce_close", "volume": "ce_volume"})
                                                _ce_merged = pd.merge_asof(
                                                    _remaining_spot.sort_values("spot_ts"),
                                                    _ce_new.sort_values("ce_ts"),
                                                    left_on="spot_ts", right_on="ce_ts",
                                                    direction="nearest", tolerance=pd.Timedelta("2min"),
                                                )
                                                _ce_merged = _ce_merged.set_index(_remaining_spot.index)
                                                for _c in ["ce_ts", "ce_open", "ce_high", "ce_low", "ce_close", "ce_volume"]:
                                                    if _c in _ce_merged.columns and _c in day.columns:
                                                        day.loc[_remaining_idx, _c] = _ce_merged[_c]
                                        except Exception:
                                            pass
                                    if _new_pe != _old_pe and _new_pe_token:
                                        try:
                                            _pe_hist = fetch_history(_kite, _new_pe_token, session_trade_date, "minute")
                                            if not _pe_hist.empty:
                                                _pe_new = _pe_hist.rename(columns={"ts": "pe_ts", "open": "pe_open", "high": "pe_high", "low": "pe_low", "close": "pe_close", "volume": "pe_volume"})
                                                _pe_merged = pd.merge_asof(
                                                    _remaining_spot.sort_values("spot_ts"),
                                                    _pe_new.sort_values("pe_ts"),
                                                    left_on="spot_ts", right_on="pe_ts",
                                                    direction="nearest", tolerance=pd.Timedelta("2min"),
                                                )
                                                _pe_merged = _pe_merged.set_index(_remaining_spot.index)
                                                for _c in ["pe_ts", "pe_open", "pe_high", "pe_low", "pe_close", "pe_volume"]:
                                                    if _c in _pe_merged.columns and _c in day.columns:
                                                        day.loc[_remaining_idx, _c] = _pe_merged[_c]
                                        except Exception:
                                            pass
                    except Exception as _ref_exc:
                        pass
                    last_symbol_refresh_ts = ts

            if ts >= no_new_entry_after_ts:
                day.loc[i, "guard_status"] = "NO_NEW_ENTRY_CUTOFF"
                continue

            mtm_hit, mtm_reason = mtm_guard_status(realized_mtm_rs) if ENABLE_MTM_GUARD else (False, '')

            if mtm_hit:
                guard_stop_reason = mtm_reason
                day.loc[i, "guard_status"] = guard_stop_reason
                break

            trade_guard_hit = MAX_TRADES is not None and MAX_TRADES > 0 and trade_count >= MAX_TRADES
            if trade_guard_hit:
                guard_stop_reason = "MAX_TRADES_REACHED"
                day.loc[i, "guard_status"] = guard_stop_reason
                break

            swing_slice = day.iloc[max(0, i - SWING_LOOKBACK):i]
            swing_low = float(swing_slice["spot_low"].min()) if len(swing_slice) else float(prev["spot_low"])
            swing_high = float(swing_slice["spot_high"].max()) if len(swing_slice) else float(prev["spot_high"])

            bull_score = bull_reversal_score(row, prev, swing_low)
            bear_score = bear_reversal_score(row, prev, swing_high)
            ce_confirm = option_premium_confirm(day["ce_close"], i)
            pe_confirm = option_premium_confirm(day["pe_close"], i)
            mtf_trend = int(row["mtf_trend"])

            # Prevent re-entry on the same option symbol shortly after a completed trade
            candidate_symbol = None
            if mtf_trend == 1:
                candidate_symbol = local_ce_symbol
            elif mtf_trend == -1:
                candidate_symbol = local_pe_symbol

            cooldown_mins = safe_int(BLOCK_SAME_SYMBOL_REENTRY_WITHIN_MINUTES, 0)
            if candidate_symbol and last_completed_option_symbol == candidate_symbol and last_completed_exit_time is not None and ts <= (last_completed_exit_time + timedelta(minutes=cooldown_mins)):
                day.loc[i, "guard_status"] = "SAME_SYMBOL_COOLDOWN"
                continue

            # Price-drop gate: after a same-symbol exit, only re-enter once the option
            # has fallen at least SAME_SYMBOL_MIN_REENTRY_DROP_POINTS below the exit price.
            _min_drop = safe_float(SAME_SYMBOL_MIN_REENTRY_DROP_POINTS, 0.0)
            if _min_drop > 0 and candidate_symbol and candidate_symbol == last_completed_option_symbol and last_completed_exit_price is not None:
                _cur_opt_price = float(row["pe_close"] if candidate_symbol == local_pe_symbol else row["ce_close"])
                if _cur_opt_price >= last_completed_exit_price - _min_drop:
                    day.loc[i, "guard_status"] = "SAME_SYMBOL_MIN_DROP_NOT_MET"
                    continue

            if mtf_trend == 1 and bull_score >= ENTRY_SCORE_THRESHOLD and ce_confirm and allow_ce_entries:
                side = "CE"
                entry_price = float(row["ce_close"])  # signal fires at candle close; enter at close price
                entry_ts = ts
                entry_spot = float(row["spot_close"])
                fixed_sl_price = get_entry_stop_price(entry_price)
                sl = fixed_sl_price
                trailing_stop_active = False
                emergency_sl_points = get_dynamic_emergency_sl_points(entry_price)
                emergency_sl_price = max(OPTION_TICK_SIZE, entry_price - emergency_sl_points)
                emergency_sl_pct_used = emergency_sl_points
                best_price = entry_price
                entry_reason = f"BUY_CE_MTF_BULL_S{bull_score}"
                in_position = True
                trade_count += 1
                day.loc[i, "signal"] = entry_reason
                day.loc[i, "position"] = "LONG_CE"
                if run_mode == "BACKTEST":
                    print(
                        f"Entered position at {entry_ts} | side={side} symbol={local_ce_symbol} "
                        f"reason={entry_reason} entry={entry_price:.2f} spot={entry_spot:.2f} "
                        f"sl={safe_float(sl, 0.0):.2f} emergency_sl={safe_float(emergency_sl_price, 0.0):.2f}"
                    )
            elif mtf_trend == 1 and bull_score >= ENTRY_SCORE_THRESHOLD and ce_confirm and not allow_ce_entries:
                day.loc[i, "guard_status"] = "CE_DISABLED_BY_CONFIG"

            elif mtf_trend == -1 and bear_score >= ENTRY_SCORE_THRESHOLD and pe_confirm and allow_pe_entries:
                side = "PE"
                entry_price = float(row["pe_close"])  # signal fires at candle close; enter at close price
                entry_ts = ts
                entry_spot = float(row["spot_close"])
                fixed_sl_price = get_entry_stop_price(entry_price)
                sl = fixed_sl_price
                trailing_stop_active = False
                emergency_sl_points = get_dynamic_emergency_sl_points(entry_price)
                emergency_sl_price = max(OPTION_TICK_SIZE, entry_price - emergency_sl_points)
                emergency_sl_pct_used = emergency_sl_points
                best_price = entry_price
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
            elif mtf_trend == -1 and bear_score >= ENTRY_SCORE_THRESHOLD and pe_confirm and not allow_pe_entries:
                day.loc[i, "guard_status"] = "PE_DISABLED_BY_CONFIG"

        else:
            ltp = float(row["ce_close"] if side == "CE" else row["pe_close"])
            best_price = max(best_price, ltp)
            pnl_pct = (ltp - entry_price) / entry_price
            open_pnl_rs = (ltp - entry_price) * QTY
            total_mtm_rs = realized_mtm_rs + open_pnl_rs
            day.loc[i, "realized_mtm_rs"] = realized_mtm_rs

            pnl_points_current = ltp - entry_price
            if ENABLE_TRAILING_PROFIT_EXIT:
                updated_sl = get_trailing_stop_for_profit(
                    entry_price,
                    pnl_points_current,
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
                    pnl_points_current,
                    sl,
                    current_time=ts,
                    stage1_triggered=trailing_stop_active,
                )
                sl = max(sl, updated_sl)
                guard_stop_reason = "DAILY_PROFIT_TARGET"  # blocks new entries after this trade closes
                day.loc[i, "guard_status"] = "PROFIT_TARGET_TRAILING"
                mtm_hit = False  # suppress immediate MTM exit; trailing SL will manage the exit

            if mtm_hit:
                exit_reason = mtm_reason
                exit_price = ltp
                guard_stop_reason = mtm_reason
                day.loc[i, "guard_status"] = mtm_reason
            elif ENABLE_EMERGENCY_SL_ORDER and emergency_sl_price is not None and ltp <= emergency_sl_price:
                exit_reason = "EMERGENCY_SL_HIT"
                exit_price = emergency_sl_price  # use exact SL level, not candle close
            elif ltp <= sl:
                if ENABLE_TRAILING_PROFIT_EXIT and trailing_stop_active:
                    exit_reason = "TRAILING_SL_HIT"
                else:
                    exit_reason = "SL_HIT"
                exit_price = sl  # use exact SL level, not candle close
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
                pnl_points = exit_price - entry_price
                pnl_rs = pnl_points * QTY
                realized_mtm_rs += pnl_rs

                # Profit-lock fix: count completed trades immediately after every exit
                global completed_trades_for_profit_lock, profit_lock_active, profit_lock_level
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
                cumulative_pnl = sum(t["pnl_rs"] for t in trades) + pnl_rs
                _active_stage = "stage2" if ts.strftime("%H:%M") >= TRAILING_PROFIT_STAGE2_TIME else "stage1"
                _trigger_points = float(TRAILING_PROFIT_STAGE2[0][0]) if _active_stage == "stage2" and TRAILING_PROFIT_STAGE2 else float(TRAILING_PROFIT_STAGE1[0][0]) if TRAILING_PROFIT_STAGE1 else 50.0
                _max_profit_points = (best_price - entry_price) if best_price is not None and entry_price is not None else 0.0
                _trailing_status = (
                    f"{_active_stage}_hit"
                    if exit_reason == "TRAILING_SL_HIT"
                    else f"{_active_stage}_armed"
                    if _max_profit_points >= _trigger_points
                    else "not_reached_target"
                )

                trades.append({
                    "run_mode": run_mode,
                    "trade_date": session_trade_date,
                    "underlying": UNDERLYING_NAME,
                    "side": side,
                    "option_symbol": local_ce_symbol if side == "CE" else local_pe_symbol,
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

                if AUTO_SELECT_OPTION_SYMBOLS and (not strike_locked_after_first_trade):
                    _base = strip_option_side_suffix(str(last_completed_option_symbol or ""))
                    if _base:
                        _locked_ce = f"{_base}CE"
                        _locked_pe = f"{_base}PE"
                        if local_ce_symbol != _locked_ce or local_pe_symbol != _locked_pe:
                            local_ce_symbol = _locked_ce
                            local_pe_symbol = _locked_pe
                        strike_locked_after_first_trade = True
                        print(
                            f"[STRIKE LOCKED] mode={run_mode} first_trade_symbol={last_completed_option_symbol} "
                            f"locked_ce={local_ce_symbol} locked_pe={local_pe_symbol}"
                        )

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
                entry_reason = None
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
            "option_symbol": local_ce_symbol if side == "CE" else local_pe_symbol,
            "entry_time": entry_ts,
            "entry_reason": entry_reason,
            "entry_spot": entry_spot,
            "entry_price": entry_price,
            "qty": QTY,
            "mtf_entry_label": entry_label,
            "gap_type": market_ctx.get("gap_type", ""),
            "gap_points": market_ctx.get("gap_points", ""),
            "gap_pct": market_ctx.get("gap_pct", ""),
            "cpr_type": market_ctx.get("cpr_type", ""),
            "cpr_width_pct": market_ctx.get("cpr_width_pct", ""),
            "emergency_sl_pct_used": emergency_sl_pct_used,
            "emergency_sl_price": emergency_sl_price,
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

# MAIN EXECUTION
# =========================
RUN_MODE = normalize_run_mode(RUN_MODE)

try:
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

            spot_ltp = 0.0
            if RUN_MODE in {"PAPERTRADE_LIVE", "LIVE_ORDER"}:
                try:
                    spot_ref_df = fetch_history(kite, UNDERLYING_TOKEN, TRADE_DATE, BASE_INTERVAL)
                    if not spot_ref_df.empty:
                        ref = str(AUTO_SELECT_SPOT_REF or "").lower()
                        if ref == "open" and "open" in spot_ref_df.columns:
                            spot_ltp = float(spot_ref_df["open"].dropna().iloc[0])
                        elif ref == "last":
                            if "last_price" in spot_ref_df.columns and not spot_ref_df["last_price"].dropna().empty:
                                spot_ltp = float(spot_ref_df["last_price"].dropna().iloc[-1])
                            elif "close" in spot_ref_df.columns and not spot_ref_df["close"].dropna().empty:
                                spot_ltp = float(spot_ref_df["close"].dropna().iloc[-1])
                        else:
                            if "close" in spot_ref_df.columns and not spot_ref_df["close"].dropna().empty:
                                spot_ltp = float(spot_ref_df["close"].dropna().iloc[-1])
                    if spot_ltp > 0:
                        print(f"Spot LTP resolved from historical ref '{AUTO_SELECT_SPOT_REF}': {spot_ltp}")
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
            if AUTO_SELECT_OPTION_SYMBOLS:
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

            print(f"Fetching CE history for {CE_SYMBOL} ({ce_token})...")
            ce = fetch_history(kite, ce_token, TRADE_DATE, BASE_INTERVAL)

            print(f"Fetching PE history for {PE_SYMBOL} ({pe_token})...")
            pe = fetch_history(kite, pe_token, TRADE_DATE, BASE_INTERVAL)
        except ValueError as exc:
            if "No history returned for token" in str(exc):
                print(f"Market has not opened on {TRADE_DATE}. No historical data available yet.")
                raise SystemExit(0)
            raise

        print("Preparing 1-minute + 5-minute merged dataset...")
        day = prepare_merged_day(spot, ce, pe)

        # Fetch previous trading day OHLC for CPR + gap context in logs/summary
        try:
            prev_ohlc = fetch_prev_trading_day_ohlc(kite, UNDERLYING_TOKEN, TRADE_DATE)
        except Exception:
            prev_ohlc = {}
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