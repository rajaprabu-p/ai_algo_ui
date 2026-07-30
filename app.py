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
from datetime import datetime, timedelta, timezone
try:
    import pandas as pd
except Exception:
    pd = None

app = Flask(__name__)

WORKDIR = os.path.dirname(os.path.abspath(__file__))
STRATEGY_SCRIPT = os.path.join(WORKDIR, "v28_50pt_strategy_main.py")
KITE_TEST_SCRIPT = os.path.join(WORKDIR, "test_kite_login.py")
CONSOLIDATE_SCRIPT = os.path.join(WORKDIR, "consolidate_backtest_logs.py")
LOGS_DIR = os.path.join(WORKDIR, "logs")
LOG_FILE = os.path.join(WORKDIR, "strategy.log")
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
]


def ist_now_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S") + " IST"


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

# App auth defaults requested by user.
LOGIN_USERNAME = os.environ.get("LOGIN_USERNAME", "rajaprabu")
LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", "R@j@Pr@bu")
APP_SECRET_KEY = os.environ.get("APP_SECRET_KEY", "change-me-login-secret")

# Persist defaults into .env if they are missing.
if "LOGIN_USERNAME" not in os.environ:
    write_env_var("LOGIN_USERNAME", LOGIN_USERNAME)
if "LOGIN_PASSWORD" not in os.environ:
    write_env_var("LOGIN_PASSWORD", LOGIN_PASSWORD)
if "APP_SECRET_KEY" not in os.environ:
    write_env_var("APP_SECRET_KEY", APP_SECRET_KEY)

app.secret_key = APP_SECRET_KEY


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


def current_log_pointer_for(name: str) -> str:
    """File that stores the path to the active run log for a named strategy."""
    return os.path.join(WORKDIR, f"strategy_{name}.logpath")


def get_active_log_path(name: str) -> str:
    """Return the current active log path from the pointer file, fallback to legacy path."""
    pointer = current_log_pointer_for(name)
    try:
        if os.path.exists(pointer):
            with open(pointer, "r", encoding="utf-8") as f:
                p = f.read().strip()
            if p:
                return p
    except Exception:
        pass
    return log_path_for(name)


def set_active_log_path(name: str, path: str) -> None:
    """Persist the active log path so all routes can find the current run's log."""
    pointer = current_log_pointer_for(name)
    try:
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
        return f"{prefix}_{date_str}_{name}_main.txt"
    time_str = dt.strftime("%H%M%S")
    return f"{prefix}_{date_str}_{time_str}_{name}_main.txt"


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


def start_strategy(args: list = None) -> tuple[bool, str]:
    if is_running():
        return False, "already_running"
    args = args or []
    python = sys.executable or "python3"
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
        proc = subprocess.Popen(cmd, cwd=WORKDIR, stdout=logf, stderr=subprocess.STDOUT, env=env, close_fds=True)
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
    # check if already running
    try:
        if os.path.exists(pidfile):
            with open(pidfile, 'r', encoding='utf-8') as f:
                pid = int(f.read().strip() or 0)
            if is_pid_running(pid):
                return False, 'already_running'
            try:
                os.remove(pidfile)
            except Exception:
                pass
    except Exception:
        pass

    # Build a timestamped log file in the logs/ folder (same naming as backtest/papertrade xlsx)
    now_ist = datetime.now(IST)
    log_filename = build_run_log_filename(name, mode, now_ist)
    os.makedirs(LOGS_DIR, exist_ok=True)
    logfile = os.path.join(LOGS_DIR, log_filename)
    set_active_log_path(name, logfile)

    python = sys.executable or "python3"
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
        proc = subprocess.Popen(cmd, cwd=WORKDIR, stdout=logf, stderr=subprocess.STDOUT, env=env, close_fds=True)
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
        return True, f"started:{proc.pid}"
    except Exception as e:
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
    return render_template("index.html")

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if username == LOGIN_USERNAME and password == LOGIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid username or password")
    if session.get("logged_in"):
        return redirect(url_for("index"))
    return render_template("login.html", error="")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/api/status")
def api_status():
    name = request.args.get('name')
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
    return jsonify({"running": running, "pid": pid, "mode": mode, "log": log_info, "access_token_masked": at_mask, "api_key_present": bool(api_key)})


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json() or {}
    # Support multi-strategy: { name, script, mode, date, args }
    name = data.get('name')
    script = data.get('script')
    args = []
    if isinstance(data.get("args"), list):
        args.extend(data.get("args"))
    mode = data.get("mode")
    if mode:
        args.extend(["--mode", str(mode)])
    date = data.get("date")
    if date:
        args.extend(["--date", str(date)])
    if name:
        # if script not supplied, default to STRATEGY_SCRIPT
        if not script:
            script = STRATEGY_SCRIPT
        ok, msg = start_named_strategy(name, script, args, mode=str(mode or ""))
    else:
        ok, msg = start_strategy(args)
    return jsonify({"ok": ok, "msg": msg})


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
        if width_pct < 0.30:
            cpr_type = "NARROW"
        elif width_pct < 0.70:
            cpr_type = "MODERATE"
        else:
            cpr_type = "WIDE"
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
    path = LOG_FILE
    if name:
        path = get_active_log_path(name)
    if not os.path.exists(path):
        return jsonify({"exists": False, "tail": ""})
    tail = tail_file(path, lines=500)
    return jsonify({"exists": True, "tail": tail})


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


@app.route('/api/log-files/consolidate', methods=['POST'])
def api_log_files_consolidate():
    if not session.get("logged_in"):
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401

    if not os.path.exists(CONSOLIDATE_SCRIPT):
        return jsonify({"ok": False, "msg": "consolidate_backtest_logs.py not found"}), 404

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
        if safe_name in selected:
            continue
        if not safe_name.lower().endswith('.xlsx'):
            continue
        if safe_name.startswith('consolidated_'):
            continue
        full_path = os.path.join(LOGS_DIR, safe_name)
        if os.path.isfile(full_path):
            selected.append(safe_name)

    if not selected:
        return jsonify({
            "ok": False,
            "msg": "No valid .xlsx source files selected from logs"
        }), 400

    python = sys.executable or "python3"
    cmd = [python, CONSOLIDATE_SCRIPT, "--files", *selected]

    try:
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.run(
            cmd,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "msg": "Consolidation timed out after 300 seconds"}), 500
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500

    combined = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
    output_file = ""
    try:
        candidates = []
        for name in os.listdir(LOGS_DIR):
            if not name.lower().endswith('.xlsx'):
                continue
            if not name.startswith('consolidated_'):
                continue
            path = os.path.join(LOGS_DIR, name)
            if os.path.isfile(path):
                candidates.append((os.path.getmtime(path), name))
        if candidates:
            candidates.sort(reverse=True)
            output_file = candidates[0][1]
    except Exception:
        output_file = ""

    if proc.returncode != 0:
        return jsonify({
            "ok": False,
            "msg": f"Consolidation failed with exit code {proc.returncode}",
            "output": combined,
        }), 500

    return jsonify({
        "ok": True,
        "msg": "Consolidated log generated",
        "files_processed": selected,
        "output_file": output_file,
        "output": combined,
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
        'entry_time', 'exit_time', 'side', 'option_symbol',
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

    return jsonify({
        "ok": updated > 0,
        "updated": updated,
        "total": len(selected),
        "reports": reports,
    })


@app.route("/api/stop", methods=["POST"])
def api_stop():
    data = request.get_json() or {}
    name = data.get('name')
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


@app.route("/logs")
def view_logs():
    name = request.args.get('name')
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
    api_key = os.environ.get("API_KEY", "").strip()
    api_secret = os.environ.get("API_SECRET", "").strip()
    if not api_key or not api_secret:
        return "API_KEY or API_SECRET not configured on server", 500
    if KiteConnect is None:
        return "KiteConnect library not available on server", 500
    try:
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data.get('access_token') or data.get('access_token')
        if access_token:
            os.environ['ACCESS_TOKEN'] = access_token
            write_env_var('ACCESS_TOKEN', access_token)
            return f"Access token generated and saved (masked): {access_token[:6]}...{access_token[-6:]}"
        return "Failed to obtain access token: " + str(data)
    except Exception as e:
        return str(e), 500


@app.route('/api/kite/exchange', methods=['POST'])
def api_kite_exchange():
    data = request.get_json() or {}
    request_token = data.get('request_token')
    api_key = os.environ.get("API_KEY", "").strip()
    api_secret = os.environ.get("API_SECRET", "").strip()
    if not request_token or not api_key or not api_secret or KiteConnect is None:
        return jsonify({"ok": False, "msg": "missing request_token/API_KEY/API_SECRET or KiteConnect"}), 400
    try:
        kite = KiteConnect(api_key=api_key)
        res = kite.generate_session(request_token, api_secret=api_secret)
        access_token = res.get('access_token')
        if access_token:
            os.environ['ACCESS_TOKEN'] = access_token
            write_env_var('ACCESS_TOKEN', access_token)
            return jsonify({"ok": True, "access_token_masked": access_token[:6] + '...' + access_token[-6:]})
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
        from login import get_kite
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
