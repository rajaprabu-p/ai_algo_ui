"""Daily NIFTY option candle download for backtesting (run after market close).

Each trading day it downloads 1-minute candles for the NEAREST NIFTY option
expiries from today onward (default 2). That automatically covers:
  * normal days     -> this week's expiry + next week's
  * expiry days     -> the contract expiring today + next week's
  * monthly weeks   -> the monthly contract (the downloader's own `week`/`all`
                       modes skip monthly expiries; this wrapper does not)

It wraps scripts/nifty_weekly_options_daily_files_only.py (one run per expiry,
--expiry-date mode), so files land in data/nifty_options/daily/ as usual and
existing complete files are skipped. Safe to run several times a day.

Usage (from anywhere; paths are resolved from this file):
    venv/bin/python scripts/daily_download.py              # today, after 15:45 IST
    venv/bin/python scripts/daily_download.py --dry-run    # show what it would download
    venv/bin/python scripts/daily_download.py --date 2026-11-10   # a missed day (only if its
                                                          # contracts have not expired yet)
Exit codes: 0 ok / nothing to do, 1 a download failed, 2 Kite login/token problem.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOWNLOADER = PROJECT_ROOT / "scripts" / "nifty_weekly_options_daily_files_only.py"
ARCHIVE_DIR = PROJECT_ROOT / "data" / "instrument_archives"
HOLIDAYS_FILE = PROJECT_ROOT / "scripts" / "market_holidays.txt"
IST = ZoneInfo("Asia/Kolkata")
MARKET_CLOSE = dt_time(15, 45)
ARCHIVE_NAME_RE = re.compile(r"^nfo_instruments_(\d{8})_(\d{6})\.csv$")


def log(message: str) -> None:
    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST] {message}", flush=True)


def load_env() -> None:
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            # .env wins: the dashboard/token tool rewrites it with the fresh daily token.
            os.environ[key.strip().lstrip("﻿")] = value.strip().strip('"').strip("'")


def load_holidays(path: Path = HOLIDAYS_FILE) -> dict[date, str]:
    """Dates from scripts/market_holidays.txt ("YYYY-MM-DD  # name"). Missing file -> none."""
    holidays: dict[date, str] = {}
    if not path.exists():
        return holidays
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        text, _, comment = raw.partition("#")
        text = text.strip()
        if not text:
            continue
        try:
            holidays[date.fromisoformat(text)] = comment.strip()
        except ValueError:
            log(f"WARNING: {path.name} line {number} is not a YYYY-MM-DD date: {raw.strip()!r} (ignored)")
    return holidays


def nearest_expiries(kite, trade_day: date, count: int) -> list[date]:
    import pandas as pd

    master = pd.DataFrame(kite.instruments("NFO"))
    nifty = master[(master["name"] == "NIFTY") & master["segment"].astype(str).str.contains("OPT", na=False)]
    expiries = sorted({d for d in pd.to_datetime(nifty["expiry"], errors="coerce").dt.date.dropna() if d >= trade_day})
    return expiries[:count]


def prune_instrument_archives() -> int:
    """Remove redundant same-day archived NFO masters. Returns files removed.

    Each downloader run archives the full NFO master (~3.4 MB). Several runs a day
    would grow the folder by GBs a year. An older same-day master is deleted ONLY if
    every instrument token in it is also in that day's newest master, so no contract
    metadata is ever lost (a pre-refresh early-morning master can hold contracts the
    later one lacks; such files are kept).
    """
    import pandas as pd

    if not ARCHIVE_DIR.exists():
        return 0
    by_day: dict[str, list[Path]] = defaultdict(list)
    for path in ARCHIVE_DIR.iterdir():
        match = ARCHIVE_NAME_RE.match(path.name)
        if match:
            by_day[match.group(1)].append(path)

    def tokens(path: Path) -> set:
        return set(pd.read_csv(path, usecols=["instrument_token"])["instrument_token"].dropna().astype("int64"))

    removed = 0
    for paths in by_day.values():
        if len(paths) < 2:
            continue
        ordered = sorted(paths, key=lambda p: p.name)
        try:
            newest = tokens(ordered[-1])
        except Exception:
            continue
        for old in ordered[:-1]:
            try:
                if tokens(old) <= newest:
                    old.unlink()
                    removed += 1
            except Exception:
                continue
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="Trading date YYYY-MM-DD (default: today IST)")
    parser.add_argument("--expiries", type=int, default=2, help="How many nearest expiries to download (default 2)")
    parser.add_argument("--dry-run", action="store_true", help="Only print the expiries it would download")
    parser.add_argument("--force", action="store_true",
                        help="Run even before 15:45 IST or on a date listed in scripts/market_holidays.txt")
    args = parser.parse_args()

    now = datetime.now(IST)
    trade_day = date.fromisoformat(args.date) if args.date else now.date()
    log(f"daily download for {trade_day} (project {PROJECT_ROOT})")

    if trade_day.weekday() >= 5:
        log("weekend: nothing to download")
        return 0
    holidays = load_holidays()
    if trade_day in holidays and not args.force:
        name = f" ({holidays[trade_day]})" if holidays[trade_day] else ""
        log(f"market holiday{name}: nothing to download")
        return 0
    if trade_day == now.date() and now.time() < MARKET_CLOSE and not args.force:
        log(f"market not closed yet (run after {MARKET_CLOSE.strftime('%H:%M')} IST): nothing to do")
        return 0

    load_env()
    api_key, token = os.getenv("API_KEY", ""), os.getenv("ACCESS_TOKEN", "")
    if not api_key or not token:
        log("ERROR: API_KEY / ACCESS_TOKEN missing in .env")
        return 2

    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(token)
    try:
        kite.profile()
    except Exception as exc:
        log(f"ERROR: Kite login failed ({exc}). Today's ACCESS_TOKEN is missing or expired; "
            "generate it (dashboard / generate_token.py) and the next scheduled run will retry.")
        return 2

    try:
        expiries = nearest_expiries(kite, trade_day, max(1, args.expiries))
    except Exception as exc:
        log(f"ERROR: could not read the NFO instrument list: {exc}")
        return 1
    if not expiries:
        log("ERROR: no NIFTY option expiries on/after this date in Kite's instrument list")
        return 1
    tags = ", ".join(f"{e}{' (expires today)' if e == trade_day else ''}" for e in expiries)
    log(f"nearest expiries: {tags}")
    if args.dry_run:
        log("dry run: no download")
        return 0

    failures = 0
    for expiry in expiries:
        log(f"--- downloading expiry {expiry} ---")
        cmd = [sys.executable, str(DOWNLOADER), "--start-date", trade_day.isoformat(),
               "--end-date", trade_day.isoformat(), "--expiry-date", expiry.isoformat()]
        result = subprocess.run(cmd, cwd=PROJECT_ROOT, env=os.environ.copy())
        if result.returncode != 0:
            failures += 1
            log(f"ERROR: downloader exited with code {result.returncode} for expiry {expiry}")

    removed = prune_instrument_archives()
    if removed:
        log(f"pruned {removed} older same-day instrument list(s) in {ARCHIVE_DIR.name}/")
    log("done" if not failures else f"done with {failures} failed expiry download(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
