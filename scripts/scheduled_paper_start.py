"""Start PAPERTRADE_LIVE sessions automatically each trading morning (cron).

Paper trading only: the mode is fixed to PAPERTRADE_LIVE and can never be
LIVE_ORDER. Sessions stop on their own at the strategy's FORCE_EXIT time.

How it behaves (cron runs it every 5 minutes, 09:00-12:25 IST):
  * weekends and dates in scripts/market_holidays.txt -> nothing to do
  * outside 09:00 IST .. NO_NEW_ENTRY_AFTER (dashboard setting) -> nothing to do
  * Kite token not refreshed today yet -> "waiting for today's Kite token";
    the next run checks again, so the session starts soon after you log in
  * otherwise starts each requested strategy exactly like the dashboard's Start
    button (same pid / mode / log files, so it appears in the dashboard with a
    working Stop button)
  * each strategy is started at most once per day (marker file in logs/), so a
    session you stop from the dashboard is NOT restarted later that day

Usage:
    venv/bin/python scripts/scheduled_paper_start.py v28            # one strategy
    venv/bin/python scripts/scheduled_paper_start.py v28 v30        # both
    venv/bin/python scripts/scheduled_paper_start.py v28 --dry-run  # checks only
Exit codes: 0 ok / nothing to do, 1 a start failed, 2 waiting for the Kite token.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

IST = ZoneInfo("Asia/Kolkata")
MODE = "PAPERTRADE_LIVE"  # fixed on purpose: this scheduler never places real orders
EARLIEST_START = dt_time(9, 0)  # start before the 09:15 open so the opening candles are tracked
STRATEGY_SCRIPTS = {"v28": "scripts/v28_strategy_main.py", "v30": "scripts/v30_strategy_main.py"}
MARKER_DIR = PROJECT_ROOT / "logs"


def log(message: str) -> None:
    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST] {message}", flush=True)


def marker_path(name: str, day: date) -> Path:
    return MARKER_DIR / f"autostart_{name}_{day.isoformat()}.done"


def latest_start_time() -> dt_time:
    """NO_NEW_ENTRY_AFTER from the dashboard's editable settings (default 12:00)."""
    try:
        from editable_config import load_editable_config

        hh, mm = str(load_editable_config().get("NO_NEW_ENTRY_AFTER", "12:00")).split(":", 1)
        return dt_time(int(hh), int(mm))
    except Exception:
        return dt_time(12, 0)


def kite_login_ok() -> tuple[bool, str]:
    """Same token rules as the dashboard, then a real profile() call to be sure."""
    import app as webapp  # importing the dashboard module does not start the web server

    status = webapp.kite_token_status()
    if status["state"] in {"missing", "expired"}:
        return False, status["message"]
    try:
        from kiteconnect import KiteConnect

        kite = KiteConnect(api_key=os.environ.get("API_KEY", ""))
        kite.set_access_token(os.environ.get("ACCESS_TOKEN", ""))
        kite.profile()
    except Exception as exc:
        return False, f"Kite login check failed: {exc}"
    return True, status["message"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("strategies", nargs="+", choices=sorted(STRATEGY_SCRIPTS), help="Strategies to start (v28, v30)")
    parser.add_argument("--dry-run", action="store_true", help="Run all checks but do not start anything")
    args = parser.parse_args()

    now = datetime.now(IST)
    today = now.date()
    names = list(dict.fromkeys(args.strategies))
    log(f"scheduled paper start check for {', '.join(names)}")

    if today.weekday() >= 5:
        log("weekend: nothing to do")
        return 0
    try:
        from daily_download import load_holidays

        holidays = load_holidays()
    except Exception as exc:
        log(f"WARNING: could not read market_holidays.txt ({exc}); assuming a trading day")
        holidays = {}
    if today in holidays:
        log(f"market holiday ({holidays[today] or 'listed'}): nothing to do")
        return 0
    latest = latest_start_time()
    if not (EARLIEST_START <= now.time() <= latest):
        log(f"outside the start window {EARLIEST_START:%H:%M}-{latest:%H:%M} IST: nothing to do")
        return 0

    pending = [n for n in names if not marker_path(n, today).exists()]
    for name in names:
        if name not in pending:
            log(f"{name}: already auto-started today (marker exists); not starting again")
    if not pending:
        return 0

    ok, message = kite_login_ok()
    if not ok:
        log(f"waiting for today's Kite token: {message}")
        return 2
    log(f"Kite token OK: {message}")

    if args.dry_run:
        log(f"dry run: would start {MODE} for {', '.join(pending)}")
        return 0

    import app as webapp

    failures = 0
    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    for name in pending:
        script = str(PROJECT_ROOT / STRATEGY_SCRIPTS[name])
        started, msg = webapp.start_named_strategy(name, script, ["--mode", MODE], mode=MODE)
        if started:
            marker_path(name, today).write_text(f"{now.isoformat()} {msg}\n", encoding="utf-8")
            verb = "already running (left as is)" if msg.startswith("resumed:") else "started"
            log(f"{name}: {MODE} {verb} [{msg}]")
        else:
            failures += 1
            log(f"ERROR: {name}: start failed: {msg}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
