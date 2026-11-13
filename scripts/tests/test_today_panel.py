"""Tests for the dashboard "Today" panel (/api/today and its log parsers).

    venv\\Scripts\\python.exe -m unittest discover -s scripts\\tests -p "test_today_panel.py" -v
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import app as webapp  # noqa: E402

TODAY = "2026-11-09"
# Real lines from a paper-trading log (papertrade_09112026_112809_v30.txt), plus a later open position.
LOG = """
[ENTRY PARITY] mode=PAPERTRADE_LIVE spot_ts=2026-11-09 09:20:00+05:30 bull_score=0
[OPEN PNL] 2026-11-09 11:38:19 IST | entry_time=2026-11-09 11:28:00+05:30 | trade_count=0 | symbol=NIFTY26N1725050CE | side=CE | ltp=253.50 | entry=194.35 | qty=65 | unrealized=59.15 pts (3844.75 rs) | realized=0.00 rs | total_mtm=3844.75 rs | trailing=S2:1:25->15
Entered position at 2026-11-09 11:28:00+05:30 | logged_at=2026-11-09 11:38:27 IST | mode=PAPERTRADE_LIVE | side=CE symbol=NIFTY26N1725050CE reason=BUY_CE_MTF_BULL_S5 entry=194.35 spot=24926.00 qty=65 emergency_sl=169.35 target=65.00 trailing=stage2_hit
Exited position at 2026-11-09 11:38:27+05:30 | logged_at=2026-11-09 11:38:27 IST with reason TRAILING_STOP_HIT. symbol=NIFTY26N1725050CE side=CE entry=194.35 exit=244.35 | P&L: 3250.00 Rs (50.00 points). Cumulative MTM: 3250.00 Rs. target=65.00 trailing=stage2_hit
Entered position at 2026-11-09 11:45:00+05:30 | logged_at=2026-11-09 11:45:02 IST | mode=PAPERTRADE_LIVE | side=PE symbol=NIFTY26N1725000PE reason=BUY_PE_MTF_BEAR_S5 entry=180.00 spot=24900.00 qty=65 emergency_sl=155.00
[OPEN PNL] 2026-11-09 11:50:00 IST | entry_time=2026-11-09 11:45:00+05:30 | trade_count=2 | symbol=NIFTY26N1725000PE | side=PE | ltp=172.00 | entry=180.00 | qty=65 | unrealized=-8.00 pts (-520.00 rs) | realized=3250.00 rs | total_mtm=2730.00 rs
[OPEN PNL] 2026-11-08 15:00:00 IST | entry_time=x | trade_count=1 | symbol=OLD | side=CE | ltp=1 | entry=1 | qty=65 | unrealized=1.00 pts (65.00 rs) | realized=0.00 rs | total_mtm=65.00 rs
"""


class StrategyLogParserTests(unittest.TestCase):
    def test_trades_open_position_and_pnl(self):
        s = webapp.parse_strategy_log_today(LOG, TODAY)
        self.assertEqual(s["trade_count"], 1)
        self.assertEqual(s["trades"][0]["pnl_rs"], 3250.0)
        self.assertEqual(s["trades"][0]["reason"], "TRAILING_STOP_HIT")
        p = s["open_position"]
        self.assertEqual((p["side"], p["symbol"], p["entry"], p["ltp"]), ("PE", "NIFTY26N1725000PE", 180.0, 172.0))
        self.assertEqual((s["realized_rs"], s["unrealized_rs"], s["total_rs"]), (3250.0, -520.0, 2730.0))

    def test_closed_position_is_not_reported_open(self):
        closed_only = "\n".join(LOG.strip().splitlines()[:4])
        s = webapp.parse_strategy_log_today(closed_only, TODAY)
        self.assertIsNone(s["open_position"])
        self.assertEqual(s["total_rs"], 3250.0)

    def test_other_days_ignored(self):
        s = webapp.parse_strategy_log_today(LOG, "2026-11-10")
        self.assertEqual((s["trade_count"], s["open_position"], s["total_rs"]), (0, None, 0.0))

    def test_stopped_reason(self):
        s = webapp.parse_strategy_log_today(LOG + "\n[2026-11-09] Trading stopped by: MAX_TRADES_REACHED\n", TODAY)
        self.assertEqual(s["stopped_by"], "MAX_TRADES_REACHED")


BACKTEST_LOG = """
=== starting: venv/bin/python3 -u scripts/run_backtest_range.py 2026-11-12 2026-11-12 --strategy scripts/v28_strategy_main.py --mode BACKTEST ===
[1/1] Running: 2026-11-12
Running mode: BACKTEST
Entered position at 2026-11-12 11:19:00+05:30 | side=PE symbol=NIFTY26N1725300PE reason=BUY_PE_MTF_BEAR_S6 entry=197.15 spot=25295.75
[OPEN PNL] 2026-11-12 11:32:00 IST | entry_time=2026-11-12 11:19:00+05:30 | trade_count=2 | symbol=NIFTY26N1725300PE | side=PE | ltp=194.75 | entry=197.15 | qty=65 | unrealized=-2.40 pts (-156.00 rs) | realized=1560.00 rs | total_mtm=1404.00 rs
Exited position at 2026-11-12 11:32:00+05:30 with reason LOW_MFE_EXIT. Entry=197.15 High=206.00 Exit=193.75 | P&L: -221.00 Rs (-3.40 points). Cumulative MTM: 1339.00 Rs.
"""


class RunModeAndPaperExcelTests(unittest.TestCase):
    """12 Nov: a backtest run from the v28 panel replaced the paper log; the card showed a phantom open trade."""

    def test_log_run_mode(self):
        self.assertEqual(webapp.log_run_mode(BACKTEST_LOG), "BACKTEST")
        paper = "=== starting: python -u scripts/v28_strategy_main.py --mode PAPERTRADE_LIVE ===\nKite login ok\n"
        self.assertEqual(webapp.log_run_mode(paper), "PAPERTRADE_LIVE")             # from the start line
        self.assertEqual(webapp.log_run_mode(BACKTEST_LOG + paper), "PAPERTRADE_LIVE")  # latest run wins
        self.assertEqual(webapp.log_run_mode("no banner"), "")

    def write_paper_xlsx(self, folder, name, trades):
        import pandas as pd
        with pd.ExcelWriter(Path(folder) / name) as xw:
            pd.DataFrame(trades).to_excel(xw, sheet_name="live_trade_events", index=False)
            pd.DataFrame([{"metric": "gap_type", "value": "GAP_DOWN"}]).to_excel(xw, sheet_name="summary", index=False)

    def test_today_paper_trades_merges_sessions_and_filters(self):
        import tempfile
        t1 = {"entry_time": "2026-11-12 10:54:00+05:30", "exit_time": "2026-11-12 11:11:00+05:30", "side": "PE",
              "option_symbol": "NIFTY26N1725350PE", "entry_price": 198.7, "exit_price": 212.35, "exit_reason": "TRAILING_STOP_HIT",
              "pnl_points": 13.65, "pnl_rs": 887.25}
        t2 = dict(t1, entry_time="2026-11-12 11:15:00+05:30", exit_time="2026-11-12 11:25:00+05:30", option_symbol="NIFTY26N1725300PE",
                  entry_price=199.6, exit_price=198.0, exit_reason="LOW_MFE_EXIT", pnl_points=-1.6, pnl_rs=-104.0)
        with tempfile.TemporaryDirectory() as tmp:
            self.write_paper_xlsx(tmp, "papertrade_12112026_105500_mtf5min_mfe_max2_sl25_v28.xlsx", [t1])
            self.write_paper_xlsx(tmp, "papertrade_12112026_111500_mtf5min_mfe_max2_sl25_v28.xlsx", [t1, t2])  # restart repeats t1
            self.write_paper_xlsx(tmp, "papertrade_12112026_105500_mtf5min_mfe_max2_sl25_v30.xlsx", [t2])      # other variant
            self.write_paper_xlsx(tmp, "papertrade_11112026_105500_mtf5min_mfe_max2_sl25_v28.xlsx", [t2])      # other day
            trades = webapp.today_paper_trades(tmp, "v28", "2026-11-12")
        self.assertEqual([t["exit_reason"] for t in trades], ["TRAILING_STOP_HIT", "LOW_MFE_EXIT"])

        # The backtest log contributes nothing; the card shows the two paper trades and no open position.
        summary = webapp.parse_strategy_log_today(BACKTEST_LOG, "2026-11-12") if webapp.log_run_mode(BACKTEST_LOG) != "BACKTEST" \
            else {"trades": [], "trade_count": 0, "open_position": None, "realized_rs": 0.0, "unrealized_rs": 0.0, "total_rs": 0.0, "stopped_by": ""}
        merged = webapp.merge_today_paper_excel(summary, trades)
        self.assertIsNone(merged["open_position"])
        self.assertEqual(merged["trade_count"], 2)
        self.assertEqual(merged["total_rs"], 783.25)
        self.assertEqual((merged["trades"][0]["time"], merged["trades"][0]["entry"], merged["trades"][0]["exit"]), ("11:11", 198.7, 212.35))

    def test_merge_keeps_open_position_and_newer_log_trades(self):
        log = webapp.parse_strategy_log_today(LOG, TODAY)          # 1 closed trade + open PE (-520 unrealized)
        excel = [{"entry_time": "2026-11-09 11:28:00+05:30", "exit_time": "2026-11-09 11:38:27+05:30", "side": "CE",
                  "symbol": "NIFTY26N1725050CE", "entry_price": 194.35, "exit_price": 244.35,
                  "exit_reason": "TRAILING_STOP_HIT", "pnl_points": 50.0, "pnl_rs": 3250.0}]
        merged = webapp.merge_today_paper_excel(log, excel)
        self.assertEqual(merged["open_position"]["symbol"], "NIFTY26N1725000PE")
        self.assertEqual((merged["realized_rs"], merged["total_rs"]), (3250.0, 2730.0))
        self.assertIs(webapp.merge_today_paper_excel(log, []), log)  # no Excel yet -> log as before

    def test_today_backtest_summary_uses_newest_file_of_today(self):
        import os
        import tempfile
        import pandas as pd

        def write(folder, name, trades, mtime):
            path = Path(folder) / name
            with pd.ExcelWriter(path) as xw:
                pd.DataFrame(trades).to_excel(xw, sheet_name="trades", index=False)
            os.utime(path, (mtime, mtime))

        t = {"entry_time": "2026-11-13 09:41:00+05:30", "exit_time": "2026-11-13 09:58:00+05:30", "side": "CE",
             "option_symbol": "NIFTY26N1725150CE", "entry_price": 186.0, "exit_price": 200.0,
             "exit_reason": "TRAILING_STOP_HIT", "pnl_points": 14.0, "pnl_rs": 910.0, "net_pnl_rs": 836.0}
        loss = dict(t, entry_time="2026-11-13 09:27:00+05:30", exit_time="2026-11-13 09:30:00+05:30", side="PE",
                    option_symbol="NIFTY26N1725100PE", entry_price=166.1, exit_price=140.1,
                    exit_reason="EMERGENCY_SL_HIT", pnl_points=-26.0, pnl_rs=-1690.0)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(webapp.today_backtest_summary(tmp, "v30", "2026-11-13"))
            write(tmp, "backtest_13112026_mtf5min_mfe_max2_sl25_v30_csv.xlsx", [t, loss], 2_000_000_000)
            write(tmp, "backtest_13112026_mtf5min_mfe_max3_sl25_v30_csv.xlsx", [t], 1_900_000_000)   # older run
            write(tmp, "backtest_12112026_mtf5min_mfe_max2_sl25_v30_csv.xlsx", [t], 2_100_000_000)   # other day
            write(tmp, "backtest_13112026_mtf5min_mfe_max2_sl25_v28_csv.xlsx", [t], 2_100_000_000)   # other variant
            bt = webapp.today_backtest_summary(tmp, "v30", "2026-11-13")
        self.assertEqual((bt["trade_count"], bt["total_rs"]), (2, -780.0))
        self.assertEqual([r["time"] for r in bt["trades"]], ["09:30", "09:58"])
        self.assertEqual(bt["settings"], "mtf5min_mfe_max2_sl25 (csv)")


class AutostartParserTests(unittest.TestCase):
    def test_statuses(self):
        def status(lines, markers=None):
            return webapp.parse_autostart_log_today("\n".join(lines), TODAY, markers or {})["status"]

        self.assertEqual(status([]), "NOT_RUN")
        self.assertEqual(status([f"[{TODAY} 09:00:01 IST] waiting for today's Kite token: expired"]), "WAITING_TOKEN")
        self.assertEqual(status([f"[{TODAY} 09:00:01 IST] market holiday (Diwali): nothing to do"]), "HOLIDAY")
        self.assertEqual(status([f"[{TODAY} 08:55:00 IST] outside the start window 09:00-12:00 IST: nothing to do"]), "OUTSIDE_WINDOW")
        self.assertEqual(status([f"[{TODAY} 09:00:03 IST] ERROR: v28: start failed: boom"]), "ERROR")
        self.assertEqual(status(["[2026-11-08 09:00:01 IST] waiting for today's Kite token"]), "NOT_RUN")  # other day
        started = webapp.parse_autostart_log_today("", TODAY, {"v28": "09:00", "v30": "09:00"})
        self.assertEqual(started["status"], "STARTED")
        self.assertIn("v28 at 09:00", started["detail"])


class TodayEndpointTests(unittest.TestCase):
    def test_endpoint_shape_and_login(self):
        self.assertEqual(webapp.app.test_client().get("/api/today").status_code, 401)
        client = webapp.app.test_client()
        with client.session_transaction() as s:
            s["logged_in"] = True
        j = client.get("/api/today").get_json()
        self.assertTrue(j["ok"])
        for key in ("date", "token", "autostart", "strategies", "download", "parity"):
            self.assertIn(key, j)
        self.assertEqual(set(j["strategies"]), {"v28", "v30"})

    def test_panel_on_dashboard(self):
        client = webapp.app.test_client()
        with client.session_transaction() as s:
            s["logged_in"] = True
        html = client.get("/").get_data(as_text=True)
        self.assertIn('id="today-section"', html)
        self.assertIn("loadTodayPanel", html)


if __name__ == "__main__":
    unittest.main()
