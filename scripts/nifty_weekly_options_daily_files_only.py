#!/usr/bin/env python3
"""
Collect configurable date-range NIFTY weekly-expiry CE/PE 1-minute candles from Kite.

Behavior:
- No manual expiry/data date variables required.
- Finds the nearest upcoming NIFTY weekly expiry in the current NFO instrument master,
  excluding the monthly expiry (latest listed expiry in that calendar month).
- Downloads the selected expiry's candles for the configured date range; without an
  explicit expiry, selects the next listed weekly expiry after the start date.
- Saves a dated instrument master and a contract manifest.
- If a requested specific expiry is unavailable, displays any listed weekly expiry
  candidates in that calendar week without switching dates automatically.
- Without an explicit expiry, selects the nearest weekly expiry after the start date.
- Saves one CSV per trading date and expiry. Does not create consolidated/merged CSVs. If a daily file already exists, skips
  downloading that date. On the next run, it only downloads dates whose files are absent.
- Run after market close each trading day to archive that day's candles.

Important:
- It cannot retrieve expired contracts/tokens absent from today's instrument master.
- Weekly/monthly classification is inferred from the current master; validate holiday
  weeks against NSE's official expiry calendar.
- This script saves files per date so existing days are skipped, rather than re-downloaded.

Install:
    py -m pip install kiteconnect pandas

Windows CMD:
    set KITE_API_KEY=your_api_key
    set KITE_ACCESS_TOKEN=your_access_token
    set START_DATE=2026-10-05
    set END_DATE=2026-10-09
    set EXPIRY_MODE=specific
    set SPECIFIC_EXPIRY_DATE=2026-10-13
    set OPTION_TYPES=CE,PE
    set SKIP_EXISTING=true
    py scripts\\nifty_weekly_options_daily_files_only.py

Alternatively, add API_KEY and ACCESS_TOKEN to the repository root .env file.
"""

import os
import re
import sys
import time
import argparse
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from kiteconnect import KiteConnect


WORKDIR = Path(__file__).resolve().parents[1]


def load_env_file() -> None:
    env_path = WORKDIR / ".env"
    if not env_path.exists():
        return

    try:
        with env_path.open("r", encoding="utf-8-sig") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().lstrip("\ufeff")
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError as exc:
        raise RuntimeError(f"Unable to read environment file {env_path}: {exc}") from exc


load_env_file()

_cli = argparse.ArgumentParser(description="Download daily NIFTY weekly option candle CSVs")
_cli.add_argument("--start-date", help="First trading date to download (YYYY-MM-DD)")
_cli.add_argument("--end-date", help="Last trading date to download (YYYY-MM-DD)")
_cli.add_argument("--expiry-date", help="Exact option expiry to download (YYYY-MM-DD)")
_cli.add_argument(
    "--expiry-mode",
    choices=("specific", "week", "all"),
    help="Expiry selection mode; defaults to the next weekly expiry",
)
_cli_args, _ = _cli.parse_known_args()

ARCHIVE_DIR = Path(os.getenv("INSTRUMENT_ARCHIVE_DIR", "data/instrument_archives"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "data/nifty_options"))
PAUSE = float(os.getenv("KITE_REQUEST_PAUSE_SECONDS", "0.4"))
IST = ZoneInfo("Asia/Kolkata")
MARKET_CLOSE_TIME = dt_time(15, 45)

# Download configuration (override via environment variables).
# Dates are inclusive and use YYYY-MM-DD.
TODAY = datetime.now(IST).date().isoformat()
START_DATE = _cli_args.start_date or os.getenv("START_DATE", TODAY)
END_DATE = _cli_args.end_date or os.getenv("END_DATE", START_DATE)
configured_expiry_mode = _cli_args.expiry_mode or os.getenv("EXPIRY_MODE")
if _cli_args.expiry_date:
    EXPIRY_MODE = "specific"
elif configured_expiry_mode:
    EXPIRY_MODE = configured_expiry_mode.strip().lower()
elif os.getenv("SPECIFIC_EXPIRY_DATE"):
    EXPIRY_MODE = "specific"
else:
    EXPIRY_MODE = "week"
SPECIFIC_EXPIRY_DATE = _cli_args.expiry_date or os.getenv("SPECIFIC_EXPIRY_DATE")
OPTION_TYPES = {
    value.strip().upper()
    for value in os.getenv("OPTION_TYPES", "CE,PE").split(",")
    if value.strip()
}
SKIP_EXISTING = os.getenv("SKIP_EXISTING", "true").strip().lower() in {"1", "true", "yes", "y"}



def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def should_skip_current_date(day: date, now: datetime | None = None) -> bool:
    """Avoid downloading an incomplete current-day candle file before market close."""
    current = now or datetime.now(IST)
    return current.date() == day and current.timetz().replace(tzinfo=None) < MARKET_CLOSE_TIME


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


REQUEST_ATTEMPTS = int(os.getenv("KITE_REQUEST_ATTEMPTS", "3"))
FAILED_LINE_RE = re.compile(r"^(\S+) token=(\d+):")


def failed_tokens_from(error_path: Path) -> set[int]:
    """Instrument tokens listed in an errors_<expiry>_<day>.txt file."""
    tokens = set()
    for line in error_path.read_text(encoding="utf-8").splitlines():
        match = FAILED_LINE_RE.match(line.strip())
        if match:
            tokens.add(int(match.group(2)))
    return tokens


def fetch_contract_day(kite, inst, day: date, expiry: date) -> list[dict]:
    """One contract's minute candles for one day, retrying failed requests with a backoff."""
    token = int(inst["instrument_token"])
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        try:
            candles = kite.historical_data(
                instrument_token=token,
                from_date=datetime.combine(day, dt_time(9, 15)),
                to_date=datetime.combine(day, dt_time(15, 30)),
                interval="minute",
                continuous=False,
                oi=True,
            )
            break
        except Exception:
            if attempt == REQUEST_ATTEMPTS:
                raise
            time.sleep(2 * attempt)
    return [
        {
            "timestamp": pd.Timestamp(candle["date"]).isoformat(),
            "trade_date": day.isoformat(),
            "expiry": expiry.isoformat(),
            "tradingsymbol": str(inst["tradingsymbol"]),
            "instrument_token": token,
            "strike": float(inst["strike"]),
            "option_type": str(inst["instrument_type"]),
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "volume": candle["volume"],
            "oi": candle.get("oi"),
        }
        for candle in candles
    ]


def main():
    api_key = os.getenv("KITE_API_KEY") or os.getenv("API_KEY")
    access_token = os.getenv("KITE_ACCESS_TOKEN") or os.getenv("ACCESS_TOKEN")
    if not api_key or not access_token:
        sys.exit("Set KITE_API_KEY and KITE_ACCESS_TOKEN (or API_KEY and ACCESS_TOKEN).")

    # Validate the requested dates before making API calls.
    try:
        start_day = date.fromisoformat(START_DATE)
        end_day = date.fromisoformat(END_DATE)
        specific_expiry = (
            date.fromisoformat(SPECIFIC_EXPIRY_DATE)
            if EXPIRY_MODE == "specific" and SPECIFIC_EXPIRY_DATE
            else None
        )
    except ValueError as exc:
        sys.exit(f"Invalid date setting. Use YYYY-MM-DD: {exc}")

    if end_day < start_day:
        sys.exit("END_DATE must be on or after START_DATE.")
    if EXPIRY_MODE not in {"specific", "week", "all"}:
        sys.exit("EXPIRY_MODE must be one of: specific, week, all.")
    if EXPIRY_MODE == "specific" and specific_expiry is None:
        sys.exit("Specific expiry mode requires --expiry-date or SPECIFIC_EXPIRY_DATE.")
    if not OPTION_TYPES or not OPTION_TYPES.issubset({"CE", "PE"}):
        sys.exit("OPTION_TYPES must contain CE, PE, or both (for example CE,PE).")

    print(
        f"Configuration: start={start_day}, end={end_day}, "
        f"expiry_mode={EXPIRY_MODE}, specific_expiry={specific_expiry or 'n/a'}, "
        f"option_types={','.join(sorted(OPTION_TYPES))}, skip_existing={SKIP_EXISTING}"
    )
    downloaded_contracts = set()
    downloaded_contract_days = 0
    incomplete_days = []
    if EXPIRY_MODE == "specific":
        print(
            f"Requested expiry: {specific_expiry}. The script will refresh the NFO master "
            "from Kite first, then use current and archived metadata; it will not switch "
            "to a different expiry automatically."
        )

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    daily_dir = OUTPUT_DIR / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    # Always refresh and save the current NFO instrument master on every run.
    # This is how future expiry contracts are discovered; archived masters are fallback
    # metadata for dates/contracts that are no longer present in today's master.
    try:
        current_master = pd.DataFrame(kite.instruments("NFO"))
    except Exception as exc:
        sys.exit(f"Unable to download NFO instrument master from Kite: {exc}")
    if current_master.empty:
        sys.exit("Kite returned an empty NFO instrument master.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    current_master_path = ARCHIVE_DIR / f"nfo_instruments_{stamp}.csv"
    current_master.to_csv(current_master_path, index=False)
    print(f"Downloaded and saved current NFO instrument master: {current_master_path}")

    required = {
        "name", "exchange", "segment", "instrument_type", "tradingsymbol",
        "instrument_token", "expiry", "strike"
    }
    missing = required - set(current_master.columns)
    if missing:
        sys.exit(f"Instrument master missing columns: {sorted(missing)}")

    # Combine current and archived instrument masters. Older masters can help resolve
    # expired contracts; they do not guarantee Kite will return their historical candles.
    master_frames = [current_master]
    if ARCHIVE_DIR.exists():
        for archived_path in sorted(ARCHIVE_DIR.glob("nfo_instruments_*.csv")):
            if archived_path.resolve() == current_master_path.resolve():
                continue
            try:
                archived = pd.read_csv(archived_path)
                if required.issubset(archived.columns):
                    master_frames.append(archived)
            except Exception as exc:
                print(f"WARNING: Could not read archived instrument master {archived_path}: {exc}")

    master = pd.concat(master_frames, ignore_index=True)
    master["expiry"] = pd.to_datetime(master["expiry"], errors="coerce").dt.date
    master["instrument_token"] = pd.to_numeric(master["instrument_token"], errors="coerce")
    master["strike"] = pd.to_numeric(master["strike"], errors="coerce")
    for column in ("name", "exchange", "segment", "instrument_type", "tradingsymbol"):
        master[column] = master[column].astype(str).str.upper()

    # Filter strictly to NIFTY option contracts, and requested CE/PE types.
    nifty = master[
        (master["exchange"] == "NFO")
        & (master["name"] == "NIFTY")
        & (master["segment"].str.contains("OPT", na=False))
        & (master["instrument_type"].isin(OPTION_TYPES))
        & master["expiry"].notna()
        & master["instrument_token"].notna()
        & master["strike"].notna()
    ].copy()

    if nifty.empty:
        sys.exit("No matching NIFTY option contracts found in current or archived instrument masters.")

    # Deduplicate contract records, preferring the newest occurrence from the concatenated
    # data (current master is first; duplicate resolution below keeps the last record).
    nifty = nifty.drop_duplicates(
        subset=["expiry", "tradingsymbol", "instrument_token"], keep="last"
    )
    nifty["expiry_month"] = nifty["expiry"].map(lambda d: (d.year, d.month))
    monthly_dates = set(nifty.groupby("expiry_month")["expiry"].max().tolist())
    weekly = nifty[~nifty["expiry"].isin(monthly_dates)].copy()

    if EXPIRY_MODE == "specific":
        # Honor the exact user-selected expiry, whether weekly or monthly.
        # Never silently substitute another expiry date.
        expiry_dates = [specific_expiry]
    elif EXPIRY_MODE == "all":
        # Include weekly expiries that overlap the requested trading-date interval.
        expiry_dates = [
            d for d in sorted(weekly["expiry"].unique().tolist())
            if d >= start_day and d <= end_day + timedelta(days=7)
        ]
    else:  # "week": choose the next serial weekly expiry after the start date.
        monday = start_day - timedelta(days=start_day.weekday())
        week_end = monday + timedelta(days=6)
        matching = sorted(
            d for d in weekly["expiry"].unique().tolist()
            if start_day < d <= week_end
        )
        if not matching:
            matching = sorted(d for d in weekly["expiry"].unique().tolist() if d > start_day)
        if not matching:
            sys.exit("No weekly expiry found for the selected start-date week.")
        expiry_dates = [matching[0]]

    expiry_dates = list(dict.fromkeys(expiry_dates))
    if not expiry_dates:
        sys.exit("No weekly expiry dates found for the selected settings and available metadata.")

    current_expiries = set(
        pd.to_datetime(current_master["expiry"], errors="coerce").dt.date.dropna().tolist()
    )
    for expiry in expiry_dates:
        expiry_source = nifty if EXPIRY_MODE == "specific" else weekly
        available_expiries = set(expiry_source["expiry"].tolist())
        if expiry not in available_expiries:
            if EXPIRY_MODE == "specific":
                week_start = expiry - timedelta(days=expiry.weekday())
                week_end = week_start + timedelta(days=6)
                weekly_candidates = sorted(
                    d for d in weekly["expiry"].unique().tolist()
                    if week_start <= d <= week_end
                )
                if weekly_candidates:
                    candidates = ", ".join(d.isoformat() for d in weekly_candidates)
                    print(
                        f"Weekly expiry candidate(s) for the week of {expiry}: {candidates}. "
                        "Update SPECIFIC_EXPIRY_DATE or use --expiry-date to select one."
                    )
            if expiry in current_expiries:
                print(
                    f"WARNING: Expiry {expiry} exists in the current NFO master, "
                    "but no matching NIFTY CE/PE contracts survived the filters. "
                    "Check instrument type, segment, and symbol fields."
                )
            else:
                print(
                    f"WARNING: Expiry {expiry} is not present in the freshly downloaded "
                    "NFO master or any archived instrument master. The current NFO master "
                    "was saved for inspection; no substitute expiry will be selected."
                )
            continue

        selected = expiry_source[expiry_source["expiry"] == expiry].copy()
        if selected.empty:
            print(f"WARNING: No NIFTY CE/PE contracts found for expiry {expiry}; skipping.")
            continue

        selected = selected.drop_duplicates(
            subset=["instrument_token", "tradingsymbol"], keep="last"
        )
        print(f"Selected expiry: {expiry}; contracts: {len(selected)}")
        manifest = OUTPUT_DIR / f"nifty_weekly_contracts_{expiry.isoformat()}.csv"
        selected.drop(columns=["expiry_month"], errors="ignore").to_csv(manifest, index=False)
        print(f"Saved contract manifest: {manifest}")

        # Only request candles for trading dates in the configured inclusive date range.
        # Do not silently create empty files; missing/expired contracts can be retried.
        last_requested_day = min(end_day, expiry)
        first_requested_day = start_day
        if last_requested_day < first_requested_day:
            print(f"No requested dates occur on/before expiry {expiry}; skipping candle requests.")
            continue

        for offset in range((last_requested_day - first_requested_day).days + 1):
            day = first_requested_day + timedelta(days=offset)
            if not is_weekday(day):
                continue
            if should_skip_current_date(day):
                print(
                    f"SKIP current date {day}: market is still open "
                    f"(download after {MARKET_CLOSE_TIME.strftime('%H:%M')} IST)."
                )
                continue

            day_file = daily_dir / f"nifty_weekly_{expiry.isoformat()}_{day.isoformat()}.csv"
            # An errors file marks the day incomplete: it lists the contracts whose
            # requests failed, and the next run fetches just those and merges them in.
            error_path = daily_dir / f"errors_{expiry.isoformat()}_{day.isoformat()}.txt"
            existing = day_file.exists() and day_file.stat().st_size > 0
            retrying = SKIP_EXISTING and existing
            to_fetch = selected
            if retrying:
                if not error_path.exists():
                    print(f"SKIP existing file: {day_file.name}")
                    continue
                failed = failed_tokens_from(error_path)
                to_fetch = selected[selected["instrument_token"].astype(int).isin(failed)]
                if to_fetch.empty:
                    print(
                        f"SKIP {day_file.name}: {error_path.name} names no contract from this "
                        "expiry's manifest; review it, and delete it once checked."
                    )
                    continue
                print(f"RETRY {len(to_fetch)} failed contract(s) for incomplete file: {day_file.name}")
            elif existing:
                print(f"Re-downloading existing file: {day_file.name}")

            rows = []
            day_errors = []
            successful_contracts = set()

            for _, inst in to_fetch.iterrows():
                token = int(inst["instrument_token"])
                symbol = str(inst["tradingsymbol"])
                try:
                    contract_rows = fetch_contract_day(kite, inst, day, expiry)
                    if contract_rows:
                        successful_contracts.add(symbol)
                    rows.extend(contract_rows)
                except Exception as exc:
                    day_errors.append(f"{symbol} token={token}: {exc}")
                time.sleep(PAUSE)

            if rows:
                frame = pd.DataFrame(rows)
                if retrying:
                    frame = pd.concat([pd.read_csv(day_file), frame], ignore_index=True)
                    frame = frame.drop_duplicates(subset=["timestamp", "tradingsymbol"], keep="last")
                frame = frame.sort_values(["timestamp", "strike", "option_type"])
                frame.to_csv(day_file, index=False)
                downloaded_contracts.update(successful_contracts)
                downloaded_contract_days += len(successful_contracts)
                action = "Added" if retrying else "Saved"
                print(
                    f"{action} {len(rows):,} candles from {len(successful_contracts):,} contracts: "
                    f"{day_file.resolve()}"
                )
            elif not retrying:
                print(
                    f"No candles returned for {day} / expiry {expiry}; "
                    "no daily file created, so a later run can retry."
                )

            if not day_errors:
                if error_path.exists():
                    error_path.unlink()
                    print(f"All contracts fetched; {day_file.name} is now complete.")
            else:
                incomplete_days.append(day_file.name)
                error_path.write_text("\n".join(day_errors), encoding="utf-8")
                print(
                    f"{len(day_errors)} contract request errors saved to {error_path}; "
                    "the day stays incomplete and the next run retries them."
                )
                if any("expired" in err.lower() or "invalid" in err.lower() for err in day_errors):
                    print(
                        "NOTE: Kite may reject historical requests for expired option contracts. "
                        "Archived instrument metadata helps identify symbols but does not guarantee candle availability."
                    )

        # Consolidated merged archives are intentionally disabled.
        # Daily CSV files are the only candle output; existing daily files remain untouched.

    print(
        f"Done. Contracts downloaded: {len(downloaded_contracts):,} unique "
        f"({downloaded_contract_days:,} contract-day downloads). "
        "Complete daily CSVs are skipped when SKIP_EXISTING=true; incomplete ones are retried."
    )
    if incomplete_days:
        print(
            f"INCOMPLETE ({len(incomplete_days)}): {', '.join(incomplete_days)}. "
            "Run the download again to retry the failed contracts."
        )


if __name__ == "__main__":
    main()
