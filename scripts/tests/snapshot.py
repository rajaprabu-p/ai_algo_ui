"""Backtest snapshot harness: proves a refactor did not change any trades.

For each strategy variant (v28, v30) and each trading day that has both saved
spot candles (fixtures/spot) and downloaded option files (data/nifty_options/daily),
it runs the real launcher script in BACKTEST mode, reads the `trades` sheet of
the Excel it writes, and compares it with the saved golden file
golden/<variant>/<date>.csv.

Isolation, so a test run can never touch real accounts or real settings:
  * scripts/ is copied to a temp folder (no .env next to it, so no real keys load)
  * a fake `kiteconnect` package serves saved spot candles and refuses orders
  * settings are pinned by fixtures/editable_config.json, not the UI's copy
  * Excel / logs go to the temp folder, not logs/

Usage (from the project root, with the venv python):
    venv\\Scripts\\python.exe scripts\\tests\\snapshot.py              # compare with golden
    venv\\Scripts\\python.exe scripts\\tests\\snapshot.py --update     # (re)write golden files
    venv\\Scripts\\python.exe scripts\\tests\\snapshot.py --variant v28 --date 2026-10-14
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
PROJECT_ROOT = SCRIPTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
SPOT_DIR = FIXTURES_DIR / "spot"
PINNED_CONFIG = FIXTURES_DIR / "editable_config.json"
GOLDEN_DIR = TESTS_DIR / "golden"
FAKE_KITE_DIR = TESTS_DIR / "fake_kite"
OPTION_DAILY_DIR = Path(os.environ.get("OPTION_DAILY_DIR") or PROJECT_ROOT / "data" / "nifty_options" / "daily")

VARIANTS = {"v28": "v28_strategy_main.py", "v30": "v30_strategy_main.py"}
RUN_TIMEOUT_SECONDS = 600
FLOAT_FORMAT = "%.4f"
NO_TRADES = "# no trades\n"


@dataclass
class RunResult:
    variant: str
    trade_date: str
    csv_text: str | None
    error: str = ""
    log_path: Path | None = None
    seconds: float = 0.0


def available_dates() -> list[str]:
    spot_days = {p.stem for p in SPOT_DIR.glob("*.csv")}
    option_days = set()
    for path in OPTION_DAILY_DIR.glob("nifty_weekly_*_*.csv"):
        match = re.search(r"_(\d{4}-\d{2}-\d{2})\.csv$", path.name)
        if match:
            option_days.add(match.group(1))
    return sorted(spot_days & option_days)


def prepare_workspace(root: Path) -> Path:
    """Copy scripts/ into a throw-away folder with pinned settings."""
    work_scripts = root / "scripts"
    shutil.copytree(
        SCRIPTS_DIR,
        work_scripts,
        ignore=shutil.ignore_patterns("tests", "__pycache__", "*.lock", "*.orig", "*.mode", "data", "editable_config.json"),
    )
    shutil.copyfile(PINNED_CONFIG, work_scripts / "editable_config.json")
    return work_scripts


def trades_to_csv(trades: pd.DataFrame) -> str:
    if trades.empty or len(trades.columns) == 0:
        return NO_TRADES
    return trades.to_csv(index=False, float_format=FLOAT_FORMAT, lineterminator="\n")


def run_one(work_scripts: Path, variant: str, trade_date: str) -> RunResult:
    run_dir = work_scripts.parent / "runs" / f"{variant}_{trade_date}"
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "console.log"

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"API_KEY", "API_SECRET", "ACCESS_TOKEN", "STRATEGY_LOG_DIR", "OPTION_DAILY_DIR", "PYTHONPATH"}
    }
    env.update(
        {
            "PYTHONPATH": str(FAKE_KITE_DIR),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "MPLBACKEND": "Agg",
            "API_KEY": "snapshot-test",
            "ACCESS_TOKEN": "snapshot-test",
            "FAKE_KITE_SPOT_DIR": str(SPOT_DIR),
            "OPTION_DAILY_DIR": str(OPTION_DAILY_DIR),
            "STRATEGY_LOG_DIR": str(log_dir),
            "BACKTEST_OPTION_DATA_SOURCE": "DOWNLOADED",
        }
    )
    cmd = [
        sys.executable,
        str(work_scripts / VARIANTS[variant]),
        "--mode", "BACKTEST",
        "--date", trade_date,
        "--option-data-source", "DOWNLOADED",
    ]
    started = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as log_file:
        try:
            proc = subprocess.run(
                cmd, cwd=run_dir, env=env, stdout=log_file, stderr=subprocess.STDOUT,
                timeout=RUN_TIMEOUT_SECONDS, check=False,
            )
            code = proc.returncode
        except subprocess.TimeoutExpired:
            return RunResult(variant, trade_date, None, f"timed out after {RUN_TIMEOUT_SECONDS}s", log_path)
    seconds = time.monotonic() - started

    if code != 0:
        return RunResult(variant, trade_date, None, f"exit code {code}", log_path, seconds)
    workbooks = sorted(log_dir.glob("backtest_*.xlsx"))
    if len(workbooks) != 1:
        return RunResult(variant, trade_date, None, f"expected 1 backtest workbook, found {len(workbooks)}", log_path, seconds)
    trades = pd.read_excel(workbooks[0], sheet_name="trades")
    return RunResult(variant, trade_date, trades_to_csv(trades), "", log_path, seconds)


def golden_path(variant: str, trade_date: str) -> Path:
    return GOLDEN_DIR / variant / f"{trade_date}.csv"


def run_all(variants: list[str], dates: list[str], workers: int) -> tuple[list[RunResult], Path]:
    """Run every (variant, date). The caller decides whether to delete the returned workspace."""
    root = Path(tempfile.mkdtemp(prefix="algo_snapshot_"))
    work_scripts = prepare_workspace(root)
    jobs = [(variant, trade_date) for variant in variants for trade_date in dates]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(lambda job: run_one(work_scripts, *job), jobs))
    return results, root


def compare(result: RunResult) -> str:
    """Return '' when the run matches golden, else a short diff/explanation."""
    if result.error:
        return f"run failed: {result.error} (log: {result.log_path})"
    path = golden_path(result.variant, result.trade_date)
    if not path.exists():
        return f"no golden file {path.relative_to(TESTS_DIR)} (run with --update)"
    expected = path.read_text(encoding="utf-8")
    if expected == result.csv_text:
        return ""
    diff = difflib.unified_diff(
        expected.splitlines(), (result.csv_text or "").splitlines(),
        fromfile=f"golden/{result.variant}/{result.trade_date}", tofile="this run", lineterm="", n=0,
    )
    return "trades differ:\n" + "\n".join(list(diff)[:40])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true", help="Write golden files from this run")
    parser.add_argument("--variant", choices=sorted(VARIANTS), action="append")
    parser.add_argument("--date", action="append", help="Limit to these dates (repeatable)")
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 2))
    parser.add_argument("--keep", action="store_true", help="Keep the temp workspace (logs, Excel) afterwards")
    args = parser.parse_args()

    variants = args.variant or sorted(VARIANTS)
    dates = args.date or available_dates()
    if not dates:
        print("No days with both spot fixtures and option files. Run capture_spot_fixtures.py first.")
        return 1

    print(f"Running {len(variants)} variant(s) x {len(dates)} day(s) with {args.workers} workers...")
    started = time.monotonic()
    results, root = run_all(variants, dates, args.workers)
    failures = 0
    for result in results:
        if args.update:
            if result.error:
                failures += 1
                print(f"FAIL   {result.variant} {result.trade_date}: {result.error} (log: {result.log_path})")
                continue
            path = golden_path(result.variant, result.trade_date)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result.csv_text or NO_TRADES, encoding="utf-8")
            trades = 0 if result.csv_text == NO_TRADES else max(0, (result.csv_text or "").count("\n") - 1)
            print(f"saved  {result.variant} {result.trade_date}: {trades} trade(s)  [{result.seconds:.0f}s]")
        else:
            problem = compare(result)
            if problem:
                failures += 1
                print(f"FAIL   {result.variant} {result.trade_date}: {problem}")
            else:
                print(f"ok     {result.variant} {result.trade_date}  [{result.seconds:.0f}s]")

    print(f"{len(results) - failures}/{len(results)} passed in {time.monotonic() - started:.0f}s")
    if failures or args.keep:
        print(f"Workspace kept for inspection (console.log + Excel per run): {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
