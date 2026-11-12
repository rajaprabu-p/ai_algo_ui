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
    day_results = []   # (date, trades, net_rs) for the summary block at the end
    for i, d in enumerate(dates, 1):
        ds = d.isoformat()
        print(f"{'='*60}")
        print(f"[{i}/{len(dates)}] Running: {ds}", flush=True)
        print(f"{'='*60}", flush=True)
        child_env = os.environ.copy()
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        # Pass the day's output through unchanged (same log as before) and note its
        # trade count and net P&L for the range summary.
        proc = subprocess.Popen(
            [
                sys.executable, "-u",
                args.strategy,
                "--date", ds,
                "--mode", args.mode,
                "--option-data-source", args.option_data_source,
            ],
            cwd=".", env=child_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        trades, net = 0, None
        for line in proc.stdout:
            print(line, end="", flush=True)
            parsed = parse_day_line(line)
            if parsed:
                key, value = parsed
                if key == "trades":
                    trades = int(value)
                else:
                    net = value
        returncode = proc.wait()
        if returncode != 0:
            print(f"[WARNING] Script exited with code {returncode} for {ds}")
            failed.append(ds)
        else:
            day_results.append((ds, trades, 0.0 if net is None else net))
        print()

    print(f"{'='*60}")
    print(f"[DONE] Completed {len(dates)} runs.")
    if failed:
        print(f"[FAILED] {len(failed)} date(s) had errors: {', '.join(failed)}")
    else:
        print("[OK] All dates ran successfully.")
    print_range_summary(day_results, len(dates))


def parse_day_line(line: str):
    """('trades', n) or ('net', rupees) from a day's result block, else None."""
    text = line.strip()
    for prefix, key in (("Total trades", "trades"), ("Net P&L (Rs)", "net")):
        if text.startswith(prefix) and ":" in text:
            try:
                value = float(text.split(":", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                return None
            return key, (int(value) if key == "trades" else value)
    return None


def print_range_summary(day_results, total_days: int) -> None:
    """Range totals at the very end of the log, so the dashboard reads them from the log tail."""
    net = round(sum(r[2] for r in day_results), 2)
    trades = sum(r[1] for r in day_results)
    print(f"{'='*60}")
    for ds, n, rs in day_results:
        print(f"[RANGE DAY] {ds} trades={n} net_rs={rs:.2f}")
    print(f"[RANGE SUMMARY] days={len(day_results)}/{total_days} trades={trades} net_rs={net:.2f} "
          f"winning_days={sum(1 for r in day_results if r[2] > 0)} losing_days={sum(1 for r in day_results if r[2] < 0)}",
          flush=True)


if __name__ == "__main__":
    main()
