"""One-time capture of NIFTY 50 1-minute spot candles for the backtest snapshot tests.

Read-only: it only calls kite.historical_data(). It saves one CSV per trading
day to scripts/tests/fixtures/spot/YYYY-MM-DD.csv, covering every day that has
downloaded option files plus the trading days just before them (the strategy
reads the previous day's OHLC for gap and CPR context).

Usage (needs a valid ACCESS_TOKEN in .env):
    venv\\Scripts\\python.exe scripts\\tests\\capture_spot_fixtures.py
    venv\\Scripts\\python.exe scripts\\tests\\capture_spot_fixtures.py --from 2026-09-28 --to 2026-11-06
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent.parent
SPOT_DIR = TESTS_DIR / "fixtures" / "spot"
OPTION_DAILY_DIR = Path(os.environ.get("OPTION_DAILY_DIR") or PROJECT_ROOT / "data" / "nifty_options" / "daily")
NIFTY_50_TOKEN = 256265
PREVIOUS_DAY_LOOKBACK = 7


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def option_trade_dates() -> list[date]:
    found = set()
    for path in OPTION_DAILY_DIR.glob("nifty_weekly_*_*.csv"):
        match = re.search(r"_(\d{4}-\d{2}-\d{2})\.csv$", path.name)
        if match:
            found.add(date.fromisoformat(match.group(1)))
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="date_from", default=None)
    parser.add_argument("--to", dest="date_to", default=None)
    parser.add_argument("--force", action="store_true", help="Re-download days that are already saved")
    args = parser.parse_args()

    trade_dates = option_trade_dates()
    if args.date_from or args.date_to:
        start = date.fromisoformat(args.date_from) if args.date_from else trade_dates[0]
        end = date.fromisoformat(args.date_to) if args.date_to else trade_dates[-1]
    elif trade_dates:
        start, end = trade_dates[0] - timedelta(days=PREVIOUS_DAY_LOOKBACK), trade_dates[-1]
    else:
        print(f"No option files found in {OPTION_DAILY_DIR}; pass --from/--to.")
        return 1

    env = {**read_env(PROJECT_ROOT / ".env"), **os.environ}
    api_key, access_token = env.get("API_KEY", ""), env.get("ACCESS_TOKEN", "")
    if not api_key or not access_token:
        print("API_KEY / ACCESS_TOKEN missing from .env")
        return 1

    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    try:
        kite.profile()
    except Exception as exc:  # token expired, etc.
        print(f"Kite login failed: {exc}\nGenerate a fresh ACCESS_TOKEN and retry.")
        return 1

    SPOT_DIR.mkdir(parents=True, exist_ok=True)
    saved = skipped = empty = 0
    day = start
    while day <= end:
        target = SPOT_DIR / f"{day.isoformat()}.csv"
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        if target.exists() and not args.force:
            skipped += 1
            day += timedelta(days=1)
            continue
        rows = kite.historical_data(
            instrument_token=NIFTY_50_TOKEN,
            from_date=f"{day.isoformat()} 09:15:00",
            to_date=f"{day.isoformat()} 15:30:00",
            interval="minute",
            continuous=False,
            oi=False,
        )
        if rows:
            frame = pd.DataFrame(rows)
            frame["date"] = pd.to_datetime(frame["date"]).map(lambda ts: ts.isoformat())
            frame[["date", "open", "high", "low", "close", "volume"]].to_csv(target, index=False)
            print(f"saved  {target.name}  ({len(frame)} candles)")
            saved += 1
        else:
            print(f"empty  {day.isoformat()}  (holiday?)")
            empty += 1
        time.sleep(0.35)  # stay well under Kite's historical API rate limit
        day += timedelta(days=1)

    print(f"done: saved={saved} already_had={skipped} empty={empty} -> {SPOT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
