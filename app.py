from flask import Flask, render_template, jsonify, request, send_file, Response, stream_with_context, redirect, url_for, session
try:
    from kiteconnect import KiteConnect
except Exception:
    KiteConnect = None
import subprocess
import os
import signal
import sys
import time
import re
import socket
import hmac
import json
import threading
from datetime import datetime, timedelta, timezone
from werkzeug.security import check_password_hash
try:
    import pandas as pd
except Exception:
    pd = None
from scripts.editable_config import load_editable_config, save_editable_config

app = Flask(__name__)

WORKDIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(WORKDIR, "scripts")
STRATEGY_SCRIPT = os.path.join(SCRIPTS_DIR, "v28_strategy_main.py")
KITE_TEST_SCRIPT = os.path.join(SCRIPTS_DIR, "test_kite_login.py")
WEEKLY_OPTIONS_SCRIPT = os.path.join(SCRIPTS_DIR, "nifty_weekly_options_daily_files_only.py")
PID_FILE = os.path.join(WORKDIR, "strategy.pid")
ENV_FILE = os.path.join(WORKDIR, ".env")
IST = timezone(timedelta(hours=5, minutes=30))

SUMMARY_MARKET_CONTEXT_FIELDS = [
    "nifty_prev_close",
    "nifty_open",
    "nifty_high",
    "nifty_high_time",
    "nifty_low",
    "nifty_low_time",
    "nifty_close",
    "nifty_change",
    "gap_type",
    "gap_points",
    "gap_pct",
    "cpr_width_pct",
    "cpr_type",
]


def ist_now_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S") + " IST"


def local_server_ip() -> str:
    """Return the local address other devices can use to reach this server."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except OSError:
        try:
            addresses = socket.gethostbyname_ex(socket.gethostname())[2]
            for address in addresses:
                if not address.startswith("127."):
                    return address
        except OSError:
            pass
    return "127.0.0.1"


def write_env_var(key: str, value: str) -> None:
    # write or update a simple .env file in WORKDIR
    lines = []
    if os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
        except Exception:
            lines = []
    out = []
    found = False
    for line in lines:
        if not line or line.strip().startswith("#") or "=" not in line:
            out.append(line)
            continue
        k, _ = line.split("=", 1)
        if k.strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    try:
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except Exception:
        pass


def load_env_file(env_path: str = None) -> None:
    if env_path is None:
        env_path = ENV_FILE
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8-sig") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                # Handle UTF-8 BOM so first key like API_KEY loads correctly.
                key = key.strip().lstrip("\ufeff")
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ[key] = value
    except Exception:
        pass


# Load .env into environment at startup so `API_KEY`/`API_SECRET` are available
load_env_file()

DEFAULT_STRATEGY_LOG_DIR = os.path.join(WORKDIR, "logs")
STRATEGY_LOG_DIR = os.path.abspath(
    os.environ.get("STRATEGY_LOG_DIR", DEFAULT_STRATEGY_LOG_DIR).strip()
    or DEFAULT_STRATEGY_LOG_DIR
)
LOGS_DIR = STRATEGY_LOG_DIR
LOG_FILE = os.path.join(STRATEGY_LOG_DIR, "strategy.log")

# Login credentials come only from .env: a password hash, never the password.
# Create them with `python scripts/set_login_password.py`. There are no built-in
# defaults: a missing or known-public value stops the app instead of exposing it.
LOGIN_USERNAME = os.environ.get("LOGIN_USERNAME", "").strip()
LOGIN_PASSWORD_HASH = os.environ.get("LOGIN_PASSWORD_HASH", "").strip()
APP_SECRET_KEY = os.environ.get("APP_SECRET_KEY", "").strip()
_PUBLIC_SECRET_KEYS = {"change-me-login-secret", "REPLACE_WITH_APP_SECRET_KEY"}
if not LOGIN_USERNAME or not LOGIN_PASSWORD_HASH or len(APP_SECRET_KEY) < 32 or APP_SECRET_KEY in _PUBLIC_SECRET_KEYS:
    sys.exit(
        "Login is not configured: LOGIN_USERNAME, LOGIN_PASSWORD_HASH and a random "
        "APP_SECRET_KEY (32+ characters) must be set in .env.\n"
        "Run: python scripts/set_login_password.py"
    )

app.secret_key = APP_SECRET_KEY
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(hours=6),
    SESSION_COOKIE_HTTPONLY=True,
    # Browsers do not send a Lax cookie on cross-site form posts or fetches, so
    # another website cannot trigger dashboard actions in a logged-in browser.
    SESSION_COOKIE_SAMESITE="Lax",
    # Send the login cookie only over HTTPS (the site is served by nginx with TLS).
    # For local testing over plain http://<LAN-IP>:5000, set SESSION_COOKIE_SECURE=false in .env.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "true").strip().lower()
    not in {"0", "false", "no", "off"},
)

# Routes reachable without logging in. Everything else requires a session.
# The login page also describes the site (for visitors and URL classifiers such as
# Zscaler); "robots_txt" lets crawlers read it and nothing else.
PUBLIC_ENDPOINTS = {"login_page", "static", "robots_txt"}
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")

LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 300
_login_failures: dict = {}
_login_failures_lock = threading.Lock()


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or session.get("logged_in"):
        return None
    if request.endpoint == "kite_callback" and request.args.get("request_token"):
        # Kite redirects to the Redirect URL registered for the app. If that address
        # differs from the one the dashboard was opened on, this browser has no session
        # here. Keep the one-time token briefly and save it right after login here.
        session["pending_kite_request_token"] = request.args["request_token"]
        session["pending_kite_at"] = time.time()
        return redirect(url_for("login_page", kite="pending"))
    if request.path.startswith(("/api/", "/stream-logs", "/logs")):
        return jsonify({"ok": False, "msg": "login required"}), 401
    return redirect(url_for("login_page"))


ACCESS_TOKEN_SAVED_AT_KEY = "ACCESS_TOKEN_SAVED_AT"
PENDING_KITE_TOKEN_MAX_AGE_SECONDS = 300


def save_kite_access_token(access_token: str) -> None:
    """Persist a new Kite access token together with when it was issued."""
    saved_at = datetime.now(IST).isoformat(timespec="seconds")
    os.environ["ACCESS_TOKEN"] = access_token
    os.environ[ACCESS_TOKEN_SAVED_AT_KEY] = saved_at
    write_env_var("ACCESS_TOKEN", access_token)
    write_env_var(ACCESS_TOKEN_SAVED_AT_KEY, saved_at)


def kite_token_status() -> dict:
    """Describe the saved Kite token. Kite clears access tokens at 06:00 IST each day."""
    load_env_file()  # pick up a token saved by another process (e.g. generate_token.py)
    if not os.environ.get("ACCESS_TOKEN", "").strip():
        return {"state": "missing", "message": "No Kite token saved. Click Open Kite Login."}
    try:
        saved = datetime.fromisoformat(os.environ.get(ACCESS_TOKEN_SAVED_AT_KEY, "").strip()).astimezone(IST)
    except ValueError:
        return {"state": "unknown", "message": "Kite token age unknown. Log in once to start tracking it."}
    expires = saved.replace(hour=6, minute=0, second=0, microsecond=0)
    if expires <= saved:
        expires += timedelta(days=1)
    status = {"saved_at": saved.isoformat(), "expires_at": expires.isoformat()}
    if datetime.now(IST) >= expires:
        return {**status, "state": "expired",
                "message": f"Kite token from {saved:%d %b %H:%M} expired at {expires:%d %b %H:%M} IST. Click Open Kite Login."}
    return {**status, "state": "valid",
            "message": f"Kite token saved {saved:%d %b %H:%M} IST, valid until {expires:%d %b %H:%M} IST."}


def safe_process_name(name):
    """Strategy process names become file names; allow only plain identifiers."""
    name = str(name or "").strip()
    return name if SAFE_NAME_RE.fullmatch(name) else None


def read_pid() -> int:
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def pid_path_for(name: str) -> str:
    return os.path.join(WORKDIR, f"strategy_{name}.pid")


def log_path_for(name: str) -> str:
    return os.path.join(WORKDIR, f"strategy_{name}.log")


def strategy_python_executable() -> str:
    """Use the project's virtual environment for strategy subprocesses when available."""
    venv_python = os.path.join(WORKDIR, "venv", "Scripts", "python.exe")
    if os.path.isfile(venv_python):
        return venv_python
    return sys.executable or "python3"


def strategy_process_options() -> dict:
    """Create a Windows process group so Ctrl-Break reaches the strategy child."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {}


def current_log_pointer_for(name: str) -> str:
    """File that stores the path to the active run log for a named strategy."""
    return os.path.join(LOGS_DIR, f"strategy_{name}.logpath")


def legacy_log_pointer_for(name: str) -> str:
    """Return the pre-STRATEGY_LOG_DIR location for a pointer file."""
    return os.path.join(WORKDIR, f"strategy_{name}.logpath")


def get_active_log_path(name: str) -> str:
    """Return the current active log path from the pointer file, fallback to legacy path."""
    pointer = current_log_pointer_for(name)
    legacy_pointer = legacy_log_pointer_for(name)
    pointer_to_read = pointer if os.path.exists(pointer) else legacy_pointer
    try:
        if os.path.exists(pointer_to_read):
            with open(pointer_to_read, "r", encoding="utf-8") as f:
                p = f.read().strip()
            if p:
                if pointer_to_read == legacy_pointer and pointer != legacy_pointer:
                    os.makedirs(LOGS_DIR, exist_ok=True)
                    os.replace(legacy_pointer, pointer)
                return p
    except Exception:
        pass
    return log_path_for(name)


def set_active_log_path(name: str, path: str) -> None:
    """Persist the active log path so all routes can find the current run's log."""
    pointer = current_log_pointer_for(name)
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(pointer, "w", encoding="utf-8") as f:
            f.write(path)
    except Exception:
        pass


def build_run_log_filename(name: str, mode: str, dt: datetime = None) -> str:
    """Build a log filename matching the backtest/papertrade Excel naming convention."""
    if dt is None:
        dt = datetime.now(IST)
    mode_str = str(mode or "").upper()
    if "PAPERTRADE" in mode_str:
        prefix = "papertrade"
    elif "BACKTEST" in mode_str:
        prefix = "backtest"
    elif "LIVE" in mode_str:
        prefix = "livetrade"
    else:
        prefix = "strategy"
    date_str = dt.strftime("%d%m%Y")
    # Backtest filenames omit the time component (matches Excel output naming)
    if prefix == "backtest":
        return f"{prefix}_{date_str}_{name}.txt"
    time_str = dt.strftime("%H%M%S")
    return f"{prefix}_{date_str}_{time_str}_{name}.txt"


def mode_path_for(name: str) -> str:
    return os.path.join(WORKDIR, f"strategy_{name}.mode")


def write_mode(name: str, mode: str) -> None:
    try:
        with open(mode_path_for(name), "w", encoding="utf-8") as f:
            f.write(str(mode or "").strip())
    except Exception:
        pass


def read_mode(name: str) -> str:
    try:
        with open(mode_path_for(name), "r", encoding="utf-8") as f:
            return str(f.read().strip())
    except Exception:
        return ""


def remove_mode(name: str) -> None:
    try:
        os.remove(mode_path_for(name))
    except Exception:
        pass


def write_pid(pid: int) -> None:
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(pid))


def remove_pid() -> None:
    try:
        os.remove(PID_FILE)
    except Exception:
        pass


def is_pid_running(pid: int) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            # /NH removes header; when not found, tasklist returns an INFO line.
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                errors="ignore",
            )
            line = (proc.stdout or "").strip()
            if not line or line.upper().startswith("INFO:"):
                return False
            return f'"{pid}"' in line
        # On Linux, exited children can remain as zombies until reaped.
        # Zombies should not be treated as running strategies.
        try:
            stat_path = f"/proc/{pid}/stat"
            if os.path.exists(stat_path):
                with open(stat_path, "r", encoding="utf-8", errors="ignore") as sf:
                    parts = sf.read().split()
                if len(parts) > 2 and parts[2] == "Z":
                    return False
        except Exception:
            pass
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def is_running() -> bool:
    pid = read_pid()
    if not pid:
        return False
    running = is_pid_running(pid)
    if not running:
        remove_pid()
    return running


def claim_start_slot(pidfile: str) -> str:
    """Atomically reserve a strategy start slot before spawning its process."""
    claim_file = f"{pidfile}.starting"
    try:
        fd = os.open(claim_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return claim_file
    except FileExistsError:
        raise RuntimeError("already_running")


def live_strategy_lock_is_held(script: str) -> bool:
    """Check the shared LIVE_ORDER lock without starting a child."""
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
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    except (OSError, IOError):
        return False
    finally:
        if handle is not None:
            handle.close()


def start_strategy(args: list = None) -> tuple[bool, str]:
    if is_running():
        return False, "already_running"
    args = args or []
    python = strategy_python_executable()
    # Run child Python in unbuffered mode so prints appear immediately in redirected logs
    cmd = [python, "-u", STRATEGY_SCRIPT] + args
    logf = open(LOG_FILE, "a", buffering=1)
    try:
        # write a starter line to log so it's easy to detect startup
        try:
            logf.write(f"\n=== starting: {' '.join(cmd)} pid-request at {ist_now_str()} ===\n")
            logf.flush()
        except Exception:
            pass
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            cmd,
            cwd=WORKDIR,
            stdout=logf,
            stderr=subprocess.STDOUT,
            env=env,
            close_fds=True,
            **strategy_process_options(),
        )
        write_pid(proc.pid)
        # close parent handle; child keeps the descriptor
        try:
            logf.close()
        except Exception:
            pass
        return True, f"started:{proc.pid}"
    except Exception as e:
        return False, str(e)


def start_named_strategy(name: str, script: str, args: list = None, mode: str = "") -> tuple[bool, str]:
    args = args or []
    pidfile = pid_path_for(name)
    claim_file = None
    # check if already running
    try:
        if os.path.exists(pidfile):
            with open(pidfile, 'r', encoding='utf-8') as f:
                pid = int(f.read().strip() or 0)
            if is_pid_running(pid):
                return True, f"resumed:{pid}"
            try:
                os.remove(pidfile)
            except Exception:
                pass
    except Exception:
        pass
    if str(mode or "").upper() == "LIVE_ORDER" and live_strategy_lock_is_held(script):
        return True, "resumed:existing-live-session"
    try:
        claim_file = claim_start_slot(pidfile)
    except RuntimeError:
        return True, "resumed:start-in-progress"

    # Build a timestamped log file in the logs/ folder (same naming as backtest/papertrade xlsx)
    now_ist = datetime.now(IST)
    log_filename = build_run_log_filename(name, mode, now_ist)
    os.makedirs(LOGS_DIR, exist_ok=True)
    logfile = os.path.join(LOGS_DIR, log_filename)
    set_active_log_path(name, logfile)

    python = strategy_python_executable()
    cmd = [python, "-u", script] + args
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        # Backtest logs use a fixed filename (no time component) so overwrite on each run.
        # Papertrade/live filenames include a timestamp so append is fine (unique per run).
        log_open_mode = "w" if "BACKTEST" in str(mode or "").upper() else "a"
        try:
            logf = open(logfile, log_open_mode, buffering=1, encoding='utf-8')
        except Exception:
            logf = open(logfile, log_open_mode, buffering=1)
        try:
            logf.write(f"\n=== starting: {' '.join(cmd)} pid-request at {ist_now_str()} ===\n")
            logf.flush()
        except Exception:
            pass
        proc = subprocess.Popen(
            cmd,
            cwd=WORKDIR,
            stdout=logf,
            stderr=subprocess.STDOUT,
            env=env,
            close_fds=True,
            **strategy_process_options(),
        )
        try:
            with open(pidfile, 'w', encoding='utf-8') as pf:
                pf.write(str(proc.pid))
        except Exception:
            pass
        write_mode(name, mode)
        try:
            logf.close()
        except Exception:
            pass
        try:
            os.remove(claim_file)
        except Exception:
            pass
        return True, f"started:{proc.pid}"
    except Exception as e:
        if claim_file:
            try:
                os.remove(claim_file)
            except Exception:
                pass
        return False, str(e)


def _wait_for_pid_exit(pid: int, attempts: int = 20, interval_seconds: float = 0.5) -> bool:
    for _ in range(max(1, int(attempts))):
        time.sleep(max(0.05, float(interval_seconds)))
        if not is_pid_running(pid):
            return True
    return not is_pid_running(pid)


def _request_graceful_stop(pid: int) -> bool:
    # Prefer console-style interrupts so strategy cleanup handlers can run and
    # print shutdown summaries before process exit.
    signal_steps = []
    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        signal_steps.append((signal.SIGBREAK, 24))
    signal_steps.append((signal.SIGINT, 36))

    for sig, wait_attempts in signal_steps:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except Exception:
            continue
        if _wait_for_pid_exit(pid, attempts=wait_attempts, interval_seconds=0.5):
            return True
    return not is_pid_running(pid)


def _force_terminate_pid(pid: int) -> bool:
    try:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    errors="ignore",
                )
            except Exception:
                pass
            return not is_pid_running(pid)

        # POSIX fallback
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except Exception:
            pass
        if _wait_for_pid_exit(pid, attempts=10, interval_seconds=0.5):
            return True
        if hasattr(signal, "SIGKILL"):
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        return not is_pid_running(pid)
    except Exception:
        return not is_pid_running(pid)


def stop_strategy() -> tuple[bool, str]:
    pid = read_pid()
    if not pid:
        return False, "not_running"
    try:
        if _request_graceful_stop(pid):
            remove_pid()
            return True, "stopped"
        if _force_terminate_pid(pid):
            remove_pid()
            return True, "terminated"
        return False, "stop_timeout"
    except ProcessLookupError:
        remove_pid()
        return False, "process_not_found"
    except Exception as e:
        return False, str(e)


def stop_named_strategy(name: str) -> tuple[bool, str]:
    pidfile = pid_path_for(name)
    try:
        if not os.path.exists(pidfile):
            remove_mode(name)
            return False, 'not_running'
        with open(pidfile, 'r', encoding='utf-8') as f:
            pid = int(f.read().strip() or 0)
    except Exception:
        remove_mode(name)
        return False, 'not_running'
    try:
        if _request_graceful_stop(pid):
            try:
                os.remove(pidfile)
            except Exception:
                pass
            remove_mode(name)
            return True, 'stopped'

        if _force_terminate_pid(pid):
            try:
                os.remove(pidfile)
            except Exception:
                pass
            remove_mode(name)
            return True, 'terminated'

        return False, 'stop_timeout'
    except Exception as e:
        return False, str(e)


@app.route("/")
def index():
    if not session.get("logged_in"):
        return redirect(url_for("login_page"))
    return render_template(
        "index.html",
        client_ip=local_server_ip(),
        server_time=ist_now_str(),
        server_time_epoch=time.time(),
        market_holidays=load_market_holidays(),
    )


@app.route("/robots.txt")
def robots_txt():
    """Let crawlers/URL classifiers read the login page (which describes the site); nothing else."""
    body = "User-agent: *\nAllow: /login$\nDisallow: /\n"
    return Response(body, mimetype="text/plain")


@app.route("/api/editable-config", methods=["GET", "POST"])
def api_editable_config():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "login required"}), 401
    if request.method == "GET":
        return jsonify({"ok": True, "config": load_editable_config()})
    try:
        data = request.get_json() or {}
        config = save_editable_config(data)
        return jsonify({"ok": True, "config": config})
    except (TypeError, ValueError, OSError) as exc:
        return jsonify({"ok": False, "msg": str(exc)}), 400


@app.route("/api/market/nifty50")
def api_market_nifty50():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "login required"}), 401

    kite, kite_err = _build_kite_client_from_env()
    if kite is None:
        return jsonify({"ok": False, "msg": f"Live NIFTY 50 unavailable: {kite_err}"}), 400

    try:
        quote = kite.quote(["NSE:NIFTY 50"]).get("NSE:NIFTY 50") or {}
        last_price = to_float(quote.get("last_price"), 0.0)
        if last_price <= 0:
            return jsonify({"ok": False, "msg": "Live NIFTY 50 quote is empty"}), 502
        previous_close = to_float((quote.get("ohlc") or {}).get("close"), 0.0)
        change = last_price - previous_close if previous_close > 0 else None
        change_pct = (change / previous_close * 100) if previous_close > 0 else None
        return jsonify({
            "ok": True,
            "symbol": "NIFTY 50",
            "last_price": last_price,
            "change": change,
            "change_pct": change_pct,
            "updated_at": ist_now_str(),
        })
    except Exception as exc:
        return jsonify({"ok": False, "msg": f"Live NIFTY 50 fetch failed: {exc}"}), 502


@app.route("/api/market/gift-nifty")
def api_market_gift_nifty():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "login required"}), 401

    kite, kite_err = _build_kite_client_from_env()
    if kite is None:
        return jsonify({"ok": False, "msg": f"Gift Nifty unavailable: {kite_err}"}), 400

    try:
        quote_keys = ["NSEIX:GIFT NIFTY", "NSEIX:GIFTNIFTY"]
        quotes = kite.quote(quote_keys)
        quote = next((quotes.get(key) for key in quote_keys if quotes.get(key)), {})
        last_price = to_float(quote.get("last_price"), 0.0)
        if last_price <= 0:
            return jsonify({"ok": False, "msg": "Gift Nifty quote is empty"}), 502
        previous_close = to_float((quote.get("ohlc") or {}).get("close"), 0.0)
        change = last_price - previous_close if previous_close > 0 else None
        change_pct = (change / previous_close * 100) if previous_close > 0 else None
        return jsonify({
            "ok": True,
            "symbol": "Gift Nifty",
            "last_price": last_price,
            "change": change,
            "change_pct": change_pct,
            "updated_at": ist_now_str(),
        })
    except Exception as exc:
        return jsonify({"ok": False, "msg": f"Gift Nifty fetch failed: {exc}"}), 502

@app.route("/login", methods=["GET", "POST"])
def login_page():
    notice = ""
    if session.get("pending_kite_request_token"):
        notice = (
            f"Kite login returned to this dashboard address ({request.host}), but this browser "
            "is not signed in here. Sign in to finish saving the Kite token. To avoid this, "
            "open the dashboard at the same address as the Kite app's Redirect URL."
        )
    if request.method == "POST":
        client = request.remote_addr or "unknown"
        now = time.time()
        with _login_failures_lock:
            failures, locked_until = _login_failures.get(client, (0, 0.0))
        if locked_until > now:
            wait_min = int((locked_until - now) // 60) + 1
            return render_template("login.html", error=f"Too many failed attempts. Try again in {wait_min} minute(s).", notice=notice), 429
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        # Check both fields every time so a wrong username takes as long as a wrong password.
        user_ok = hmac.compare_digest(username.encode(), LOGIN_USERNAME.encode())
        password_ok = check_password_hash(LOGIN_PASSWORD_HASH, password)
        if user_ok and password_ok:
            with _login_failures_lock:
                _login_failures.pop(client, None)
            pending_token = session.get("pending_kite_request_token")
            pending_age = now - float(session.get("pending_kite_at") or 0)
            session.clear()
            session.permanent = True
            session["logged_in"] = True
            if pending_token and pending_age <= PENDING_KITE_TOKEN_MAX_AGE_SECONDS:
                return redirect(url_for("kite_callback", request_token=pending_token))
            return redirect(url_for("index"))
        with _login_failures_lock:
            failures += 1
            _login_failures[client] = (
                (0, now + LOGIN_LOCKOUT_SECONDS) if failures >= LOGIN_MAX_FAILURES else (failures, 0.0)
            )
        return render_template("login.html", error="Invalid username or password", notice=notice)
    if session.get("logged_in"):
        return redirect(url_for("index"))
    return render_template("login.html", error="", notice=notice)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/api/status")
def api_status():
    name = request.args.get('name')
    if name and not safe_process_name(name):
        return jsonify({"ok": False, "msg": "invalid name"}), 400
    mode = ""
    if name:
        pidfile = pid_path_for(name)
        logpath = get_active_log_path(name)
        mode = read_mode(name)
        pid = 0
        try:
            with open(pidfile, 'r', encoding='utf-8') as f:
                pid = int(f.read().strip() or 0)
        except Exception:
            pid = 0
        running = False
        try:
            if pid:
                running = is_pid_running(pid)
                if not running:
                    try:
                        os.remove(pidfile)
                    except Exception:
                        pass
                    remove_mode(name)
                    mode = ""
        except Exception:
            running = False
        log_info = {"exists": False, "size": 0, "mtime": None}
        try:
            if os.path.exists(logpath):
                stat = os.stat(logpath)
                log_info = {"exists": True, "size": stat.st_size, "mtime": stat.st_mtime}
        except Exception:
            pass
    else:
        pid = read_pid()
        running = is_running()
        log_info = {"exists": False, "size": 0, "mtime": None}
        try:
            if os.path.exists(LOG_FILE):
                stat = os.stat(LOG_FILE)
                log_info = {"exists": True, "size": stat.st_size, "mtime": stat.st_mtime}
        except Exception:
            pass
    # also report masked access token presence
    at = os.environ.get("ACCESS_TOKEN", "")
    at_mask = "" if not at else (at[:6] + "..." + at[-6:])
    api_key = os.environ.get("API_KEY", "")
    return jsonify({"running": running, "pid": pid, "mode": mode, "log": log_info, "access_token_masked": at_mask,
                    "api_key_present": bool(api_key), "kite_token": kite_token_status()})


# The only scripts the dashboard may start, and the options it may pass. The
# request must never choose an arbitrary program or interpreter flag.
STRATEGY_SCRIPTS = {"scripts/v28_strategy_main.py", "scripts/v30_strategy_main.py"}
ALLOWED_START_SCRIPTS = STRATEGY_SCRIPTS | {
    "scripts/nifty_weekly_options_daily_files_only.py",
    "scripts/run_backtest_range.py",
}
ALLOWED_START_FLAGS = {
    "--option-data-source", "--start-date", "--end-date", "--expiry-date",
    "--strategy", "--after-hours-replay",
}
ALLOWED_MODES = {"BACKTEST", "PAPERTRADE_LIVE", "LIVE_ORDER"}
SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,80}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_start_script(script) -> str:
    rel = os.path.normpath(str(script or "")).replace("\\", "/")
    return rel if rel in ALLOWED_START_SCRIPTS else ""


def validate_start_args(args) -> str:
    """Return an error message, or "" when every argument is allowed."""
    if not isinstance(args, list) or len(args) > 20:
        return "args must be a short list"
    previous = ""
    for arg in args:
        if not isinstance(arg, str) or not SAFE_ARG_RE.fullmatch(arg):
            return f"invalid argument: {arg!r}"
        if arg.startswith("-") and arg not in ALLOWED_START_FLAGS:
            return f"option not allowed: {arg}"
        if previous == "--strategy" and normalize_start_script(arg) not in STRATEGY_SCRIPTS:
            return f"strategy not allowed: {arg}"
        previous = arg
    return ""


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    # Support multi-strategy: { name, script, mode, date, args }
    name = data.get('name')
    if name and not safe_process_name(name):
        return jsonify({"ok": False, "msg": "invalid name"}), 400
    script = data.get('script')
    if script:
        script = normalize_start_script(script)
        if not script:
            return jsonify({"ok": False, "msg": "script not allowed"}), 400
        script = os.path.join(WORKDIR, script)
    args = list(data.get("args") or [])
    args_error = validate_start_args(args)
    if args_error:
        return jsonify({"ok": False, "msg": args_error}), 400
    # Every mode needs Kite (backtests fetch the NIFTY 50 index from it), so do not
    # start a process that would only fail at its first API call.
    token = kite_token_status()
    if token["state"] in {"missing", "expired"}:
        return jsonify({"ok": False, "msg": token["message"]}), 409
    mode = str(data.get("mode") or "").strip().upper()
    if mode:
        if mode not in ALLOWED_MODES:
            return jsonify({"ok": False, "msg": "invalid mode"}), 400
        args.extend(["--mode", mode])
    date = str(data.get("date") or "").strip()
    if date:
        if not DATE_RE.fullmatch(date):
            return jsonify({"ok": False, "msg": "invalid date"}), 400
        args.extend(["--date", date])
    if name:
        # if script not supplied, default to STRATEGY_SCRIPT
        if not script:
            script = STRATEGY_SCRIPT
        ok, msg = start_named_strategy(name, script, args, mode=mode)
    else:
        ok, msg = start_strategy(args)
    pid = 0
    if ok and name:
        pid = read_pid() if name == "" else 0
        try:
            with open(pid_path_for(name), "r", encoding="utf-8") as pf:
                pid = int(pf.read().strip() or 0)
        except Exception:
            pid = 0
    return jsonify({"ok": ok, "msg": msg, "pid": pid})


def tail_file(path: str, lines: int = 200) -> str:
    try:
        with open(path, "rb") as f:
            avg_line_length = 100
            to_read = lines * avg_line_length
            try:
                f.seek(-to_read, os.SEEK_END)
            except Exception:
                f.seek(0)
            data = f.read().decode(errors="ignore")
            parts = data.splitlines()
            return "\n".join(parts[-lines:])
    except Exception:
        return ""


def _parse_shutdown_summary_from_log(log_text: str) -> dict:
    marker = "========== SESSION SUMMARY (SHUTDOWN) =========="
    if not log_text:
        return {}
    idx = log_text.rfind(marker)
    if idx < 0:
        return {}

    lines = log_text[idx:].splitlines()
    summary = {
        "run_mode": "",
        "trigger": "",
        "total_trades": None,
        "winning_trades": None,
        "loss_or_flat_trades": None,
        "net_pnl_rs": None,
        "open_position": "",
        "excel_file": "",
        "raw": "",
    }
    raw_lines = []
    for line in lines:
        raw_lines.append(line)
        if "SHUTDOWN COMPLETE" in line:
            break
        line_clean = line.strip()
        if line_clean.startswith("Run mode:"):
            summary["run_mode"] = line_clean.split(":", 1)[1].strip()
        elif line_clean.startswith("Trigger"):
            summary["trigger"] = line_clean.split(":", 1)[1].strip()
        elif line_clean.startswith("Total trades"):
            try:
                summary["total_trades"] = int(line_clean.split(":", 1)[1].strip())
            except Exception:
                pass
        elif line_clean.startswith("Winning trades"):
            try:
                summary["winning_trades"] = int(line_clean.split(":", 1)[1].strip())
            except Exception:
                pass
        elif line_clean.startswith("Losing/flat trades"):
            try:
                summary["loss_or_flat_trades"] = int(line_clean.split(":", 1)[1].strip())
            except Exception:
                pass
        elif line_clean.startswith("Net P&L (Rs)"):
            try:
                summary["net_pnl_rs"] = float(line_clean.split(":", 1)[1].strip())
            except Exception:
                pass
        elif line_clean.startswith("Open position"):
            summary["open_position"] = line_clean.split(":", 1)[1].strip()
        elif line_clean.startswith("Excel file"):
            summary["excel_file"] = line_clean.split(":", 1)[1].strip()

    summary["raw"] = "\n".join(raw_lines).strip()
    return summary


def _wait_for_shutdown_summary(log_path: str, before_mtime: float = 0.0, timeout_seconds: float = 10.0) -> dict:
    deadline = time.time() + max(0.2, float(timeout_seconds))
    while time.time() < deadline:
        try:
            if not os.path.exists(log_path):
                time.sleep(0.2)
                continue
            current_mtime = os.path.getmtime(log_path)
            if before_mtime and current_mtime <= before_mtime:
                time.sleep(0.2)
                continue
            log_tail = tail_file(log_path, lines=1500)
            parsed = _parse_shutdown_summary_from_log(log_tail)
            if parsed:
                return parsed
        except Exception:
            pass
        time.sleep(0.2)
    return {}


def _extract_latest_excel_name_from_log(log_text: str) -> str:
    if not log_text:
        return ""
    # Matches lines like:
    # [Excel updated] logs\papertrade_....xlsx
    # [Excel updated after shutdown close] logs\papertrade_....xlsx
    matches = re.findall(r"\[Excel updated(?: after shutdown close)?\]\s+([^\r\n]+\.xlsx)", log_text, flags=re.IGNORECASE)
    if not matches:
        return ""
    last_path = str(matches[-1]).strip().strip('"').strip("'")
    return os.path.basename(last_path)


def _append_forced_shutdown_summary(log_path: str, trigger_reason: str = "FORCED_TERMINATE") -> dict:
    """Write a fallback shutdown summary when process was killed before cleanup."""
    try:
        log_tail = tail_file(log_path, lines=2000)
    except Exception:
        log_tail = ""

    # If a normal summary already exists, reuse it.
    existing = _parse_shutdown_summary_from_log(log_tail)
    if existing:
        return existing

    excel_name = _extract_latest_excel_name_from_log(log_tail)
    total_trades = 0
    win_trades = 0
    loss_or_flat = 0
    net_pnl_rs = 0.0

    if excel_name and pd is not None:
        try:
            payload, err = parse_trade_details_from_xlsx(excel_name, row_limit=5)
            if not err:
                summary = payload.get("summary") or {}
                total_trades = int(payload.get("total_rows") or 0)
                win_trades = int(summary.get("win_trades") or 0)
                loss_or_flat = int(summary.get("loss_or_flat_trades") or 0)
                net_pnl_rs = float(summary.get("total_pnl_rs") or 0.0)
        except Exception:
            pass

    marker_lines = [
        "========== SESSION SUMMARY (SHUTDOWN) ==========",
        "Run mode: UNKNOWN",
        f"Trigger : {trigger_reason}",
        f"Total trades       : {total_trades}",
        f"Winning trades     : {win_trades}",
        f"Losing/flat trades : {loss_or_flat}",
        f"Net P&L (Rs)       : {net_pnl_rs:.2f}",
        "Open position      : UNKNOWN",
    ]
    if excel_name:
        marker_lines.append(f"Excel file         : logs\\{excel_name}")
    marker_lines.extend([
        "==============================================",
        "========== SHUTDOWN COMPLETE ==========",
    ])
    block = "\n" + "\n".join(marker_lines) + "\n"

    try:
        with open(log_path, "a", encoding="utf-8", errors="ignore") as f:
            f.write(block)
            f.flush()
    except Exception:
        pass

    return _parse_shutdown_summary_from_log(block)


def parse_kite_user_details(output: str) -> dict:
    details = {}
    if not output:
        return details

    patterns = {
        "user_id": r"User ID:\s*(.+)",
        "user_name": r"User Name:\s*(.+)",
        "email": r"Email:\s*(.+)",
        "broker": r"Broker:\s*(.+)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, output)
        if m:
            details[key] = m.group(1).strip()
    return details


def to_float(value, default=0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def mtf_interval_from_gap_pct(gap_pct) -> str:
    editable_config = load_editable_config()
    if editable_config.get("MTF_MODE") == "MANUAL":
        return str(editable_config.get("MTF_INTERVAL") or "")
    try:
        gap_value = float(gap_pct)
    except (TypeError, ValueError):
        return ""
    return "1min" if abs(gap_value) < 0.20 else "5min"


def cpr_type_from_width_pct(width_pct) -> str:
    try:
        width_value = float(width_pct)
    except (TypeError, ValueError):
        return ""
    if width_value < 0.10:
        return "NARROW"
    if width_value <= 0.20:
        return "NORMAL"
    return "WIDE"


def _extract_trade_date_from_filename(safe_name: str) -> str:
    """Extract YYYY-MM-DD from names like backtest_ddmmyyyy_*.xlsx."""
    name = os.path.basename(str(safe_name or "")).strip().lower()
    m = re.match(r"^(?:backtest|papertrade|livetrade)_(\d{8})(?:_|\.|$)", name)
    if not m:
        return ""
    raw = m.group(1)
    try:
        return datetime.strptime(raw, "%d%m%Y").date().isoformat()
    except Exception:
        return ""


def _read_trade_date_from_summary_sheet(path: str) -> str:
    if pd is None:
        return ""
    try:
        df = pd.read_excel(path, sheet_name="summary")
    except Exception:
        return ""
    if df is None or df.empty:
        return ""

    cols = [str(c).strip().lower() for c in df.columns]
    metric_col = None
    value_col = None
    for idx, col in enumerate(cols):
        if col == "metric":
            metric_col = df.columns[idx]
        if col == "value":
            value_col = df.columns[idx]
    if metric_col is None or value_col is None:
        return ""

    try:
        row = df[df[metric_col].astype(str).str.strip().str.lower() == "trade_date"].head(1)
        if row.empty:
            return ""
        raw = str(row.iloc[0][value_col]).strip()
        if not raw:
            return ""
        return datetime.fromisoformat(raw).date().isoformat()
    except Exception:
        return ""


def _read_summary_market_context(path: str) -> dict:
    out = {k: "" for k in SUMMARY_MARKET_CONTEXT_FIELDS}
    if pd is None:
        return out
    try:
        df = pd.read_excel(path, sheet_name="summary")
    except Exception:
        return out
    if df is None or df.empty:
        return out

    cols = [str(c).strip().lower() for c in df.columns]
    metric_col = None
    value_col = None
    for idx, col in enumerate(cols):
        if col == "metric":
            metric_col = df.columns[idx]
        if col == "value":
            value_col = df.columns[idx]
    if metric_col is None or value_col is None:
        return out

    try:
        metric_map = {}
        for _, row in df.iterrows():
            key = str(row.get(metric_col) or "").strip().lower()
            if not key:
                continue
            metric_map[key] = normalize_json_value(row.get(value_col))
        for key in SUMMARY_MARKET_CONTEXT_FIELDS:
            val = metric_map.get(key.lower(), "")
            out[key] = "" if val is None else val
        if out.get("cpr_width_pct", "") != "":
            derived_cpr_type = cpr_type_from_width_pct(out["cpr_width_pct"])
            if derived_cpr_type:
                out["cpr_type"] = derived_cpr_type
    except Exception:
        pass
    return out


def _merge_market_context(contexts: list[dict]) -> dict:
    merged = {k: "" for k in SUMMARY_MARKET_CONTEXT_FIELDS}
    if not contexts:
        return merged

    for key in SUMMARY_MARKET_CONTEXT_FIELDS:
        values = []
        for ctx in contexts:
            val = "" if ctx is None else ctx.get(key, "")
            sval = "" if val is None else str(val).strip()
            if sval:
                values.append((sval, val))
        if not values:
            merged[key] = ""
            continue
        unique = {v[0] for v in values}
        if len(unique) == 1:
            merged[key] = values[-1][1]
        else:
            merged[key] = "MULTIPLE"
    return merged
def _build_kite_client_from_env() -> tuple[object, str]:
    if KiteConnect is None:
        return None, "kiteconnect_not_installed"
    api_key = str(os.environ.get("API_KEY", "")).strip()
    access_token = str(os.environ.get("ACCESS_TOKEN", "")).strip()
    if not api_key:
        return None, "missing_api_key"
    if not access_token:
        return None, "missing_access_token"
    try:
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        return kite, ""
    except Exception as exc:
        return None, f"kite_init_failed:{exc}"


def _fetch_gap_from_broker_for_date(kite, trade_date: str, token: int) -> tuple[dict, str]:
    trade_date = str(trade_date or "").strip()
    if not trade_date:
        return {}, "trade_date_unavailable"

    try:
        target_date = datetime.fromisoformat(trade_date).date()
    except Exception:
        return {}, "invalid_trade_date"

    def _fetch_day_rows(day_date):
        from_ts = datetime.combine(day_date, datetime.min.time()).replace(hour=9, minute=15)
        to_ts = datetime.combine(day_date, datetime.min.time()).replace(hour=15, minute=30)
        return kite.historical_data(token, from_ts, to_ts, "minute", continuous=False, oi=False) or []

    try:
        rows_today = _fetch_day_rows(target_date)
    except Exception as exc:
        return {}, f"day_history_failed:{exc}"

    if not rows_today:
        return {}, "no_day_history"

    today_open = to_float(rows_today[0].get("open"), 0.0)
    if today_open <= 0:
        return {}, "invalid_today_open"
    today_high = 0.0
    today_low = 0.0
    today_close = 0.0
    today_high_time = ""
    today_low_time = ""

    def _fmt_ist_time(raw_dt) -> str:
        if raw_dt in (None, ""):
            return ""
        try:
            ts = pd.Timestamp(raw_dt) if pd is not None else raw_dt
            if hasattr(ts, "to_pydatetime"):
                ts = ts.to_pydatetime()
            if getattr(ts, "tzinfo", None) is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts = ts.astimezone(IST)
            return ts.strftime("%H:%M")
        except Exception:
            return ""

    try:
        highs = [to_float(r.get("high"), 0.0) for r in rows_today]
        lows = [to_float(r.get("low"), 0.0) for r in rows_today if to_float(r.get("low"), 0.0) > 0]
        today_high = max(highs) if highs else 0.0
        today_low = min(lows) if lows else 0.0
        # pick last available non-zero close (some historical endpoints may include
        # trailing empty/zero rows) instead of blindly using the final element
        today_close = 0.0
        for r in reversed(rows_today):
            c = to_float(r.get("close"), 0.0)
            if c and c > 0:
                today_close = c
                break

        if today_high > 0:
            for r in rows_today:
                if to_float(r.get("high"), 0.0) == today_high:
                    today_high_time = _fmt_ist_time(r.get("date"))
                    break

        if today_low > 0:
            for r in rows_today:
                if to_float(r.get("low"), 0.0) == today_low:
                    today_low_time = _fmt_ist_time(r.get("date"))
                    break
    except Exception:
        today_high = 0.0
        today_low = 0.0
        today_close = 0.0
        today_high_time = ""
        today_low_time = ""

    prev_close = 0.0
    prev_high = 0.0
    prev_low = 0.0
    prev_trade_date = ""
    prev_date = target_date - timedelta(days=1)
    for _ in range(10):
        if prev_date.weekday() < 5:
            try:
                rows_prev = _fetch_day_rows(prev_date)
            except Exception:
                rows_prev = []
            if rows_prev:
                prev_close = to_float(rows_prev[-1].get("close"), 0.0)
                if prev_close > 0:
                    try:
                        prev_high = max(to_float(r.get("high"), 0.0) for r in rows_prev)
                        lows = [to_float(r.get("low"), 0.0) for r in rows_prev if to_float(r.get("low"), 0.0) > 0]
                        prev_low = min(lows) if lows else 0.0
                    except Exception:
                        prev_high = 0.0
                        prev_low = 0.0
                    prev_trade_date = prev_date.isoformat()
                    break
        prev_date -= timedelta(days=1)

    if prev_close <= 0:
        return {}, "prev_close_unavailable"

    gap_points = round(today_open - prev_close, 2)
    gap_pct = round((gap_points / prev_close) * 100.0, 2)
    gap_type = "FLAT"
    if gap_points > 0:
        gap_type = "GAP_UP"
    elif gap_points < 0:
        gap_type = "GAP_DOWN"

    nifty_change = ""
    if prev_close > 0 and today_close > 0:
        chg_pts = today_close - prev_close
        chg_pct = (chg_pts / prev_close) * 100.0
        sign = "+" if chg_pts >= 0 else ""
        nifty_change = f"{sign}{chg_pts:.0f} pts ({sign}{chg_pct:.2f}%)"

    cpr_pivot = ""
    cpr_bc = ""
    cpr_tc = ""
    cpr_width = ""
    cpr_width_pct = ""
    cpr_type = ""
    if prev_high > 0 and prev_low > 0 and prev_close > 0:
        pivot = (prev_high + prev_low + prev_close) / 3.0
        bc = (prev_high + prev_low) / 2.0
        tc = (2.0 * pivot) - bc
        if bc > tc:
            bc, tc = tc, bc
        width = tc - bc
        width_pct = ((width / pivot) * 100.0) if pivot else 0.0
        cpr_type = cpr_type_from_width_pct(width_pct)
        cpr_pivot = int(round(pivot))
        cpr_bc = int(round(bc))
        cpr_tc = int(round(tc))
        cpr_width = round(width, 2)
        cpr_width_pct = round(width_pct, 2)

    return {
        "trade_date": target_date.isoformat(),
        "prev_trade_date": prev_trade_date,
        "nifty_prev_close": int(round(prev_close)) if prev_close > 0 else "",
        "nifty_open": int(round(today_open)) if today_open > 0 else "",
        "nifty_high": int(round(today_high)) if today_high > 0 else "",
        "nifty_high_time": today_high_time,
        "nifty_low": int(round(today_low)) if today_low > 0 else "",
        "nifty_low_time": today_low_time,
        "nifty_close": int(round(today_close)) if today_close > 0 else "",
        "nifty_change": nifty_change,
        "today_open": round(today_open, 2),
        "prev_close": round(prev_close, 2),
        "gap_type": gap_type,
        "gap_points": gap_points,
        "gap_pct": gap_pct,
        "is_gap_up": bool(gap_points > 0),
        "cpr_pivot": cpr_pivot,
        "cpr_bc": cpr_bc,
        "cpr_tc": cpr_tc,
        "cpr_width": cpr_width,
        "cpr_width_pct": cpr_width_pct,
        "cpr_type": cpr_type,
    }, ""


def _write_gap_to_summary_sheet(path: str, gap: dict) -> tuple[bool, str]:
    try:
        from openpyxl import load_workbook
    except Exception:
        return False, "openpyxl_not_installed"

    try:
        wb = load_workbook(path)
    except Exception as exc:
        return False, f"open_workbook_failed:{exc}"

    ws = wb["summary"] if "summary" in wb.sheetnames else wb.create_sheet("summary")

    if ws.max_row < 1:
        ws.cell(row=1, column=1, value="metric")
        ws.cell(row=1, column=2, value="value")

    header_metric = str(ws.cell(row=1, column=1).value or "").strip().lower()
    header_value = str(ws.cell(row=1, column=2).value or "").strip().lower()
    if header_metric != "metric":
        ws.cell(row=1, column=1, value="metric")
    if header_value != "value":
        ws.cell(row=1, column=2, value="value")

    metric_rows = {}
    for r in range(2, ws.max_row + 1):
        key = str(ws.cell(row=r, column=1).value or "").strip()
        if key:
            metric_rows[key.lower()] = r

    updates = {
        "nifty_prev_close": gap.get("nifty_prev_close"),
        "nifty_open": gap.get("nifty_open"),
        "nifty_high": gap.get("nifty_high"),
        "nifty_high_time": gap.get("nifty_high_time"),
        "nifty_low": gap.get("nifty_low"),
        "nifty_low_time": gap.get("nifty_low_time"),
        "nifty_close": gap.get("nifty_close"),
        "nifty_change": gap.get("nifty_change"),
        "gap_type": gap.get("gap_type"),
        "gap_points": gap.get("gap_points"),
        "gap_pct": gap.get("gap_pct"),
        "is_gap_up": gap.get("is_gap_up"),
        "gap_prev_trade_date": gap.get("prev_trade_date"),
        "gap_data_source": "broker_history",
        "gap_updated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S") + " IST",
    }

    next_row = ws.max_row + 1
    for key, value in updates.items():
        row_idx = metric_rows.get(key.lower())
        if row_idx is None:
            row_idx = next_row
            next_row += 1
            ws.cell(row=row_idx, column=1, value=key)
            metric_rows[key.lower()] = row_idx
        ws.cell(row=row_idx, column=2, value=value)

    try:
        wb.save(path)
    except Exception as exc:
        return False, f"save_failed:{exc}"
    return True, ""


@app.route("/api/logs")
def api_logs():
    name = request.args.get('name')
    if name and not safe_process_name(name):
        return jsonify({"ok": False, "msg": "invalid name"}), 400
    full = request.args.get('full', '').lower() == 'true'
    path = LOG_FILE
    if name:
        path = get_active_log_path(name)
    if not os.path.exists(path):
        return jsonify({"exists": False, "tail": ""})
    
    if full:
        # Return the entire log file
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            return jsonify({"exists": True, "tail": content})
        except Exception as e:
            return jsonify({"exists": False, "tail": f"Error reading log: {str(e)}"})
    else:
        # Return last 2000 lines for initial display
        tail = tail_file(path, lines=2000)
        return jsonify({"exists": True, "tail": tail})


@app.route("/api/weekly-options/files")
def api_weekly_options_files():
    output_dir = os.path.join(WORKDIR, "data", "nifty_options", "daily")
    expiry = request.args.get("expiry", "").strip()
    if expiry and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry):
        return jsonify({"ok": False, "msg": "Invalid expiry date."}), 400
    if not os.path.isdir(output_dir):
        return jsonify({"ok": True, "files": []})

    files = [
        name for name in os.listdir(output_dir)
        if name.lower().endswith(".csv")
        and (not expiry or re.match(r"^nifty_weekly_" + re.escape(expiry) + r"_", name))
        and os.path.isfile(os.path.join(output_dir, name))
    ]
    files = sorted(files)
    file_sizes_mb = {
        name: round(os.path.getsize(os.path.join(output_dir, name)) / (1024 * 1024), 2)
        for name in files
    }
    return jsonify({"ok": True, "files": files, "file_sizes_mb": file_sizes_mb})


@app.route("/api/weekly-options/files/download")
def api_weekly_options_file_download():
    name = os.path.basename((request.args.get("file") or "").strip())
    if not name or name != request.args.get("file"):
        return jsonify({"ok": False, "msg": "Invalid file name"}), 400
    if not name.lower().endswith(".csv"):
        return jsonify({"ok": False, "msg": "Only CSV files can be downloaded"}), 400
    path = os.path.join(WORKDIR, "data", "nifty_options", "daily", name)
    if not os.path.isfile(path):
        return jsonify({"ok": False, "msg": "File not found"}), 404
    return send_file(path, as_attachment=True, download_name=name)


@app.route("/api/weekly-options/files/delete", methods=["POST"])
def api_weekly_options_file_delete():
    data = request.get_json() or {}
    files = data.get("files")
    if not isinstance(files, list) or not files:
        return jsonify({"ok": False, "msg": "Select at least one CSV file"}), 400

    deleted = []
    failed = []
    daily_dir = os.path.join(WORKDIR, "data", "nifty_options", "daily")
    for raw_name in files:
        name = os.path.basename(str(raw_name or "").strip())
        if name != str(raw_name or "").strip() or not name.lower().endswith(".csv"):
            continue
        path = os.path.join(daily_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            deleted.append(name)
        except OSError as exc:
            failed.append({"file": name, "msg": str(exc)})
    return jsonify({"ok": not failed, "deleted": deleted, "failed": failed})


# Written by the cron job: scripts/daily_download.py >> logs/daily_download.log
DAILY_DOWNLOAD_LOG = os.environ.get("DAILY_DOWNLOAD_LOG") or os.path.join(WORKDIR, "logs", "daily_download.log")
_DD_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) IST\] (.*)$")
_DD_RUNNING_GRACE_SECONDS = 20 * 60


def parse_daily_download_log(text: str) -> dict:
    """Summarise runs in logs/daily_download.log (output of scripts/daily_download.py).

    Each run starts with "[<ts> IST] daily download for <date> ...". Status of a run:
    OK, FAILED, LOGIN_FAILED, SKIPPED (weekend / market still open), DRY_RUN, or
    INCOMPLETE (no closing line: still running, or the process died).
    """
    runs = []
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        match = _DD_LINE_RE.match(line)
        message = match.group(2) if match else line
        if match and message.startswith("daily download for "):
            current = {
                "trade_date": message.split()[3], "started_at": match.group(1), "finished_at": match.group(1),
                "status": "INCOMPLETE", "detail": "", "expiries": "", "saved": [], "skipped": [], "errors": [], "notes": [],
            }
            runs.append(current)
            continue
        if current is None:
            continue
        if match:
            current["finished_at"] = match.group(1)
        if message.startswith("nearest expiries: "):
            current["expiries"] = message[len("nearest expiries: "):]
        elif message.startswith(("Saved ", "Added ")):
            current["saved"].append(message)
        elif message.startswith("SKIP existing file: "):
            current["skipped"].append(message[len("SKIP existing file: "):])
        elif message.startswith("ERROR:"):
            current["errors"].append(message)
            if "Kite login failed" in message or "ACCESS_TOKEN missing" in message:
                current["status"], current["detail"] = "LOGIN_FAILED", message
        elif "contract request errors saved to" in message or message.startswith("INCOMPLETE ("):
            current["errors"].append(message)
        elif message.startswith("No candles returned for "):
            current["notes"].append(message + " (market holiday?)")
        elif match and message.startswith(("weekend:", "market not closed", "market holiday")):
            current["status"], current["detail"] = "SKIPPED", message
        elif match and message.startswith("dry run"):
            current["status"], current["detail"] = "DRY_RUN", message
        elif match and message == "done":
            current["status"] = "FAILED" if current["errors"] else "OK"
            current["detail"] = message
        elif match and message.startswith("done with "):
            current["status"], current["detail"] = "FAILED", message
    last_ok = next((r for r in reversed(runs) if r["status"] == "OK"), None)
    return {"runs": len(runs), "last": runs[-1] if runs else None, "last_ok": last_ok}


@app.route("/api/daily-download/status")
def api_daily_download_status():
    log_path = DAILY_DOWNLOAD_LOG
    if not os.path.isfile(log_path):
        return jsonify({
            "ok": True, "found": False, "log_path": log_path,
            "msg": "No daily download log yet. Set up the cron job (scripts/daily_download.py) or wait for its first run.",
        })
    size = os.path.getsize(log_path)
    with open(log_path, "rb") as handle:
        handle.seek(max(0, size - 256 * 1024))  # last 256 KB is plenty for recent runs
        text = handle.read().decode("utf-8", errors="replace")
    summary = parse_daily_download_log(text)
    last = summary["last"]
    log_age_seconds = int(time.time() - os.path.getmtime(log_path))
    if last and last["status"] == "INCOMPLETE" and log_age_seconds < _DD_RUNNING_GRACE_SECONDS:
        last["status"], last["detail"] = "RUNNING", "download in progress (log updated recently)"

    files, error_files = [], []
    if last:
        daily_dir = os.path.join(WORKDIR, "data", "nifty_options", "daily")
        suffix = f"_{last['trade_date']}"
        if os.path.isdir(daily_dir):
            for name in sorted(os.listdir(daily_dir)):
                path = os.path.join(daily_dir, name)
                if name.startswith("nifty_weekly_") and name.endswith(f"{suffix}.csv"):
                    files.append({"file": name, "size_mb": round(os.path.getsize(path) / (1024 * 1024), 2)})
                elif name.startswith("errors_") and name.endswith(f"{suffix}.txt"):
                    error_files.append(name)

    tail_lines = [line for line in text.splitlines() if line.strip()][-40:]
    return jsonify({
        "ok": True, "found": True, "log_path": log_path, "log_updated_seconds_ago": log_age_seconds,
        "runs_in_log": summary["runs"], "last": last,
        "last_ok": {k: summary["last_ok"][k] for k in ("trade_date", "finished_at")} if summary["last_ok"] else None,
        "files": files, "error_files": error_files, "tail": tail_lines,
    })


# ---------------------------------------------------------------------------
# "Today" panel: one summary of everything checked each morning / evening.
# ---------------------------------------------------------------------------
PAPER_AUTOSTART_LOG = os.path.join(WORKDIR, "logs", "scheduled_paper_start.log")
PARITY_DIR = os.path.join(WORKDIR, "logs", "parity")
TODAY_STRATEGIES = ("v28", "v30")
_OPEN_PNL_RE = re.compile(
    r"^\[OPEN PNL\] (?P<at>\S+ \S+) \S+ \| entry_time=(?P<entry_time>.*?) \| trade_count=(?P<count>\d+) \| "
    r"symbol=(?P<symbol>\S+) \| side=(?P<side>\S+) \| ltp=(?P<ltp>[-\d.]+) \| entry=(?P<entry>[-\d.]+) \| "
    r"qty=(?P<qty>\d+) \| unrealized=(?P<upts>[-\d.]+) pts \((?P<urs>[-\d.]+) rs\) \| "
    r"realized=(?P<realized>[-\d.]+) rs \| total_mtm=(?P<total>[-\d.]+) rs"
)
_ENTERED_RE = re.compile(
    r"^Entered position at (?P<at>\S+ \S+) \|.*?side=(?P<side>\S+) symbol=(?P<symbol>\S+) "
    r"reason=(?P<reason>\S+) entry=(?P<entry>[-\d.]+)"
)
_EXITED_RE = re.compile(
    r"^Exited position at (?P<at>\S+ \S+) \|.*? with reason (?P<reason>\S+)\. symbol=(?P<symbol>\S+) "
    r"side=(?P<side>\S+) entry=(?P<entry>[-\d.]+) exit=(?P<exit>[-\d.]+) \| P&L: (?P<pnl_rs>[-\d.]+) Rs "
    r"\((?P<pnl_pts>[-\d.]+) points\)\. Cumulative MTM: (?P<mtm>[-\d.]+) Rs"
)
_STOPPED_RE = re.compile(r"Trading stopped by: (?P<reason>.+)$")


def _read_tail(path: str, max_bytes: int = 512 * 1024) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def parse_strategy_log_today(text: str, today: str) -> dict:
    """Today's trades, open position and P&L from a strategy log (lines dated `today` only)."""
    trades, last_entered, last_open, stopped = [], None, None, ""
    realized = 0.0
    order = 0
    last_entered_at = last_exit_at = -1
    for raw in text.splitlines():
        line = raw.strip()
        if today not in line:
            continue
        order += 1
        if (m := _EXITED_RE.match(line)):
            trades.append({"time": m["at"][11:16], "side": m["side"], "symbol": m["symbol"],
                           "entry": float(m["entry"]), "exit": float(m["exit"]), "reason": m["reason"],
                           "pnl_points": float(m["pnl_pts"]), "pnl_rs": float(m["pnl_rs"])})
            realized = float(m["mtm"])
            last_exit_at = order
        elif (m := _ENTERED_RE.match(line)):
            last_entered = {"time": m["at"][11:16], "side": m["side"], "symbol": m["symbol"],
                            "entry": float(m["entry"]), "reason": m["reason"]}
            last_entered_at = order
        elif (m := _OPEN_PNL_RE.match(line)):
            last_open = {"at": m["at"][11:19], "symbol": m["symbol"], "side": m["side"], "ltp": float(m["ltp"]),
                         "entry": float(m["entry"]), "unrealized_points": float(m["upts"]),
                         "unrealized_rs": float(m["urs"]), "realized_rs": float(m["realized"]),
                         "total_rs": float(m["total"]), "order": order}
        elif (m := _STOPPED_RE.search(line)):
            stopped = m["reason"].strip()
    position = None
    if last_entered is not None and last_entered_at > last_exit_at:
        position = dict(last_entered)
        if last_open and last_open["order"] > last_entered_at and last_open["symbol"] == last_entered["symbol"]:
            position.update({k: last_open[k] for k in ("ltp", "unrealized_points", "unrealized_rs", "at")})
    unrealized = position.get("unrealized_rs", 0.0) if position else 0.0
    return {"trades": trades, "trade_count": len(trades), "open_position": position,
            "realized_rs": round(realized, 2), "unrealized_rs": round(unrealized, 2),
            "total_rs": round(realized + unrealized, 2), "stopped_by": stopped}


_RUN_MODE_RE = re.compile(r"^Running mode:\s*(\S+)", re.M)
_START_MODE_RE = re.compile(r"--mode\s+(\S+)")


def log_run_mode(text: str) -> str:
    """Mode of the latest run in a runner log (BACKTEST / PAPERTRADE_LIVE / LIVE_ORDER), '' if unknown."""
    start = text.rfind("=== starting:")
    current = text[start:] if start >= 0 else text
    modes = _RUN_MODE_RE.findall(current)
    if modes:
        return modes[-1].strip().upper()
    first_line = current.splitlines()[0] if current else ""
    m = _START_MODE_RE.search(first_line)
    return m.group(1).strip().upper() if m else ""


def today_paper_trades(log_dir: str, variant: str, today: str) -> list:
    """Today's closed paper trades for one variant from the papertrade Excel files (all sessions, any settings)."""
    trades, seen = [], set()
    try:
        names = sorted(os.listdir(log_dir))
    except OSError:
        return []
    for name in names:
        info = classify_result_file(name)
        if not info or info["source"] != "paper" or info["variant"] != variant or info["date"] != today:
            continue
        for t in _read_result_file(os.path.join(log_dir, name), "paper")["trades"]:
            ident = (t["side"], t["symbol"], t["entry_time"])
            if ident in seen:
                continue
            seen.add(ident)
            trades.append(t)
    trades.sort(key=lambda t: t.get("exit_time") or t.get("entry_time") or "")
    return trades


def merge_today_paper_excel(summary: dict, excel_trades: list) -> dict:
    """Use the paper Excel trades for the Today card's closed trades (the log may hold a backtest run).

    The log still supplies the open position, unrealized P&L and stop reason when it is
    today's paper/live session. If the log already shows more closed trades (Excel is
    written a moment later), the log's list is kept.
    """
    if not excel_trades or len(summary.get("trades") or []) > len(excel_trades):
        return summary

    def hhmm(value):
        text = str(value or "")
        return text[11:16] if len(text) >= 16 else text[:5]

    trades = [{"time": hhmm(t.get("exit_time")), "side": t["side"], "symbol": t["symbol"],
               "entry": t.get("entry_price", 0.0), "exit": t.get("exit_price", 0.0),
               "reason": t["exit_reason"], "pnl_points": t.get("pnl_points", 0.0), "pnl_rs": t["pnl_rs"]}
              for t in excel_trades]
    realized = round(sum(t["pnl_rs"] for t in trades), 2)
    unrealized = summary.get("unrealized_rs", 0.0) if summary.get("open_position") else 0.0
    out = dict(summary)
    out.update({"trades": trades, "trade_count": len(trades), "realized_rs": realized,
                "unrealized_rs": round(unrealized, 2), "total_rs": round(realized + unrealized, 2),
                "trades_source": "paper_excel"})
    return out


def parse_autostart_log_today(text: str, today: str, started_markers: dict) -> dict:
    """Status of today's paper auto-start from logs/scheduled_paper_start.log."""
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith(f"[{today} ")]
    if started_markers:
        status, detail = "STARTED", ", ".join(f"{n} at {t}" for n, t in sorted(started_markers.items()))
    elif not lines:
        status, detail = "NOT_RUN", "no auto-start check has run today yet"
    else:
        last = lines[-1].split("] ", 1)[-1]
        if "waiting for today's Kite token" in last:
            status = "WAITING_TOKEN"
        elif "market holiday" in last:
            status = "HOLIDAY"
        elif "weekend" in last:
            status = "WEEKEND"
        elif "outside the start window" in last:
            status = "OUTSIDE_WINDOW"
        elif "ERROR" in last:
            status = "ERROR"
        else:
            status = "CHECKED"
        detail = last
    return {"status": status, "detail": detail, "last_check": lines[-1][1:20] if lines else "",
            "started": started_markers}


@app.route("/api/today")
def api_today():
    today = datetime.now(IST).date().isoformat()

    strategies = {}
    for name in TODAY_STRATEGIES:
        pid, running = 0, False
        try:
            with open(pid_path_for(name), "r", encoding="utf-8") as f:
                pid = int(f.read().strip() or 0)
            running = bool(pid) and is_pid_running(pid)
        except Exception:
            pass
        log_path = get_active_log_path(name)
        summary = {"trades": [], "trade_count": 0, "open_position": None, "realized_rs": 0.0,
                   "unrealized_rs": 0.0, "total_rs": 0.0, "stopped_by": ""}
        log_mode = ""
        try:
            if os.path.exists(log_path) and datetime.fromtimestamp(os.path.getmtime(log_path), IST).date().isoformat() == today:
                tail = _read_tail(log_path)
                log_mode = log_run_mode(tail)
                # The runner's log holds whatever was run last from this panel. Only a paper/live
                # session describes today's trading; a backtest run must not show up here.
                if log_mode in {"PAPERTRADE_LIVE", "LIVE_ORDER"}:
                    summary = parse_strategy_log_today(tail, today)
        except Exception:
            pass
        summary = merge_today_paper_excel(summary, today_paper_trades(LOGS_DIR, name, today))
        strategies[name] = {"running": running, "mode": read_mode(name) if running else "", "log_mode": log_mode, **summary}

    markers = {}
    for name in TODAY_STRATEGIES:
        marker = os.path.join(WORKDIR, "logs", f"autostart_{name}_{today}.done")
        if os.path.exists(marker):
            markers[name] = datetime.fromtimestamp(os.path.getmtime(marker), IST).strftime("%H:%M")
    autostart = parse_autostart_log_today(_read_tail(PAPER_AUTOSTART_LOG, 64 * 1024), today, markers)

    download = None
    if os.path.isfile(DAILY_DOWNLOAD_LOG):
        last = parse_daily_download_log(_read_tail(DAILY_DOWNLOAD_LOG, 256 * 1024))["last"]
        if last:
            download = {k: last[k] for k in ("trade_date", "status", "finished_at", "expiries")}

    parity = None
    try:
        reports = sorted(n for n in os.listdir(PARITY_DIR) if re.fullmatch(r"parity_\d{4}-\d{2}-\d{2}\.json", n))
        if reports:
            with open(os.path.join(PARITY_DIR, reports[-1]), "r", encoding="utf-8") as f:
                report = json.load(f)
            parity = {"trade_date": report.get("trade_date"), "checked_at": report.get("checked_at"),
                      "variants": {v: {"verdict": r.get("verdict"), "issues": (r.get("issues") or [])[:6]}
                                   for v, r in (report.get("variants") or {}).items()}}
    except Exception:
        parity = None

    return jsonify({"ok": True, "date": today, "token": kite_token_status(), "autostart": autostart,
                    "strategies": strategies, "download": download, "parity": parity})


# ---------------------------------------------------------------------------
# Performance page: equity curve, drawdown, daily P&L and breakdowns built from
# the backtest / paper-trade Excel files in the strategy log folder.
# ---------------------------------------------------------------------------
_PERF_PATTERNS = (
    ("backtest", re.compile(r"^backtest_(?P<d>\d{2})(?P<m>\d{2})(?P<y>\d{4})_(?P<settings>mtf.+?)_(?P<variant>v\d+)_(?P<ds>csv|kite)\.xlsx$")),
    ("backtest", re.compile(r"^backtest_(?P<d>\d{2})(?P<m>\d{2})(?P<y>\d{4})_(?P<variant>v\d+)_(?P<settings>.+)\.xlsx$")),
    ("paper", re.compile(r"^papertrade_(?P<d>\d{2})(?P<m>\d{2})(?P<y>\d{4})_\d{6}_(?P<settings>.+?)_(?P<variant>v\d+)\.xlsx$")),
    ("paper", re.compile(r"^papertrade_(?P<d>\d{2})(?P<m>\d{2})(?P<y>\d{4})_\d{6}_(?P<variant>v\d+)\.xlsx$")),
    # LIVE_ORDER sessions use the same Excel writer as paper trading (sheet live_trade_events).
    ("live", re.compile(r"^livetrade_(?P<d>\d{2})(?P<m>\d{2})(?P<y>\d{4})_\d{6}_(?P<settings>.+?)_(?P<variant>v\d+)\.xlsx$")),
    ("live", re.compile(r"^livetrade_(?P<d>\d{2})(?P<m>\d{2})(?P<y>\d{4})_\d{6}_(?P<variant>v\d+)\.xlsx$")),
)
_PERF_CACHE: dict = {}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def classify_result_file(name: str):
    """Identify a strategy result file: source, trade date, variant and settings group."""
    for source, pattern in _PERF_PATTERNS:
        m = pattern.match(name)
        if not m:
            continue
        try:
            day = datetime(int(m["y"]), int(m["m"]), int(m["d"])).date().isoformat()
        except ValueError:
            return None
        settings = m.groupdict().get("settings") or "(no settings in name)"
        if m.groupdict().get("ds"):
            settings = f"{settings} ({m['ds']})"
        return {"source": source, "date": day, "variant": m["variant"], "settings": settings}
    return None


def _read_result_file(path: str, source: str) -> dict:
    """Trades + day context from one Excel file (cached by modification time)."""
    mtime = os.path.getmtime(path)
    cached = _PERF_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    import pandas as pd

    sheet = "trades" if source == "backtest" else "live_trade_events"
    out = {"trades": [], "gap_type": "", "cpr_type": ""}
    try:
        frame = pd.read_excel(path, sheet_name=sheet)
        for _, r in frame.iterrows():
            if pd.isna(r.get("pnl_rs")):
                continue
            net = r.get("net_pnl_rs")
            out["trades"].append({
                "entry_time": str(r.get("entry_time", "")), "exit_time": str(r.get("exit_time", "")),
                "entry_price": float(r.get("entry_price", 0) or 0), "exit_price": float(r.get("exit_price", 0) or 0),
                "side": str(r.get("side", "")),
                "symbol": str(r.get("option_symbol", "")), "exit_reason": str(r.get("exit_reason", "")),
                "pnl_rs": float(r.get("pnl_rs")), "pnl_points": float(r.get("pnl_points", 0) or 0),
                "net_pnl_rs": None if net is None or pd.isna(net) else float(net),
            })
        summary = pd.read_excel(path, sheet_name="summary")
        metrics = dict(zip(summary["metric"].astype(str), summary["value"]))
        out["gap_type"] = "" if pd.isna(metrics.get("gap_type")) else str(metrics.get("gap_type") or "")
        out["cpr_type"] = "" if pd.isna(metrics.get("cpr_type")) else str(metrics.get("cpr_type") or "")
    except Exception:
        pass
    _PERF_CACHE[path] = (mtime, out)
    return out


def compute_performance(days: list) -> dict:
    """KPIs, equity curve, drawdown and breakdowns from per-day trade lists (pure function).

    `days`: [{"date", "trades": [...], "gap_type", "cpr_type"}]. Uses net P&L (after
    charges) only when every trade has it; otherwise everything is gross, so the
    numbers are never a mix of the two.
    """
    days = sorted(days, key=lambda d: d["date"])
    all_trades = [t for d in days for t in d["trades"]]
    basis = "net" if all_trades and all(t.get("net_pnl_rs") is not None for t in all_trades) else "gross"
    key = "net_pnl_rs" if basis == "net" else "pnl_rs"

    daily, equity, peak, max_dd = [], 0.0, 0.0, 0.0
    for d in days:
        pnl = round(sum(t[key] for t in d["trades"]), 2)
        equity = round(equity + pnl, 2)
        peak = max(peak, equity)
        drawdown = round(equity - peak, 2)
        max_dd = min(max_dd, drawdown)
        weekday = _WEEKDAYS[datetime.fromisoformat(d["date"]).weekday()]
        daily.append({"date": d["date"], "weekday": weekday, "pnl": pnl, "equity": equity, "drawdown": drawdown,
                      "trades": len(d["trades"]), "gap_type": d.get("gap_type") or "", "cpr_type": d.get("cpr_type") or ""})

    pnls = [t[key] for t in all_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_loss = -sum(losses)
    kpis = {
        "basis": basis, "trading_days": len(daily), "trades": len(pnls),
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(100.0 * len(wins) / len(pnls), 1) if pnls else 0.0,
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss > 0 else None,
        "avg_per_trade": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "max_drawdown": round(max_dd, 2),
        "best_day": max((d["pnl"] for d in daily), default=0.0),
        "worst_day": min((d["pnl"] for d in daily), default=0.0),
        "losing_days": sum(1 for d in daily if d["pnl"] < 0),
        "charges": round(sum(t["pnl_rs"] - t["net_pnl_rs"] for t in all_trades), 2) if basis == "net" else None,
    }

    def group(rows, field, value_key):
        out = {}
        for row in rows:
            label = row.get(field) or "(unknown)"
            g = out.setdefault(label, {"label": label, "count": 0, "total": 0.0, "wins": 0})
            g["count"] += 1
            g["total"] += row[value_key]
            g["wins"] += 1 if row[value_key] > 0 else 0
        result = []
        for g in out.values():
            result.append({"label": g["label"], "count": g["count"], "total": round(g["total"], 2),
                           "avg": round(g["total"] / g["count"], 2), "win_rate": round(100.0 * g["wins"] / g["count"], 1)})
        return result

    order = {d: i for i, d in enumerate(_WEEKDAYS)}
    trade_rows = [{"exit_reason": t["exit_reason"], "pnl": t[key]} for t in all_trades]
    breakdowns = {
        "weekday": sorted(group(daily, "weekday", "pnl"), key=lambda g: order.get(g["label"], 9)),
        "exit_reason": sorted(group(trade_rows, "exit_reason", "pnl"), key=lambda g: -g["count"]),
        "gap_type": sorted(group(daily, "gap_type", "pnl"), key=lambda g: -g["count"]),
        "cpr_type": sorted(group(daily, "cpr_type", "pnl"), key=lambda g: -g["count"]),
    }
    return {"kpis": kpis, "daily": daily, "breakdowns": breakdowns}


def collect_performance_days(log_dir: str, source: str, variant: str, settings: str,
                             date_from: str = "", date_to: str = "") -> list:
    """Per-day trades for one source/variant/settings group (paper sessions of a day merged)."""
    by_day: dict = {}
    for name in sorted(os.listdir(log_dir)):
        info = classify_result_file(name)
        if not info or info["source"] != source or info["variant"] != variant or info["settings"] != settings:
            continue
        if (date_from and info["date"] < date_from) or (date_to and info["date"] > date_to):
            continue
        parsed = _read_result_file(os.path.join(log_dir, name), source)
        day = by_day.setdefault(info["date"], {"date": info["date"], "trades": [], "gap_type": "", "cpr_type": "", "_seen": set()})
        for t in parsed["trades"]:
            ident = (t["side"], t["symbol"], t["entry_time"])
            if ident in day["_seen"]:
                continue
            day["_seen"].add(ident)
            day["trades"].append(t)
        day["gap_type"] = day["gap_type"] or parsed["gap_type"]
        day["cpr_type"] = day["cpr_type"] or parsed["cpr_type"]
    for day in by_day.values():
        day.pop("_seen", None)
    return list(by_day.values())


@app.route("/performance")
def performance_page():
    return render_template("performance.html")


@app.route("/api/performance")
def api_performance():
    log_dir = LOGS_DIR
    if not os.path.isdir(log_dir):
        return jsonify({"ok": True, "groups": [], "selected": None, "result": None})
    groups: dict = {}
    for name in os.listdir(log_dir):
        info = classify_result_file(name)
        if not info:
            continue
        k = (info["source"], info["variant"], info["settings"])
        g = groups.setdefault(k, {"source": k[0], "variant": k[1], "settings": k[2], "dates": set()})
        g["dates"].add(info["date"])
    group_list = sorted(
        ({"source": g["source"], "variant": g["variant"], "settings": g["settings"], "days": len(g["dates"]),
          "first": min(g["dates"]), "last": max(g["dates"])} for g in groups.values()),
        key=lambda g: (g["source"], g["variant"], g["last"], g["days"]), reverse=True)

    source = request.args.get("source", "backtest")
    if source not in {"backtest", "paper", "live"}:
        source = "backtest"
    variant = request.args.get("variant", "v28")
    settings = request.args.get("settings", "")
    date_from = request.args.get("from", "") if DATE_RE.fullmatch(request.args.get("from", "") or "") else ""
    date_to = request.args.get("to", "") if DATE_RE.fullmatch(request.args.get("to", "") or "") else ""
    candidates = [g for g in group_list if g["source"] == source and g["variant"] == variant]
    if not any(g["settings"] == settings for g in candidates):
        # Default: downloaded-CSV backtests first (the usual runs), then the group with the
        # most days, then the most recent.
        candidates.sort(key=lambda g: (g["settings"].endswith("(csv)"), g["days"], g["last"]), reverse=True)
        settings = candidates[0]["settings"] if candidates else ""
    result = None
    if settings:
        result = compute_performance(collect_performance_days(log_dir, source, variant, settings, date_from, date_to))
    return jsonify({"ok": True, "groups": group_list,
                    "selected": {"source": source, "variant": variant, "settings": settings, "from": date_from, "to": date_to},
                    "result": result, "holidays": load_market_holidays()})


MARKET_HOLIDAYS_FILE = os.path.join(WORKDIR, "scripts", "market_holidays.txt")


def load_market_holidays(path: str = MARKET_HOLIDAYS_FILE) -> dict:
    """{"YYYY-MM-DD": name} from scripts/market_holidays.txt (same file the cron jobs use)."""
    holidays: dict = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for raw in fh:
                text, _, comment = raw.partition("#")
                text = text.strip()
                if DATE_RE.fullmatch(text):
                    holidays[text] = comment.strip() or "Market holiday"
    except OSError:
        pass
    return holidays


@app.route('/api/log-files')
def api_log_files():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401
    try:
        if not os.path.exists(LOGS_DIR):
            return jsonify({"ok": True, "files": []})
        files = []
        for name in os.listdir(LOGS_DIR):
            path = os.path.join(LOGS_DIR, name)
            if os.path.isfile(path):
                st = os.stat(path)
                files.append({
                    "name": name,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
        files.sort(key=lambda x: x["mtime"], reverse=True)
        return jsonify({"ok": True, "files": files})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route('/api/log-files/download')
def api_log_files_download():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401
    name = (request.args.get('file') or '').strip()
    if not name:
        return jsonify({"ok": False, "msg": "Missing file name"}), 400
    safe_name = os.path.basename(name)
    if safe_name != name:
        return jsonify({"ok": False, "msg": "Invalid file name"}), 400
    path = os.path.join(LOGS_DIR, safe_name)
    if not os.path.exists(path) or not os.path.isfile(path):
        return jsonify({"ok": False, "msg": "File not found"}), 404
    return send_file(path, as_attachment=True, download_name=safe_name)


@app.route('/api/log-files/delete', methods=['POST'])
def api_log_files_delete():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401

    data = request.get_json() or {}
    files = data.get('files')
    if not isinstance(files, list) or not files:
        return jsonify({"ok": False, "msg": "Provide files as a non-empty list"}), 400

    selected = []
    for raw in files:
        name = str(raw or '').strip()
        if not name:
            continue
        safe_name = os.path.basename(name)
        if safe_name != name:
            continue
        if safe_name not in selected:
            selected.append(safe_name)

    if not selected:
        return jsonify({"ok": False, "msg": "No valid files selected"}), 400

    deleted = []
    not_found = []
    failed = []

    for safe_name in selected:
        path = os.path.join(LOGS_DIR, safe_name)
        if not os.path.exists(path) or not os.path.isfile(path):
            not_found.append(safe_name)
            continue
        try:
            os.remove(path)
            deleted.append(safe_name)
        except Exception as e:
            failed.append({"file": safe_name, "msg": str(e)})

    return jsonify({
        "ok": len(failed) == 0,
        "deleted": deleted,
        "not_found": not_found,
        "failed": failed,
        "msg": f"Deleted {len(deleted)} file(s)",
    })


def normalize_json_value(value):
    try:
        if pd is not None and pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, 'isoformat'):
        try:
            return value.isoformat(sep=' ')
        except Exception:
            return str(value)
    return value


def parse_trade_details_from_xlsx(safe_name: str, row_limit: int = 200) -> tuple[dict, str | None]:
    if not safe_name or os.path.basename(safe_name) != safe_name:
        return {}, "Invalid file name"
    if not safe_name.lower().endswith('.xlsx'):
        return {}, "Only .xlsx files are supported"

    path = os.path.join(LOGS_DIR, safe_name)
    if not os.path.exists(path) or not os.path.isfile(path):
        return {}, "File not found"

    xls = pd.ExcelFile(path)
    trade_df = None
    trade_sheet = ""
    for sheet in xls.sheet_names:
        df = pd.read_excel(path, sheet_name=sheet)
        cols_lower = [str(c).strip().lower() for c in df.columns]
        if ('pnl_rs' in cols_lower) or ('pnl_points' in cols_lower):
            trade_df = df
            trade_sheet = sheet
            break

    if trade_df is None:
        return {}, "No trade sheet with pnl columns found"

    cols_map = {str(c).strip().lower(): c for c in trade_df.columns}
    pnl_rs_col = cols_map.get('pnl_rs')
    pnl_points_col = cols_map.get('pnl_points')

    total_pnl_rs = 0.0
    total_pnl_points = 0.0
    win_trades = 0
    loss_or_flat = 0

    if pnl_rs_col is not None:
        pnl_rs = pd.to_numeric(trade_df[pnl_rs_col], errors='coerce').fillna(0.0)
        total_pnl_rs = float(pnl_rs.sum())
        win_trades = int((pnl_rs > 0).sum())
        loss_or_flat = int((pnl_rs <= 0).sum())

    if pnl_points_col is not None:
        pnl_points = pd.to_numeric(trade_df[pnl_points_col], errors='coerce').fillna(0.0)
        total_pnl_points = float(pnl_points.sum())

    preferred_cols = [
        'entry_time', 'entry_reason', 'exit_time', 'side', 'option_symbol',
        'entry_spot', 'signal_spot_price', 'exit_spot',
        'entry_price', 'high_during_trade', 'exit_price', 'pnl_points', 'pnl_rs', 'exit_reason'
    ]
    available_cols = [c for c in preferred_cols if c in cols_map]
    selected_cols = [cols_map[c] for c in available_cols]

    rows = []
    if selected_cols:
        subset = trade_df[selected_cols].tail(max(1, int(row_limit)))
        for _, row in subset.iterrows():
            out = {"source_file": safe_name}
            for lc, original_col in zip(available_cols, selected_cols):
                out[lc] = normalize_json_value(row.get(original_col))
            rows.append(out)

    payload = {
        "file": safe_name,
        "sheet": trade_sheet,
        "total_rows": int(len(trade_df)),
        "summary": {
            "total_pnl_rs": total_pnl_rs,
            "total_pnl_points": total_pnl_points,
            "win_trades": win_trades,
            "loss_or_flat_trades": loss_or_flat,
        },
        "columns": available_cols,
        "rows": rows,
        "market_context": _read_summary_market_context(path),
    }
    return payload, None


@app.route('/api/log-files/trade-details')
def api_log_files_trade_details():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401
    if pd is None:
        return jsonify({"ok": False, "msg": "pandas not installed on server"}), 500

    name = (request.args.get('file') or '').strip()
    if not name:
        return jsonify({"ok": False, "msg": "Missing file name"}), 400
    safe_name = os.path.basename(name)
    try:
        payload, err = parse_trade_details_from_xlsx(safe_name, row_limit=200)
        if err:
            return jsonify({"ok": False, "msg": err}), 404
        payload["ok"] = True
        return jsonify(payload)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route('/api/log-files/trade-details-consolidated', methods=['POST'])
def api_log_files_trade_details_consolidated():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401
    if pd is None:
        return jsonify({"ok": False, "msg": "pandas not installed on server"}), 500

    data = request.get_json() or {}
    files = data.get('files')
    if not isinstance(files, list) or not files:
        return jsonify({"ok": False, "msg": "Provide files as a non-empty list"}), 400

    selected = []
    for raw in files:
        name = str(raw or '').strip()
        if not name:
            continue
        safe_name = os.path.basename(name)
        if safe_name == name and safe_name not in selected:
            selected.append(safe_name)

    if not selected:
        return jsonify({"ok": False, "msg": "No valid files selected"}), 400

    total_rows = 0
    total_pnl_rs = 0.0
    total_pnl_points = 0.0
    win_trades = 0
    loss_or_flat = 0
    all_rows = []
    file_reports = []
    market_context_list = []

    for safe_name in selected:
        try:
            payload, err = parse_trade_details_from_xlsx(safe_name, row_limit=200)
            if err:
                file_reports.append({
                    "file": safe_name,
                    "ok": False,
                    "msg": err,
                    "market_context": {k: "" for k in SUMMARY_MARKET_CONTEXT_FIELDS},
                })
                continue

            summary = payload.get('summary') or {}
            market_context = payload.get('market_context') or {k: "" for k in SUMMARY_MARKET_CONTEXT_FIELDS}
            total_rows += int(payload.get('total_rows') or 0)
            total_pnl_rs += to_float(summary.get('total_pnl_rs'), 0.0)
            total_pnl_points += to_float(summary.get('total_pnl_points'), 0.0)
            win_trades += int(summary.get('win_trades') or 0)
            loss_or_flat += int(summary.get('loss_or_flat_trades') or 0)
            all_rows.extend(payload.get('rows') or [])
            market_context_list.append(market_context)
            file_reports.append({
                "file": safe_name,
                "ok": True,
                "sheet": payload.get('sheet', ''),
                "total_rows": int(payload.get('total_rows') or 0),
                "total_pnl_rs": to_float(summary.get('total_pnl_rs'), 0.0),
                "total_pnl_points": to_float(summary.get('total_pnl_points'), 0.0),
                "market_context": market_context,
            })
        except Exception as e:
            file_reports.append({
                "file": safe_name,
                "ok": False,
                "msg": str(e),
                "market_context": {k: "" for k in SUMMARY_MARKET_CONTEXT_FIELDS},
            })

    if all_rows:
        try:
            all_rows.sort(key=lambda x: str(x.get('entry_time') or x.get('exit_time') or ''))
        except Exception:
            pass

    return jsonify({
        "ok": True,
        "files_selected": selected,
        "file_reports": file_reports,
        "total_rows": total_rows,
        "summary": {
            "total_pnl_rs": total_pnl_rs,
            "total_pnl_points": total_pnl_points,
            "win_trades": win_trades,
            "loss_or_flat_trades": loss_or_flat,
        },
        "market_context": _merge_market_context(market_context_list),
        "rows": all_rows[-600:],
    })


@app.route('/api/log-files/gap-details')
def api_log_files_gap_details():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401

    from_date = (request.args.get("from") or "").strip()
    to_date = (request.args.get("to") or "").strip()
    try:
        start_date = datetime.fromisoformat(from_date).date()
        end_date = datetime.fromisoformat(to_date).date()
    except ValueError:
        return jsonify({"ok": False, "msg": "Use valid From and To dates"}), 400
    if start_date > end_date:
        return jsonify({"ok": False, "msg": "From date cannot be after To date"}), 400

    kite, kite_err = _build_kite_client_from_env()
    if kite is None:
        return jsonify({"ok": False, "msg": f"Cannot fetch broker gap data: {kite_err}"}), 400

    token = int(to_float(os.environ.get("BROKER_GAP_UNDERLYING_TOKEN", "256265"), 256265))
    reports = []
    current_date = start_date
    while current_date <= end_date:
        if current_date.weekday() < 5:
            trade_date = current_date.isoformat()
            gap, gap_err = _fetch_gap_from_broker_for_date(kite, trade_date, token)
            if gap:
                reports.append({
                    "file": trade_date,
                    "ok": True,
                    "trade_date": trade_date,
                    "market_context": {
                        **gap,
                        "mtf_interval": mtf_interval_from_gap_pct(gap.get("gap_pct")),
                    },
                })
            else:
                reports.append({
                    "file": trade_date,
                    "ok": False,
                    "trade_date": trade_date,
                    "msg": gap_err or "gap_fetch_failed",
                    "market_context": {},
                })
        current_date += timedelta(days=1)

    return jsonify({
        "ok": True,
        "from_date": from_date,
        "to_date": to_date,
        "reports": reports,
    })


@app.route('/api/log-files/update-gap-summary', methods=['POST'])
def api_log_files_update_gap_summary():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401

    data = request.get_json() or {}
    files = data.get("files")
    if not isinstance(files, list) or not files:
        return jsonify({"ok": False, "msg": "Provide files as a non-empty list"}), 400

    kite, kite_err = _build_kite_client_from_env()
    if kite is None:
        return jsonify({"ok": False, "msg": f"Cannot fetch broker gap data: {kite_err}"}), 400

    token = int(to_float(os.environ.get("BROKER_GAP_UNDERLYING_TOKEN", "256265"), 256265))

    selected = []
    for raw in files:
        safe_name = os.path.basename(str(raw or "").strip())
        if safe_name and safe_name.lower().endswith(".xlsx") and safe_name == str(raw or "").strip():
            if safe_name not in selected:
                selected.append(safe_name)

    if not selected:
        return jsonify({"ok": False, "msg": "No valid .xlsx files selected"}), 400

    reports = []
    updated = 0

    for safe_name in selected:
        path = os.path.join(LOGS_DIR, safe_name)
        if not os.path.isfile(path):
            reports.append({"file": safe_name, "ok": False, "msg": "file_not_found"})
            continue

        trade_date = _extract_trade_date_from_filename(safe_name)
        if not trade_date:
            trade_date = _read_trade_date_from_summary_sheet(path)
        if not trade_date:
            reports.append({"file": safe_name, "ok": False, "msg": "trade_date_not_found_in_filename_or_summary"})
            continue

        gap, gap_err = _fetch_gap_from_broker_for_date(kite, trade_date, token)
        if not gap:
            reports.append({"file": safe_name, "ok": False, "msg": gap_err or "gap_fetch_failed", "trade_date": trade_date})
            continue

        ok, write_err = _write_gap_to_summary_sheet(path, gap)
        if not ok:
            reports.append({"file": safe_name, "ok": False, "msg": write_err or "write_failed", "trade_date": trade_date})
            continue

        updated += 1
        reports.append({
            "file": safe_name,
            "ok": True,
            "trade_date": trade_date,
            "gap_type": gap.get("gap_type"),
            "gap_points": gap.get("gap_points"),
        })

    failed_messages = [
        f"{report.get('file')}: {report.get('msg')}"
        for report in reports
        if not report.get("ok") and report.get("msg")
    ]
    return jsonify({
        "ok": updated > 0,
        "updated": updated,
        "total": len(selected),
        "msg": "All selected files failed: " + "; ".join(failed_messages) if updated == 0 and failed_messages else "",
        "reports": reports,
    })


@app.route("/api/stop", methods=["POST"])
def api_stop():
    data = request.get_json() or {}
    name = data.get('name')
    if name and not safe_process_name(name):
        return jsonify({"ok": False, "msg": "invalid name"}), 400
    log_path = LOG_FILE
    if name:
        log_path = get_active_log_path(name)

    before_mtime = 0.0
    try:
        if os.path.exists(log_path):
            before_mtime = os.path.getmtime(log_path)
    except Exception:
        before_mtime = 0.0

    if name:
        ok, msg = stop_named_strategy(name)
    else:
        ok, msg = stop_strategy()

    response = {"ok": ok, "msg": msg}
    if ok:
        summary = _wait_for_shutdown_summary(log_path, before_mtime=before_mtime, timeout_seconds=4.0)
        if not summary:
            summary = _append_forced_shutdown_summary(log_path, trigger_reason=f"{str(msg).upper()}_FALLBACK")
        if summary:
            response["shutdown_summary"] = summary
    return jsonify(response)


@app.route("/api/stop-all", methods=["POST"])
def api_stop_all():
    """Gracefully stop all strategy processes managed by this application."""
    reports = []
    for name in ("live_order_only", "v28", "v30", "weekly_options_download"):
        ok, msg = stop_named_strategy(name)
        reports.append({"name": name, "ok": ok, "msg": msg})

    legacy_ok, legacy_msg = stop_strategy()
    if legacy_ok or legacy_msg != "not_running":
        reports.append({"name": "legacy", "ok": legacy_ok, "msg": legacy_msg})

    stopped = [item["name"] for item in reports if item["ok"]]
    failures = [
        item for item in reports
        if not item["ok"] and item["msg"] not in {"not_running"}
    ]
    if failures:
        return jsonify({
            "ok": False,
            "msg": "Some managed strategy processes could not be stopped.",
            "stopped": stopped,
            "reports": reports,
        }), 500
    return jsonify({
        "ok": True,
        "msg": "All managed strategy processes stopped." if stopped else "No managed strategy process was running.",
        "stopped": stopped,
        "reports": reports,
    })


@app.route("/logs")
def view_logs():
    name = request.args.get('name')
    if name and not safe_process_name(name):
        return jsonify({"ok": False, "msg": "invalid name"}), 400
    path = LOG_FILE
    if name:
        path = get_active_log_path(name)
    if not os.path.exists(path):
        return "No logs yet", 404
    # return the whole log file for simplicity
    return send_file(path, mimetype="text/plain")


@app.route('/stream-logs')
def stream_logs():
    name = request.args.get('name')
    if name and not safe_process_name(name):
        return jsonify({"ok": False, "msg": "invalid name"}), 400
    path = LOG_FILE
    if name:
        path = get_active_log_path(name)
    if not os.path.exists(path):
        return Response("", mimetype="text/event-stream")

    def generate():
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                # start at EOF
                f.seek(0, os.SEEK_END)
                while True:
                    line = f.readline()
                    if line:
                        # send each line as an SSE message
                        for part in line.rstrip('\n').splitlines():
                            yield f"data: {part}\n\n"
                    else:
                        time.sleep(0.2)
        except GeneratorExit:
            return
        except Exception:
            return

    return Response(stream_with_context(generate()), mimetype='text/event-stream')


@app.route('/api/clear-logs', methods=['POST'])
def api_clear_logs():
    data = request.get_json() or {}
    name = data.get('name')
    if name and not safe_process_name(name):
        return jsonify({"ok": False, "msg": "invalid name"}), 400
    path = LOG_FILE
    if name:
        path = get_active_log_path(name)
    try:
        # Truncate and write a cleared marker so SSE clients see a change
        with open(path, 'w', encoding='utf-8') as f:
            f.write(f"=== log cleared at {ist_now_str()} ===\n")
            f.flush()
        return jsonify({"ok": True, "msg": "cleared"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route('/api/kite/login_url')
def api_kite_login_url():
    load_env_file()
    api_key = os.environ.get("API_KEY", "").strip()
    if KiteConnect is None:
        return jsonify({"ok": False, "msg": "kiteconnect library not installed. Install with: pip install kiteconnect"}), 400
    if not api_key:
        return jsonify({"ok": False, "msg": "API_KEY not set. Add API_KEY to .env or environment"}), 400
    kite = KiteConnect(api_key=api_key)
    # redirect to /zerodha/callback on this server
    redirect = request.url_root.rstrip('/') + '/zerodha/callback'
    try:
        # Some kiteconnect versions accept redirect_uri as a named arg,
        # older versions expect it as a positional argument.
        try:
            url = kite.login_url(redirect_uri=redirect)
        except TypeError:
            url = kite.login_url(redirect)
        return jsonify({"ok": True, "login_url": url, "redirect": redirect})
    except Exception as e:
        # Fallback: construct the connect URL manually (works regardless of kiteconnect version)
        try:
            from urllib.parse import quote_plus
            manual = f"https://kite.trade/connect/login?v=3&api_key={api_key}&redirect_uri={quote_plus(redirect)}"
            return jsonify({"ok": True, "login_url": manual, "redirect": redirect})
        except Exception:
            return jsonify({"ok": False, "msg": str(e)}), 500


@app.route('/kite/callback')
@app.route('/zerodha/callback')
def kite_callback():
    # Kite will redirect here with ?request_token=xxx
    request_token = request.args.get('request_token')
    if not request_token:
        return "Missing request_token in callback", 400
    load_env_file()
    api_key = os.environ.get("API_KEY", "").strip()
    api_secret = os.environ.get("API_SECRET", "").strip()
    if not api_key or not api_secret:
        return "API_KEY or API_SECRET not configured on server", 500
    if KiteConnect is None:
        return "KiteConnect library not available on server", 500
    try:
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data.get('access_token')
        if access_token:
            save_kite_access_token(access_token)
            return (
                f"Kite token saved on this dashboard ({request.host}): {access_token[:6]}...{access_token[-6:]}. "
                f"{kite_token_status()['message']} You can close this tab."
            )
        return "Failed to obtain access token: " + str(data)
    except Exception as e:
        return str(e), 500


@app.route('/api/kite/exchange', methods=['POST'])
def api_kite_exchange():
    data = request.get_json() or {}
    request_token = data.get('request_token')
    load_env_file()
    api_key = os.environ.get("API_KEY", "").strip()
    api_secret = os.environ.get("API_SECRET", "").strip()
    if not request_token or not api_key or not api_secret or KiteConnect is None:
        return jsonify({"ok": False, "msg": "missing request_token/API_KEY/API_SECRET or KiteConnect"}), 400
    try:
        kite = KiteConnect(api_key=api_key)
        res = kite.generate_session(request_token, api_secret=api_secret)
        access_token = res.get('access_token')
        if access_token:
            save_kite_access_token(access_token)
            return jsonify({"ok": True, "access_token_masked": access_token[:6] + '...' + access_token[-6:],
                            "kite_token": kite_token_status()})
        return jsonify({"ok": False, "msg": str(res)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route('/api/kite/test_login', methods=['POST'])
def api_kite_test_login():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401
    if not os.path.exists(KITE_TEST_SCRIPT):
        return jsonify({"ok": False, "msg": "test_kite_login.py not found"}), 404

    python = sys.executable or "python3"
    cmd = [python, KITE_TEST_SCRIPT]
    ts = ist_now_str()
    try:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.run(cmd, cwd=WORKDIR, capture_output=True, text=True, timeout=120, env=env)
        combined = (proc.stdout or "")
        if proc.stderr:
            combined += ("\n" if combined else "") + proc.stderr
        output = combined.strip() or "(no output)"
        ok = proc.returncode == 0
        msg = "Kite login test passed" if ok else "Kite login test failed"
        user_details = parse_kite_user_details(output) if ok else {}
        try:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(f"\n=== Kite login test at {ts} ===\n")
                f.write(f"Command: {' '.join(cmd)}\n")
                f.write(f"Exit code: {proc.returncode}\n")
                f.write(output + "\n")
        except Exception:
            pass
        return jsonify({
            "ok": ok,
            "msg": msg,
            "exit_code": proc.returncode,
            "output": output,
            "kite_user_details": user_details,
        })
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") + "\n" + (e.stderr or "")).strip() or "Timed out while running test"
        try:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(f"\n=== Kite login test timeout at {ts} ===\n")
                f.write(out + "\n")
        except Exception:
            pass
        return jsonify({"ok": False, "msg": "Kite login test timed out", "output": out}), 500
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route('/api/kite/account_summary')
def api_kite_account_summary():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401

    try:
        from scripts.login import get_kite
        kite = get_kite()

        profile = kite.profile() or {}
        positions = kite.positions() or {}
        margins = kite.margins() or {}

        day_positions = positions.get('day') or []
        net_positions = positions.get('net') or []

        pnl_day = sum(to_float(p.get('pnl', 0.0)) for p in day_positions)
        pnl_net = sum(to_float(p.get('pnl', 0.0)) for p in net_positions)

        open_day = sum(1 for p in day_positions if to_float(p.get('quantity', 0)) != 0)
        open_net = sum(1 for p in net_positions if to_float(p.get('quantity', 0)) != 0)

        equity = margins.get('equity') or {}
        available = equity.get('available') or {}
        utilized = equity.get('utilised') or equity.get('utilized') or {}

        available_cash = to_float(available.get('cash', 0.0))
        opening_balance = to_float(available.get('opening_balance', 0.0))
        live_balance = to_float(available.get('live_balance', 0.0))
        margin_used = to_float(utilized.get('debits', 0.0))

        now_ts = ist_now_str()

        return jsonify({
            "ok": True,
            "timestamp": now_ts,
            "user": {
                "user_id": profile.get('user_id'),
                "user_name": profile.get('user_name'),
            },
            "pnl": {
                "day": pnl_day,
                "net": pnl_net,
            },
            "positions": {
                "day_total": len(day_positions),
                "day_open": open_day,
                "net_total": len(net_positions),
                "net_open": open_net,
            },
            "margins": {
                "available_cash": available_cash,
                "opening_balance": opening_balance,
                "live_balance": live_balance,
                "margin_used": margin_used,
            },
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
