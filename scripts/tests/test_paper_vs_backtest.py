"""Tests for scripts/paper_vs_backtest.py (pairing / comparison logic and paper file discovery).

    venv\\Scripts\\python.exe -m unittest discover -s scripts\\tests -p "test_paper_vs_backtest.py" -v
"""

import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import paper_vs_backtest as pvb  # noqa: E402


def trade(side, entry, exit_, entry_price, exit_price, reason, symbol="NIFTY26O2123000CE"):
    return {"side": side, "option_symbol": symbol, "entry_time": pd.Timestamp(f"2026-10-21 {entry}"),
            "exit_time": pd.Timestamp(f"2026-10-21 {exit_}"), "entry_price": entry_price,
            "exit_price": exit_price, "pnl_points": exit_price - entry_price, "exit_reason": reason}


class CompareTests(unittest.TestCase):
    def test_match_after_removing_backtest_slippage(self):
        paper = pd.DataFrame([trade("CE", "09:25", "09:40", 150.0, 175.0, "TRAILING_STOP_HIT")])
        bt = pd.DataFrame([trade("CE", "09:25", "09:40", 151.0, 174.0, "TRAILING_STOP_HIT")])  # +1 / -1 slippage
        r = pvb.compare(paper, bt, entry_slip=1.0, exit_slip=1.0)
        self.assertEqual(r["verdict"], "MATCH", r["issues"])
        self.assertEqual((r["pairs"][0]["entry_price_diff"], r["pairs"][0]["exit_price_diff"]), (0.0, 0.0))

    def test_entry_within_tolerance_still_pairs(self):
        paper = pd.DataFrame([trade("PE", "09:27", "09:50", 120.0, 95.0, "EMERGENCY_SL_HIT", "NIFTY26O2123000PE")])
        bt = pd.DataFrame([trade("PE", "09:25", "09:49", 120.5, 95.0, "EMERGENCY_SL_HIT", "NIFTY26O2123000PE")])
        r = pvb.compare(paper, bt)
        self.assertEqual(r["verdict"], "MATCH", r["issues"])
        self.assertEqual(r["pairs"][0]["entry_minutes_apart"], 2.0)
        self.assertEqual(r["drifts"], ["PE 09:27: entry 09:27 vs backtest 09:25"])

    def test_fill_drifts_do_not_change_the_verdict(self):
        # 13 Nov v30: same trade, paper exited 2 min early at 199.20; backtest 200.00 after 1 pt slippage.
        paper = pd.DataFrame([trade("CE", "09:41", "09:56", 185.0, 199.2, "TRAILING_STOP_HIT")])
        bt = pd.DataFrame([trade("CE", "09:41", "09:58", 186.0, 200.0, "TRAILING_STOP_HIT")])
        r = pvb.compare(paper, bt, entry_slip=1.0, exit_slip=1.0)
        self.assertEqual(r["verdict"], "MATCH", r["issues"])
        self.assertEqual(r["drifts"], ["CE 09:41: exit 09:56 vs backtest 09:58"])   # -1.80 is within 2.5
        r = pvb.compare(paper.assign(exit_price=193.15), bt, entry_slip=1.0, exit_slip=1.0)
        self.assertIn("CE 09:41: exit price -7.85 pts", r["drifts"])
        self.assertEqual(r["max_price_drift"], 7.85)

    def test_differences_are_reported(self):
        paper = pd.DataFrame([trade("CE", "09:25", "09:40", 150.0, 175.0, "TRAILING_STOP_HIT")])
        bt = pd.DataFrame([trade("CE", "09:26", "10:05", 150.0, 125.0, "EMERGENCY_SL_HIT", "NIFTY26O2123050CE")])
        r = pvb.compare(paper, bt)
        self.assertEqual(r["verdict"], "DIFFERENT")
        text = " | ".join(r["issues"])
        for expected in ("different contract", "exit reason differs"):
            self.assertIn(expected, text)
        drifts = " | ".join(r["drifts"])
        for expected in ("exit price +50.00 pts", "exit 09:40 vs backtest 10:05"):
            self.assertIn(expected, drifts)

    def test_unmatched_trades_on_either_side(self):
        paper = pd.DataFrame([trade("CE", "09:25", "09:40", 150.0, 175.0, "TRAILING_STOP_HIT")])
        bt = pd.DataFrame([trade("PE", "09:25", "09:40", 140.0, 115.0, "EMERGENCY_SL_HIT", "NIFTY26O2123000PE"),
                           trade("CE", "10:30", "10:45", 160.0, 170.0, "TRAILING_STOP_HIT")])
        r = pvb.compare(paper, bt)
        self.assertEqual(r["verdict"], "DIFFERENT")
        self.assertEqual(len(r["only_paper"]), 1)       # CE 09:25 (backtest's CE is at 10:30, too far)
        self.assertEqual(len(r["only_backtest"]), 2)

    def test_no_trades_and_no_paper(self):
        empty = pd.DataFrame()
        self.assertEqual(pvb.compare(empty, empty)["verdict"], "NO_TRADES")
        bt = pd.DataFrame([trade("CE", "09:25", "09:40", 150.0, 175.0, "TRAILING_STOP_HIT")])
        self.assertEqual(pvb.compare(empty, bt)["verdict"], "NO_PAPER_TRADES")

    def test_settings_tags(self):
        self.assertEqual(pvb.settings_tag_from_name("papertrade_21102026_102400_mtf5min_max2_sl25_v28.xlsx"), "mtf5min_max2_sl25")
        self.assertEqual(pvb.settings_tag_from_name("backtest_21102026_mtf5min_mfe_max2_sl25_v28_csv.xlsx"), "mtf5min_mfe_max2_sl25")
        self.assertEqual(
            pvb.settings_mismatches(["papertrade_21102026_102400_mtf5min_max2_sl25_v28.xlsx",
                                     "papertrade_21102026_110000_mtf5min_mfe_max2_sl25_v28.xlsx"],
                                    "backtest_21102026_mtf5min_mfe_max2_sl25_v28_csv.xlsx"),
            ["papertrade_21102026_102400_mtf5min_max2_sl25_v28.xlsx"])

    def test_slippage_parsed_from_summary(self):
        self.assertEqual(pvb.backtest_slippage({"backtest_slippage_points": "entry 1 / exit 1.5"}), (1.0, 1.5))
        self.assertEqual(pvb.backtest_slippage({"backtest_slippage_points": "off"}), (0.0, 0.0))
        self.assertEqual(pvb.backtest_slippage({}), (0.0, 0.0))


class PaperFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pvb_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, rows):
        with pd.ExcelWriter(self.tmp / name) as xw:
            pd.DataFrame([{"metric": "run_mode", "value": "PAPERTRADE_LIVE"}]).to_excel(xw, sheet_name="summary", index=False)
            pd.DataFrame(rows).to_excel(xw, sheet_name="live_trade_events", index=False)

    def test_merges_restarted_sessions_and_filters_variant_and_day(self):
        a = trade("CE", "09:25", "09:40", 150.0, 175.0, "TRAILING_STOP_HIT")
        b = trade("PE", "10:05", "10:20", 130.0, 105.0, "EMERGENCY_SL_HIT", "NIFTY26O2123000PE")
        self.write("papertrade_21102026_091500_mtf5min_max2_sl25_v28.xlsx", [a])
        self.write("papertrade_21102026_100000_mtf5min_max2_sl25_v28.xlsx", [a, b])  # restart repeats trade a
        self.write("papertrade_21102026_091500_mtf5min_max2_sl25_v30.xlsx", [b])       # other variant
        self.write("papertrade_22102026_091500_mtf5min_max2_sl25_v28.xlsx", [b])       # other day
        trades, files = pvb.load_paper_trades(self.tmp, date(2026, 10, 21), "v28")
        self.assertEqual(len(files), 2)
        self.assertEqual(list(trades["side"]), ["CE", "PE"])  # de-duplicated, sorted by entry time

    def test_no_files(self):
        trades, files = pvb.load_paper_trades(self.tmp, date(2026, 10, 21), "v28")
        self.assertTrue(trades.empty)
        self.assertEqual(files, [])


if __name__ == "__main__":
    unittest.main()
