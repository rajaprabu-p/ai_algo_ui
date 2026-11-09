"""unittest entry point for the backtest snapshot check.

    venv\\Scripts\\python.exe -m unittest discover -s scripts\\tests -v

Takes about 2 minutes (48 backtest runs). For details on failures, or to
regenerate golden files, use snapshot.py directly.
"""

import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import snapshot  # noqa: E402


class BacktestSnapshotTest(unittest.TestCase):
    def test_trades_match_golden(self):
        dates = snapshot.available_dates()
        if not dates:
            self.skipTest("no days with both spot fixtures and downloaded option files")
        results, root = snapshot.run_all(sorted(snapshot.VARIANTS), dates, workers=6)
        problems = [f"{r.variant} {r.trade_date}: {p}" for r in results if (p := snapshot.compare(r))]
        if problems:
            self.fail(f"{len(problems)} snapshot(s) changed; workspace kept at {root}\n" + "\n".join(problems))
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
