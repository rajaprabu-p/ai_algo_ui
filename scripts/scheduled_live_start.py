"""Start ONE strategy in LIVE_ORDER automatically each trading morning (cron). REAL ORDERS.

Off unless you switch it on in the dashboard (Today panel, "Live auto-start" card),
which also picks the strategy. The switch is the file logs/live_autostart.json:
    {"enabled": true, "strategy": "v30"}
No strategy can be named on the command line, so a cron line can never start
something the dashboard switch does not show.

How it behaves (cron runs it every 5 minutes, 09:00-12:25 IST):
  * switch off or no strategy chosen -> nothing to do
  * weekends and dates in scripts/market_holidays.txt -> nothing to do
  * outside 09:00 IST .. NO_NEW_ENTRY_AFTER (dashboard setting) -> nothing to do
  * Kite token not refreshed today yet -> "waiting for today's Kite token";
    the next run checks again, so the session starts soon after you log in
  * strategy files do not match MANIFEST.json -> ERROR, nothing is started
  * otherwise starts the strategy exactly like the dashboard's "Start LIVE_ORDER"
    button (name live_<strategy>, same pid / log files, working Stop button)
  * at most one live start per day (marker file in logs/), so a session you stop
    from the dashboard is NOT restarted later that day

Usage:
    venv/bin/python scripts/scheduled_live_start.py            # what cron runs
    venv/bin/python scripts/scheduled_live_start.py --dry-run  # checks only
Exit codes: 0 ok / nothing to do, 1 a start failed or files do not match, 2 waiting for the Kite token.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scheduled_paper_start import (  # noqa: E402
    EARLIEST_START, IST, STRATEGY_SCRIPTS, kite_login_ok, latest_start_time, log,
)

MODE = "LIVE_ORDER"
MARKER_DIR = PROJECT_ROOT / "logs"
SETTINGS_PATH = PROJECT_ROOT / "logs" / "live_autostart.json"


def marker_path(day: date) -> Path:
    return MARKER_DIR / f"live_autostart_{day.isoformat()}.done"


def load_settings(path: Path | None = None) -> dict:
    """{"enabled": bool, "strategy": "v28" | "v30" | ""}; anything unreadable or unknown means off."""
    try:
        data = json.loads(Path(path or SETTINGS_PATH).read_text(encoding="utf-8"))
        strategy = str(data.get("strategy") or "")
        if strategy not in STRATEGY_SCRIPTS:
            return {"enabled": False, "strategy": ""}
        return {"enabled": data.get("enabled") is True, "strategy": strategy}
    except Exception:
        return {"enabled": False, "strategy": ""}


def strategy_files_ok() -> tuple[bool, str]:
    """The deploy check LIVE_ORDER itself runs: every strategy file matches MANIFEST.json."""
    try:
        from strategy import deploy_check

        result = deploy_check.check()
        if result.ok:
            return True, f"version {result.version}"
        return False, "; ".join(result.problems()[:3]) or "strategy files do not match MANIFEST.json"
    except Exception as exc:
        return False, f"deploy check failed: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Run all checks but do not start anything")
    args = parser.parse_args()

    now = datetime.now(IST)
    today = now.date()
    settings = load_settings()
    if not settings["enabled"]:
        log("live auto-start is off: nothing to do")
        return 0
    name = settings["strategy"]
    log(f"scheduled LIVE_ORDER start check for {name}")

    if today.weekday() >= 5:
        log("weekend: nothing to do")
        return 0
    try:
        from daily_download import load_holidays

        holidays = load_holidays()
    except Exception as exc:
        # Paper trading assumes a trading day here; real orders do not.
        log(f"ERROR: could not read market_holidays.txt ({exc}); not starting LIVE_ORDER")
        return 1
    if today in holidays:
        log(f"market holiday ({holidays[today] or 'listed'}): nothing to do")
        return 0
    latest = latest_start_time()
    if not (EARLIEST_START <= now.time() <= latest):
        log(f"outside the start window {EARLIEST_START:%H:%M}-{latest:%H:%M} IST: nothing to do")
        return 0

    if marker_path(today).exists():
        log("already auto-started today (marker exists); not starting again")
        return 0

    ok, message = kite_login_ok()
    if not ok:
        log(f"waiting for today's Kite token: {message}")
        return 2
    log(f"Kite token OK: {message}")

    ok, message = strategy_files_ok()
    if not ok:
        log(f"ERROR: strategy files do not match the manifest, LIVE_ORDER not started: {message}")
        return 1

    if args.dry_run:
        log(f"dry run: would start {MODE} for {name}")
        return 0

    import app as webapp

    MARKER_DIR.mkdir(parents=True, exist_ok=True)
    script = str(PROJECT_ROOT / STRATEGY_SCRIPTS[name])
    started, msg = webapp.start_named_strategy(f"live_{name}", script, ["--mode", MODE], mode=MODE)
    if not started:
        log(f"ERROR: {name}: LIVE_ORDER start failed: {msg}")
        return 1
    marker_path(today).write_text(f"{now.isoformat()} {name} {msg}\n", encoding="utf-8")
    verb = "already running (left as is)" if msg.startswith("resumed:") else "started"
    log(f"{name}: {MODE} {verb} [{msg}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
