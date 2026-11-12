"""run_backtest_range.py: per-day result parsing and the range summary printed at the end of the log.

    venv\\Scripts\\python.exe -m unittest discover -s scripts\\tests -p "test_backtest_range.py" -v
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_backtest_range as rbr  # noqa: E402


class ParseDayLineTests(unittest.TestCase):
    def test_result_lines(self):
        self.assertEqual(rbr.parse_day_line("Total trades       : 2\n"), ("trades", 2))
        self.assertEqual(rbr.parse_day_line("Net P&L (Rs)       : -1255.79\n"), ("net", -1255.79))
        self.assertEqual(rbr.parse_day_line("Net P&L (Rs)       : 1181.18"), ("net", 1181.18))

    def test_other_lines_ignored(self):
        for line in ("Total P&L (Rs)     : 1339.00", "Entered position at ...", "Net P&L (Rs) : n/a", ""):
            self.assertIsNone(rbr.parse_day_line(line))


class RangeSummaryTests(unittest.TestCase):
    def test_summary_block(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rbr.print_range_summary([("2026-11-04", 2, -1255.79), ("2026-11-05", 2, -1244.59), ("2026-11-06", 2, 1517.95)], 3)
        text = out.getvalue()
        self.assertIn("[RANGE DAY] 2026-11-06 trades=2 net_rs=1517.95", text)
        self.assertTrue(text.rstrip().endswith(
            "[RANGE SUMMARY] days=3/3 trades=6 net_rs=-982.43 winning_days=1 losing_days=2"))

    def test_failed_days_are_not_counted(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rbr.print_range_summary([("2026-11-04", 1, 500.0)], 2)   # one of two days failed
        self.assertIn("[RANGE SUMMARY] days=1/2 trades=1 net_rs=500.00", out.getvalue())


if __name__ == "__main__":
    unittest.main()
