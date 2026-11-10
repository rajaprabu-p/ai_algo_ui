"""Daily parity check: compare the day's paper trades with a backtest of the same day.

Run after scripts/daily_download.py has saved the day's option candles. For each
strategy variant it:
  1. collects every paper trade of the day from STRATEGY_LOG_DIR
     (papertrade_<ddmmyyyy>_*_<variant>.xlsx, sheet live_trade_events; a day can
     have several files if the session was restarted),
  2. runs the variant's launcher in BACKTEST mode on the downloaded data, into a
     temporary folder (your log folder is not touched),
  3. pairs trades (same side, entry within ENTRY_MATCH_MINUTES) and compares
     entry/exit times, exit reasons and prices. The backtest's configured slippage
     is removed first, because paper trading fills at the signal price.

Verdicts: MATCH, DIFFERENT, NO_PAPER_TRADES (paper did not run or did not trade
while the backtest did), NO_TRADES (neither traded), NO_DATA (no downloaded
option files yet), ERROR. Reports go to logs/parity/parity_<date>.json and .txt.

Usage (from anywhere; paths resolve from this file):
    venv/bin/python scripts/paper_vs_backtest.py                  # today, both variants
    venv/bin/python scripts/paper_vs_backtest.py --date 2026-11-11 --variant v28
Exit codes: 0 MATCH / no trades / nothing to do, 1 DIFFERENT or ERROR, 2 no data yet.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
OPTION_DAILY_DIR = PROJECT_ROOT / "data" / "nifty_options" / "daily"
REPORT_DIR = PROJECT_ROOT / "logs" / "parity"
IST = ZoneInfo("Asia/Kolkata")
VARIANTS = {"v28": "v28_strategy_main.py", "v30": "v30_strategy_main.py"}
ENTRY_MATCH_MINUTES = 3        # paper and backtest entries this close are the same trade
PRICE_TOLERANCE_POINTS = 1.5   # price differences above this (after removing slippage) are flagged
EXIT_TIME_TOLERANCE_MINUTES = 3
BACKTEST_TIMEOUT_SECONDS = 900


def log(message: str) -> None:
    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')} IST] {message}", flush=True)


def load_env() -> dict:
    values = {}
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def strategy_log_dir(env: dict) -> Path:
    raw = os.environ.get("STRATEGY_LOG_DIR") or env.get("STRATEGY_LOG_DIR") or str(PROJECT_ROOT / "logs")
    return Path(raw.strip() or str(PROJECT_ROOT / "logs"))


def _naive(series: pd.Series) -> pd.Series:
    ts = pd.to_datetime(series, errors="coerce")
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert(IST).dt.tz_localize(None)
    return ts


def load_paper_trades(log_dir: Path, trade_day: date, variant: str) -> tuple[pd.DataFrame, list[str]]:
    """All paper trades of the day for one variant (merged across restarted sessions)."""
    pattern = re.compile(rf"^papertrade_{trade_day:%d%m%Y}_\d{{6}}_.*_{re.escape(variant)}\.xlsx$")
    files = sorted(p for p in log_dir.glob(f"papertrade_{trade_day:%d%m%Y}_*.xlsx") if pattern.match(p.name))
    frames = []
    for path in files:
        try:
            frame = pd.read_excel(path, sheet_name="live_trade_events")
        except Exception:
            continue
        if len(frame):
            frames.append(frame)
    if not frames:
        return pd.DataFrame(), [p.name for p in files]
    trades = pd.concat(frames, ignore_index=True)
    trades["entry_time"] = _naive(trades["entry_time"])
    trades["exit_time"] = _naive(trades["exit_time"])
    trades = trades.dropna(subset=["entry_time"])
    trades = trades.drop_duplicates(subset=["side", "option_symbol", "entry_time"], keep="last")
    return trades.sort_values("entry_time").reset_index(drop=True), [p.name for p in files]


def option_files_exist(trade_day: date) -> bool:
    return any(OPTION_DAILY_DIR.glob(f"nifty_weekly_*_{trade_day.isoformat()}.csv"))


def run_backtest(variant: str, trade_day: date, env_file: dict | None = None) -> tuple[pd.DataFrame, dict, str]:
    """Backtest one day in a temp copy of scripts/. Returns (trades, summary metrics, error text).

    The strategy loads the project's .env with override=True, which would replace
    STRATEGY_LOG_DIR and write the backtest Excel into the real log folder. Running
    a copy of scripts/ (no .env beside it) keeps the run fully isolated; the Kite
    keys are passed as environment variables, and the copy keeps the real
    editable_config.json so the backtest uses the same settings as paper trading.
    """
    tmp = Path(tempfile.mkdtemp(prefix=f"parity_{variant}_"))
    try:
        work = tmp / "scripts"
        shutil.copytree(SCRIPTS_DIR, work, ignore=shutil.ignore_patterns(
            "tests", "__pycache__", "data", "*.lock", "*.mode", "*.orig"))
        out_dir = tmp / "logs"
        out_dir.mkdir()
        env = os.environ.copy()
        for key in ("API_KEY", "ACCESS_TOKEN"):
            if (env_file or {}).get(key) and not env.get(key):
                env[key] = env_file[key]
        env.update(STRATEGY_LOG_DIR=str(out_dir), OPTION_DAILY_DIR=str(OPTION_DAILY_DIR),
                   PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", MPLBACKEND="Agg")
        cmd = [sys.executable, str(work / VARIANTS[variant]), "--mode", "BACKTEST",
               "--date", trade_day.isoformat(), "--option-data-source", "DOWNLOADED"]
        proc = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=BACKTEST_TIMEOUT_SECONDS)
        books = sorted(out_dir.glob("backtest_*.xlsx"))
        if proc.returncode != 0 or len(books) != 1:
            tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-8:])
            return pd.DataFrame(), {}, f"backtest exit code {proc.returncode}, workbooks {len(books)}:\n{tail}"
        trades = pd.read_excel(books[0], sheet_name="trades")
        summary = pd.read_excel(books[0], sheet_name="summary")
        metrics = dict(zip(summary["metric"].astype(str), summary["value"]))
        metrics["_workbook_name"] = books[0].name
        if len(trades):
            trades["entry_time"] = _naive(trades["entry_time"])
            trades["exit_time"] = _naive(trades["exit_time"])
        return trades, metrics, ""
    except subprocess.TimeoutExpired:
        return pd.DataFrame(), {}, f"backtest timed out after {BACKTEST_TIMEOUT_SECONDS}s"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def settings_tag_from_name(name: str) -> str:
    """Settings part of a strategy Excel name, e.g. 'mtf5min_mfe_max2_sl25'.

    papertrade_<ddmmyyyy>_<hhmmss>_<settings>_<variant>.xlsx
    backtest_<ddmmyyyy>_<settings>_<variant>_<csv|kite>.xlsx
    """
    match = re.search(r"_(mtf[^_]+(?:_mfe)?_max\d+_sl\d+)_v\d+", str(name))
    return match.group(1) if match else ""


def settings_mismatches(paper_files: list[str], backtest_workbook: str) -> list[str]:
    bt = settings_tag_from_name(backtest_workbook)
    if not bt:
        return []
    return sorted({f for f in paper_files if settings_tag_from_name(f) and settings_tag_from_name(f) != bt})


def backtest_slippage(metrics: dict) -> tuple[float, float]:
    """Entry/exit slippage the backtest applied, from its summary ('entry 1 / exit 1' or 'off')."""
    text = str(metrics.get("backtest_slippage_points", "off"))
    match = re.match(r"entry\s+([\d.]+)\s*/\s*exit\s+([\d.]+)", text)
    return (float(match.group(1)), float(match.group(2))) if match else (0.0, 0.0)


def compare(paper: pd.DataFrame, backtest: pd.DataFrame, entry_slip: float = 0.0, exit_slip: float = 0.0) -> dict:
    """Pair trades and list the differences. Pure function (no I/O) so it can be unit-tested."""
    if paper.empty and backtest.empty:
        return {"verdict": "NO_TRADES", "pairs": [], "only_paper": [], "only_backtest": [], "issues": []}
    if paper.empty:
        return {"verdict": "NO_PAPER_TRADES", "pairs": [], "only_paper": [],
                "only_backtest": [_trade_row(t) for _, t in backtest.iterrows()],
                "issues": ["backtest traded but no paper trades were found for this day"]}

    used_bt: set[int] = set()
    pairs, only_paper, issues = [], [], []
    for _, p in paper.iterrows():
        best, best_gap = None, None
        for j, b in backtest.iterrows():
            if j in used_bt or str(b["side"]) != str(p["side"]):
                continue
            gap = abs((b["entry_time"] - p["entry_time"]).total_seconds()) / 60.0
            if gap <= ENTRY_MATCH_MINUTES and (best_gap is None or gap < best_gap):
                best, best_gap = j, gap
        if best is None:
            only_paper.append(_trade_row(p))
            issues.append(f"paper {p['side']} at {p['entry_time']:%H:%M} has no matching backtest trade")
            continue
        used_bt.add(best)
        b = backtest.loc[best]
        bt_entry = float(b["entry_price"]) - entry_slip
        bt_exit = float(b["exit_price"]) + exit_slip
        pair = {
            "side": str(p["side"]),
            "paper": _trade_row(p),
            "backtest": _trade_row(b),
            "entry_minutes_apart": round(best_gap, 1),
            "entry_price_diff": round(float(p["entry_price"]) - bt_entry, 2),
            "exit_price_diff": round(float(p["exit_price"]) - bt_exit, 2),
            "exit_minutes_apart": round(abs((b["exit_time"] - p["exit_time"]).total_seconds()) / 60.0, 1)
            if pd.notna(p.get("exit_time")) and pd.notna(b.get("exit_time")) else None,
            "same_exit_reason": str(p.get("exit_reason")) == str(b.get("exit_reason")),
            "same_symbol": str(p.get("option_symbol")) == str(b.get("option_symbol")),
        }
        pairs.append(pair)
        tag = f"{pair['side']} {p['entry_time']:%H:%M}"
        if not pair["same_symbol"]:
            issues.append(f"{tag}: different contract (paper {p.get('option_symbol')}, backtest {b.get('option_symbol')})")
        if not pair["same_exit_reason"]:
            issues.append(f"{tag}: exit reason differs (paper {p.get('exit_reason')}, backtest {b.get('exit_reason')})")
        if abs(pair["entry_price_diff"]) > PRICE_TOLERANCE_POINTS:
            issues.append(f"{tag}: entry price differs by {pair['entry_price_diff']:+.2f} pts")
        if abs(pair["exit_price_diff"]) > PRICE_TOLERANCE_POINTS:
            issues.append(f"{tag}: exit price differs by {pair['exit_price_diff']:+.2f} pts")
        if pair["exit_minutes_apart"] is not None and pair["exit_minutes_apart"] > EXIT_TIME_TOLERANCE_MINUTES:
            issues.append(f"{tag}: exit time differs by {pair['exit_minutes_apart']:.0f} min")
    only_backtest = [_trade_row(b) for j, b in backtest.iterrows() if j not in used_bt]
    for t in only_backtest:
        issues.append(f"backtest {t['side']} at {t['entry_time'][11:16]} has no matching paper trade")
    return {"verdict": "DIFFERENT" if issues else "MATCH", "pairs": pairs,
            "only_paper": only_paper, "only_backtest": only_backtest, "issues": issues}


def _trade_row(t) -> dict:
    def fmt(value):
        return value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, (pd.Timestamp, datetime)) and pd.notna(value) else str(value)
    return {
        "side": str(t.get("side")), "option_symbol": str(t.get("option_symbol")),
        "entry_time": fmt(t.get("entry_time")), "exit_time": fmt(t.get("exit_time")),
        "entry_price": round(float(t.get("entry_price", 0) or 0), 2), "exit_price": round(float(t.get("exit_price", 0) or 0), 2),
        "pnl_points": round(float(t.get("pnl_points", 0) or 0), 2), "exit_reason": str(t.get("exit_reason")),
    }


def write_report(trade_day: date, results: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORT_DIR / f"parity_{trade_day.isoformat()}.json"
    json_path.write_text(json.dumps({"trade_date": trade_day.isoformat(),
                                     "checked_at": datetime.now(IST).isoformat(timespec="seconds"),
                                     "variants": results}, indent=2), encoding="utf-8")
    lines = [f"Paper vs backtest, {trade_day.isoformat()}"]
    for variant, r in results.items():
        lines.append(f"\n[{variant}] {r['verdict']}  (paper trades {r.get('paper_trades', 0)}, backtest trades {r.get('backtest_trades', 0)})")
        lines += [f"  - {issue}" for issue in r.get("issues", [])]
    (REPORT_DIR / f"parity_{trade_day.isoformat()}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", help="Trading date YYYY-MM-DD (default: today IST)")
    parser.add_argument("--variant", choices=sorted(VARIANTS), action="append", help="Default: v28 and v30")
    args = parser.parse_args()

    now = datetime.now(IST)
    trade_day = date.fromisoformat(args.date) if args.date else now.date()
    variants = args.variant or sorted(VARIANTS)
    log(f"paper vs backtest check for {trade_day} ({', '.join(variants)})")

    if trade_day.weekday() >= 5:
        log("weekend: nothing to check")
        return 0
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        from daily_download import load_holidays

        if trade_day in load_holidays():
            log("market holiday: nothing to check")
            return 0
    except Exception:
        pass
    if trade_day == now.date() and now.time() < dt_time(15, 45):
        log("market not closed yet: nothing to check")
        return 0
    if not option_files_exist(trade_day):
        log(f"no downloaded option files for {trade_day} yet (run daily_download.py first)")
        return 2

    env = load_env()
    log_dir = strategy_log_dir(env)
    results, worst = {}, 0
    for variant in variants:
        paper, files = load_paper_trades(log_dir, trade_day, variant)
        backtest, metrics, error = run_backtest(variant, trade_day, env)
        if error:
            results[variant] = {"verdict": "ERROR", "issues": [error], "paper_files": files,
                                "paper_trades": len(paper), "backtest_trades": 0}
            log(f"{variant}: ERROR {error.splitlines()[0]}")
            worst = 1
            continue
        entry_slip, exit_slip = backtest_slippage(metrics)
        result = compare(paper, backtest, entry_slip, exit_slip)
        mismatched = settings_mismatches(files, metrics.get("_workbook_name", ""))
        if mismatched:
            bt_tag = settings_tag_from_name(metrics.get("_workbook_name", ""))
            result["issues"].insert(0, (
                f"SETTINGS DIFFER: paper session(s) ran with "
                f"{', '.join(sorted({settings_tag_from_name(f) for f in mismatched}))} but the backtest uses "
                f"{bt_tag}; differences below may come from the settings, not the strategy"))
            result["verdict"] = "DIFFERENT"
        if len(files) > 1:
            result["issues"].append(
                f"note: {len(files)} paper sessions that day (restarts); each restart starts a fresh trade count")
        result.update(paper_files=files, paper_trades=len(paper), backtest_trades=len(backtest),
                      backtest_slippage_removed={"entry": entry_slip, "exit": exit_slip},
                      settings_mismatch_files=mismatched)
        results[variant] = result
        log(f"{variant}: {result['verdict']} (paper {len(paper)} trade(s) from {len(files)} file(s), backtest {len(backtest)})")
        for issue in result["issues"]:
            log(f"{variant}:   - {issue}")
        if result["verdict"] == "DIFFERENT":
            worst = 1
    path = write_report(trade_day, results)
    log(f"report: {path}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
