"""
Consolidates all backtest Excel logs from the logs/ folder into a single
summary Excel file: logs/consolidated_backtest.xlsx

Sheets produced:
  - all_trades      : every trade row from every log file
  - daily_summary   : one row per source file (date, trades, P&L, etc.)
  - overall_summary : aggregate stats across all files

Usage:
    python consolidate_backtest_logs.py
    python consolidate_backtest_logs.py --files backtest_19062026.xlsx backtest_22062026.xlsx
"""

import argparse
import os
import re
import sys
import glob
import pandas as pd
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_LOGS_FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOGS_FOLDER    = os.path.abspath(os.environ.get("STRATEGY_LOG_DIR", DEFAULT_LOGS_FOLDER).strip() or DEFAULT_LOGS_FOLDER)
OUTPUT_FILE    = os.path.join(LOGS_FOLDER, "consolidated_backtest.xlsx")  # overridden at runtime
FILE_PATTERNS  = ["backtest_*.xlsx", "papertrade_*.xlsx"]  # backtest and papertrade files
LOGS_SUBFOLDER = ""                         # optional subfolder inside LOGS_FOLDER

EXCLUDED_SUMMARY_METRICS = {
    "run_mode",
    "script_name",
    "mtm_guard_enabled",
    "trade_stop_basis",
    "tracking_start",
    "no_new_entry_after",
    "trailing_profit_stages",
    "breakeven_trigger",
    "trail_trigger",
    "trail_pct",
    "daily_profit_target_rs",
    "daily_loss_limit_rs",
    "max_trades",
    "max_concurrent_trades",
    "max_concurrent_trades_ce",
    "max_concurrent_trades_pe",
    "lots",
    "lot_size",
    "qty",
    "losing_or_flat_trades",
    "auto_select_spot_ref",
    "notes",
}

ALL_TRADES_DROP_COLUMNS = {
    "gap_type",
    "gap_points",
    "gap_pct",
    "is_gap_up",
    "cpr_pivot",
    "cpr_bc",
    "cpr_tc",
    "cpr_width",
    "cpr_width_pct",
    "cpr_type",
}
# ──────────────────────────────────────────────────────────────────────────────


def load_excel_trades(path: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (trades_df, summary_df) from one backtest Excel file."""
    try:
        xf = pd.ExcelFile(path, engine="openpyxl")
    except Exception as exc:
        print(f"  [SKIP] Cannot open {os.path.basename(path)}: {exc}")
        return pd.DataFrame(), pd.DataFrame()

    trades = pd.DataFrame()
    summary = pd.DataFrame()

    if "trades" in xf.sheet_names:
        try:
            trades = xf.parse("trades")
        except Exception as exc:
            print(f"  [WARN] Could not read 'trades' sheet in {os.path.basename(path)}: {exc}")

    if "summary" in xf.sheet_names:
        try:
            summary = xf.parse("summary")
        except Exception as exc:
            print(f"  [WARN] Could not read 'summary' sheet in {os.path.basename(path)}: {exc}")

    return trades, summary


def extract_chart_bytes(path: str):
    """Extract raw PNG bytes from the 'chart' sheet of a backtest Excel file.
    Returns bytes or None."""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=False, data_only=True)
        if "chart" not in wb.sheetnames:
            return None
        ws = wb["chart"]
        images = getattr(ws, '_images', [])
        if not images:
            return None
        return images[0]._data()
    except Exception as exc:
        print(f"  [WARN] Could not extract chart from {os.path.basename(path)}: {exc}")
        return None


def summary_to_dict(summary_df: pd.DataFrame) -> dict:
    """Convert the metric/value summary sheet into a plain dict."""
    if summary_df.empty or "metric" not in summary_df.columns:
        return {}
    return dict(zip(summary_df["metric"].astype(str), summary_df["value"]))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Consolidate backtest Excel logs into one workbook."
    )
    parser.add_argument(
        "--files",
        nargs="+",
        default=None,
        help=(
            "Explicit list of files to process. Accepts full paths or file names "
            "inside logs/. You can also pass comma-separated values."
        ),
    )
    return parser.parse_args()


def _expand_explicit_files(raw_inputs: list[str], logs_root: str) -> list[str]:
    """Resolve explicit file inputs into concrete Excel file paths."""
    requested = []
    for item in raw_inputs:
        parts = [p.strip() for p in str(item).split(",") if p.strip()]
        requested.extend(parts)

    resolved = []
    missing = []

    for item in requested:
        candidates = []
        # Keep absolute paths as-is; for relative names also try under logs_root.
        if os.path.isabs(item):
            candidates.append(item)
        else:
            candidates.append(item)
            candidates.append(os.path.join(logs_root, item))

        matched = None
        for candidate in candidates:
            if os.path.isfile(candidate):
                matched = os.path.abspath(candidate)
                break

        if matched is None:
            missing.append(item)
        elif os.path.basename(matched).startswith("consolidated_"):
            print(f"  [SKIP] Ignoring consolidated output file: {os.path.basename(matched)}")
        else:
            resolved.append(matched)

    if missing:
        print("  [WARN] Some explicit files were not found:")
        for item in missing:
            print(f"    - {item}")

    # De-duplicate while preserving order.
    seen = set()
    uniq = []
    for path in resolved:
        if path not in seen:
            seen.add(path)
            uniq.append(path)
    return uniq


def _extract_date_and_suffix_from_backtest_name(filename: str) -> tuple[str, str]:
    """Extract (ddmmyyyy, suffix_after_date) from backtest or papertrade file name if possible."""
    name_no_ext = os.path.splitext(os.path.basename(filename))[0]
    rest = ""
    for prefix in ("backtest_", "papertrade_"):
        if name_no_ext.startswith(prefix):
            rest = name_no_ext[len(prefix):]
            break
    if not rest:
        return "", ""

    if len(rest) < 8:
        return "", ""

    date_token = rest[:8]
    if not date_token.isdigit():
        return "", ""

    suffix = rest[8:]
    if suffix.startswith("_"):
        suffix = suffix[1:]

    return date_token, suffix


def _common_suffix_across_files(files: list[str]) -> str:
    """Return common suffix token after date across all parseable files."""
    suffixes = []
    for path in files:
        _, suffix = _extract_date_and_suffix_from_backtest_name(os.path.basename(path))
        if suffix:
            suffixes.append(suffix)

    if not suffixes:
        return ""

    first = suffixes[0]
    if all(s == first for s in suffixes):
        return first
    return ""


def _detect_strategy_version(files: list[str]) -> str:
    """Return 'v28', 'v30', or '' based on a common version token in all filenames."""
    versions = set()
    pattern = re.compile(r'(?:^|[_-])(v\d+)(?:[_-]|$)', re.IGNORECASE)
    for path in files:
        name = os.path.splitext(os.path.basename(path))[0]
        matches = pattern.findall(name)
        if matches:
            versions.update(m.lower() for m in matches)
    if len(versions) == 1:
        return versions.pop()
    return ""


def main():
    args = parse_args()
    logs_root = os.path.join(LOGS_FOLDER, LOGS_SUBFOLDER) if str(LOGS_SUBFOLDER).strip() else LOGS_FOLDER
    if args.files:
        files = _expand_explicit_files(args.files, logs_root)
        print("Using explicit file list from command line.\n")
    else:
        seen = set()
        files = []
        for pattern_glob in FILE_PATTERNS:
            for f in glob.glob(os.path.join(logs_root, pattern_glob)):
                if f not in seen:
                    seen.add(f)
                    files.append(f)
        files = sorted(files)
        # Exclude consolidated output files
        files = [f for f in files if not os.path.basename(f).startswith("consolidated_")]

    if not files:
        if args.files:
            print("No valid backtest/papertrade files found from explicit --files input.")
        else:
            print(f"No backtest/papertrade files found in: {logs_root}")
        sys.exit(0)

    print(f"Found {len(files)} file(s) in {logs_root}\n")

    all_trades_list    = []
    daily_summary_rows = []
    chart_entries      = []  # list of (trade_date, sheet_name, XlImage)

    for path in files:
        filename = os.path.basename(path)
        print(f"Reading: {filename}")
        trades_df, summary_df = load_excel_trades(path)
        summary_d = summary_to_dict(summary_df)

        # Extract chart image if present
        chart_img = extract_chart_bytes(path)

        # Attach source filename to every trade row
        if not trades_df.empty:
            trades_df = trades_df.copy()
            trades_df.insert(0, "source_file", filename)
            all_trades_list.append(trades_df)

        # Build one daily-summary row from the summary sheet + trades data
        total_trades  = int(float(summary_d.get("total_trades", 0) or 0))
        winning       = int(float(summary_d.get("winning_trades", 0) or 0))
        losing        = int(float(summary_d.get("losing_or_flat_trades", 0) or 0))
        total_pts     = float(summary_d.get("total_pnl_points", 0) or 0)
        total_rs      = float(summary_d.get("total_pnl_rs", 0) or 0)
        guard_reason  = str(summary_d.get("guard_stop_reason", "") or "")
        nifty_prev_close = summary_d.get("nifty_prev_close", "")
        nifty_open    = summary_d.get("nifty_open", "")
        nifty_close   = summary_d.get("nifty_close", "")
        nifty_high    = summary_d.get("nifty_high", "")
        nifty_low     = summary_d.get("nifty_low", "")
        gap_type      = summary_d.get("gap_type", "")
        gap_points    = summary_d.get("gap_points", "")
        gap_pct       = summary_d.get("gap_pct", "")
        cpr_pivot     = summary_d.get("cpr_pivot", "")
        cpr_bc        = summary_d.get("cpr_bc", "")
        cpr_tc        = summary_d.get("cpr_tc", "")
        cpr_width     = summary_d.get("cpr_width", "")
        cpr_width_pct = summary_d.get("cpr_width_pct", "")
        cpr_type      = summary_d.get("cpr_type", "")
        nifty_change  = str(summary_d.get("nifty_change", "") or "")

        # Derive trade_date from the trades sheet if present, else from filename
        trade_date = ""
        if not trades_df.empty and "trade_date" in trades_df.columns:
            dates = trades_df["trade_date"].dropna().astype(str).unique()
            trade_date = ", ".join(sorted(dates))
        if not trade_date:
            # Try to extract date from filename like backtest_12062026_... or papertrade_12062026_...
            date_token, _ = _extract_date_and_suffix_from_backtest_name(filename)
            if date_token:
                try:
                    trade_date = datetime.strptime(date_token, "%d%m%Y").strftime("%Y-%m-%d")
                except ValueError:
                    trade_date = date_token

        daily_row = {
            "source_file"     : filename,
            "trade_date"      : trade_date,
            "nifty_prev_close": nifty_prev_close,
            "nifty_open"      : nifty_open,
            "nifty_high"      : nifty_high,
            "nifty_low"       : nifty_low,
            "nifty_close"     : nifty_close,
            "gap_points"      : gap_points,
            "gap_pct"         : gap_pct,
            "gap_type"        : gap_type,
            "cpr_pivot"       : cpr_pivot,
            "cpr_bc"          : cpr_bc,
            "cpr_tc"          : cpr_tc,
            "cpr_width"       : cpr_width,
            "cpr_width_pct"   : cpr_width_pct,
            "cpr_type"        : cpr_type,
            "nifty_change"    : nifty_change,
            "total_trades"    : total_trades,
            "winning_trades"  : winning,
            "losing_trades"   : losing,
            "win_rate_pct"    : round(100 * winning / total_trades, 1) if total_trades > 0 else 0.0,
            "total_pnl_points": round(total_pts, 2),
            "total_pnl_rs"    : round(total_rs, 2),
            "guard_stop_reason": guard_reason,
        }

        # Include all extra summary metrics from source Sheet1/summary.
        for metric, value in summary_d.items():
            metric_name = str(metric).strip()
            if metric_name in EXCLUDED_SUMMARY_METRICS:
                continue
            if metric_name and metric_name not in daily_row:
                daily_row[metric_name] = value

        daily_summary_rows.append(daily_row)

        # Store chart bytes for combined sheet
        if chart_img is not None:
            chart_entries.append((trade_date or filename, chart_img))

    # ── Combine all trades ───────────────────────────────────────────────────
    all_trades = pd.concat(all_trades_list, ignore_index=True) if all_trades_list else pd.DataFrame()

    # Keep all_trades focused on trade execution fields; remove market-context CPR/gap columns.
    if not all_trades.empty:
        drop_cols = [c for c in all_trades.columns if c in ALL_TRADES_DROP_COLUMNS]
        if drop_cols:
            all_trades = all_trades.drop(columns=drop_cols)

    # Strip timezone info for Excel compatibility
    for df in [all_trades]:
        for col in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[col]):
                if df[col].dt.tz is not None:
                    df[col] = df[col].dt.tz_localize(None)

    # ── Determine output file prefix based on file types ───────────────────
    basenames = [os.path.basename(f) for f in files]
    has_backtest   = any(n.startswith("backtest_")   for n in basenames)
    has_papertrade = any(n.startswith("papertrade_") for n in basenames)
    strategy_ver   = _detect_strategy_version(files)   # e.g. 'v28', 'v30', or ''
    if has_backtest and has_papertrade:
        type_part = "consolidated_backtest_papertrade"
    elif has_papertrade:
        type_part = "consolidated_papertrade"
    else:
        type_part = "consolidated_backtest"
    out_prefix = type_part
    ver_suffix = f"_{strategy_ver}" if strategy_ver else ""

    # ── Build output filename with date range (+ common suffix if present) ──
    filename_dates = []
    for path in files:
        date_token, _ = _extract_date_and_suffix_from_backtest_name(os.path.basename(path))
        if date_token:
            filename_dates.append(date_token)

    if filename_dates:
        try:
            dt_values = [datetime.strptime(d, "%d%m%Y") for d in filename_dates]
            from_date = min(dt_values).strftime("%d%m%Y")
            to_date = max(dt_values).strftime("%d%m%Y")
        except Exception:
            from_date = min(filename_dates)
            to_date = max(filename_dates)

        suffix = _common_suffix_across_files(files)
        if suffix:
            fname = f"{out_prefix}_{from_date}_to_{to_date}{ver_suffix}_{suffix}.xlsx"
        else:
            fname = f"{out_prefix}_{from_date}_to_{to_date}{ver_suffix}.xlsx"
    else:
        all_dates = sorted(
            d["trade_date"] for d in daily_summary_rows
            if d.get("trade_date") and str(d["trade_date"]).strip()
        )
        if all_dates:
            try:
                dt_values = [datetime.strptime(d, "%Y-%m-%d") for d in all_dates]
                from_date = min(dt_values).strftime("%d%m%Y")
                to_date = max(dt_values).strftime("%d%m%Y")
            except Exception:
                from_date = all_dates[0].replace("-", "")
                to_date = all_dates[-1].replace("-", "")
            fname = f"{out_prefix}_{from_date}_to_{to_date}{ver_suffix}.xlsx"
        else:
            fname = f"{out_prefix}{ver_suffix}.xlsx"
    OUTPUT_FILE = os.path.join(logs_root, fname)

    # ── Daily summary ────────────────────────────────────────────────────────
    daily_df = pd.DataFrame(daily_summary_rows)

    # ── Overall summary ──────────────────────────────────────────────────────
    n_files        = len(files)
    n_days_trades  = int((daily_df["total_trades"] > 0).sum()) if not daily_df.empty else 0
    total_trades_all  = int(daily_df["total_trades"].sum()) if not daily_df.empty else 0
    winning_all    = int(daily_df["winning_trades"].sum()) if not daily_df.empty else 0
    losing_all     = int(daily_df["losing_trades"].sum()) if not daily_df.empty else 0
    total_pts_all  = round(float(daily_df["total_pnl_points"].sum()), 2) if not daily_df.empty else 0.0
    total_rs_all   = round(float(daily_df["total_pnl_rs"].sum()), 2) if not daily_df.empty else 0.0
    avg_daily_rs   = round(total_rs_all / n_days_trades, 2) if n_days_trades > 0 else 0.0
    win_rate_all   = round(100 * winning_all / total_trades_all, 1) if total_trades_all > 0 else 0.0

    overall_rows = [
        {"metric": "generated_at",        "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        {"metric": "source_folder",       "value": logs_root},
        {"metric": "files_processed",     "value": n_files},
        {"metric": "days_with_trades",    "value": n_days_trades},
        {"metric": "total_trades",        "value": total_trades_all},
        {"metric": "winning_trades",      "value": winning_all},
        {"metric": "losing_trades",       "value": losing_all},
        {"metric": "win_rate_pct",        "value": win_rate_all},
        {"metric": "total_pnl_points",    "value": total_pts_all},
        {"metric": "total_pnl_rs",        "value": total_rs_all},
        {"metric": "avg_daily_pnl_rs",    "value": avg_daily_rs},
    ]
    overall_df = pd.DataFrame(overall_rows)

    # ── Write output ─────────────────────────────────────────────────────────
    print(f"\nWriting consolidated log to: {OUTPUT_FILE}")
    with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:
        overall_df.to_excel(writer, sheet_name="overall_summary", index=False)
        daily_df.to_excel(writer, sheet_name="daily_summary", index=False)
        if not all_trades.empty:
            all_trades.to_excel(writer, sheet_name="all_trades", index=False)
        else:
            pd.DataFrame(columns=["source_file", "trade_date"]).to_excel(
                writer, sheet_name="all_trades", index=False
            )

        # ── Single combined charts sheet ─────────────────────────────────────
        if chart_entries:
            try:
                import io as _io
                from openpyxl.drawing.image import Image as XlImage
                from openpyxl.styles import Font, Alignment

                def _img_pixel_height(raw_bytes):
                    try:
                        from PIL import Image as PILImage
                        return PILImage.open(_io.BytesIO(raw_bytes)).size[1]
                    except Exception:
                        return 900  # safe fallback

                # Excel default row height ≈ 15 pt = 20 px at 96 DPI
                PX_PER_ROW = 20
                LABEL_ROWS = 2   # rows for the date label before each image
                PAD_ROWS   = 3   # blank rows after each image

                charts_ws = writer.book.create_sheet("charts")
                current_row = 1

                for date_label, raw_bytes in chart_entries:
                    # Date label
                    cell = charts_ws.cell(row=current_row, column=1, value=date_label)
                    cell.font = Font(bold=True, size=12)
                    cell.alignment = Alignment(horizontal="left")
                    current_row += LABEL_ROWS

                    # Place image
                    img = XlImage(_io.BytesIO(raw_bytes))
                    img.anchor = f"A{current_row}"
                    charts_ws.add_image(img)

                    # Advance past image
                    h_px = _img_pixel_height(raw_bytes)
                    rows_needed = max(1, int(h_px / PX_PER_ROW)) + PAD_ROWS
                    current_row += rows_needed

                print(f"  Embedded {len(chart_entries)} chart(s) in 'charts' sheet.")
            except Exception as exc:
                print(f"  [WARN] Could not build combined charts sheet: {exc}")

    # ── Print summary to console ─────────────────────────────────────────────
    print("\n========== CONSOLIDATED BACKTEST SUMMARY ==========")
    print(f"Files processed     : {n_files}")
    print(f"Days with trades    : {n_days_trades}")
    print(f"Total trades        : {total_trades_all}")
    print(f"Winning trades      : {winning_all}")
    print(f"Losing trades       : {losing_all}")
    print(f"Win rate            : {win_rate_all:.1f}%")
    print(f"Total P&L (points)  : {total_pts_all:.2f}")
    print(f"Total P&L (Rs)      : {total_rs_all:.2f}")
    print(f"Avg daily P&L (Rs)  : {avg_daily_rs:.2f}")
    print(f"\nOutput              : {OUTPUT_FILE}")

    if not daily_df.empty:
        print("\n---------- Per-file breakdown ----------")
        print(daily_df[["source_file", "trade_date", "nifty_prev_close", "nifty_open", "nifty_high", "nifty_low",
                         "nifty_close", "gap_points", "gap_pct", "gap_type",
                         "cpr_pivot", "cpr_bc", "cpr_tc", "cpr_width", "cpr_width_pct", "cpr_type",
                         "nifty_change", "total_trades", "winning_trades",
                         "total_pnl_rs", "guard_stop_reason"]].to_string(index=False))


if __name__ == "__main__":
    main()
