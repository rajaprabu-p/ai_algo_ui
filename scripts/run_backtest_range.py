"""
run_backtest_range.py
---------------------
Runs the strategy script (STRATEGY_SCRIPT) for each trading day in a date range.

Usage:
    py run_backtest_range.py 2026-06-15 2026-06-19
    py run_backtest_range.py 2026-06-01 2026-06-12 --skip-weekends   (default: skips weekends)
    py run_backtest_range.py 2026-06-01 2026-06-12 --include-weekends
"""

import os
import sys
import subprocess
import argparse
from datetime import date, timedelta

DEFAULT_STRATEGY_SCRIPT = os.path.join("scripts", "v30_strategy_main.py")


def date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(description="Loop backtest over a date range")
    parser.add_argument("start_date", help="Start date YYYY-MM-DD")
    parser.add_argument("end_date",   help="End date   YYYY-MM-DD")
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY_SCRIPT,
                        help="Strategy script filename")
    parser.add_argument("--mode", default="BACKTEST",
                        choices=("BACKTEST",), help="Strategy run mode")
    parser.add_argument(
        "--option-data-source",
        default="DOWNLOADED",
        choices=("DOWNLOADED", "KITE"),
        help="Option candle source for the strategy backtest",
    )
    parser.add_argument("--include-weekends", action="store_true",
                        help="Also run on Saturdays and Sundays")
    args = parser.parse_args()

    try:
        start = date.fromisoformat(args.start_date)
        end   = date.fromisoformat(args.end_date)
    except ValueError as e:
        print(f"[ERROR] Invalid date format: {e}")
        sys.exit(1)

    if start > end:
        print("[ERROR] start_date must be <= end_date")
        sys.exit(1)

    dates = [
        d for d in date_range(start, end)
        if args.include_weekends or d.weekday() < 5   # Mon=0 … Fri=4
    ]

    if not dates:
        print("[INFO] No dates to run after filtering.")
        sys.exit(0)

    print(f"[INFO] Running backtest for {len(dates)} day(s): "
          f"{dates[0].isoformat()} to {dates[-1].isoformat()}\n")

    failed = []
    for i, d in enumerate(dates, 1):
        ds = d.isoformat()
        print(f"{'='*60}")
        print(f"[{i}/{len(dates)}] Running: {ds}")
        print(f"{'='*60}")
        child_env = os.environ.copy()
        result = subprocess.run(
            [
                sys.executable,
                args.strategy,
                "--date",
                ds,
                "--mode",
                args.mode,
                "--option-data-source",
                args.option_data_source,
            ],
            cwd=".",
            env=child_env,
        )
        if result.returncode != 0:
            print(f"[WARNING] Script exited with code {result.returncode} for {ds}")
            failed.append(ds)
        print()

    print(f"{'='*60}")
    print(f"[DONE] Completed {len(dates)} runs.")
    if failed:
        print(f"[FAILED] {len(failed)} date(s) had errors: {', '.join(failed)}")
    else:
        print("[OK] All dates ran successfully.")


if __name__ == "__main__":
    main()
