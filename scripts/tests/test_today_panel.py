"""Tests for the dashboard "Today" panel (/api/today and its log parsers).

    venv\\Scripts\\python.exe -m unittest discover -s scripts\\tests -p "test_today_panel.py" -v
"""

import os
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
        self.assertEqual([(r["entry_time"], r["time"]) for r in bt["trades"]], [("09:27", "09:30"), ("09:41", "09:58")])
        self.assertEqual(bt["settings"], "mtf5min_mfe_max2_sl25 (csv)")


class NiftyChartTests(unittest.TestCase):
    def test_candles(self):
        from datetime import datetime

        rows = [{"date": datetime(2026, 11, 16, 9, 16), "open": 25250.5, "high": 25260.0, "low": 25245.25, "close": 25255.0},
                {"date": datetime(2026, 11, 16, 9, 15), "open": 25240.0, "high": 25252.0, "low": 25238.0, "close": 25250.5},
                {"date": "2026-11-16 09:17:00+05:30", "open": 25255, "high": 25258, "low": 25250, "close": 25251},
                {"date": datetime(2026, 11, 16, 9, 18), "open": None, "high": 1, "low": 1, "close": 1},      # broken row dropped
                {"date": datetime(2026, 11, 16, 9, 19), "open": 0, "high": 0, "low": 0, "close": 0}]         # empty row dropped
        self.assertEqual(webapp.nifty_chart_candles(rows), [
            ["09:15", 25240.0, 25252.0, 25238.0, 25250.5], ["09:16", 25250.5, 25260.0, 25245.25, 25255.0],
            ["09:17", 25255.0, 25258.0, 25250.0, 25251.0]])
        self.assertEqual(webapp.nifty_chart_candles(None), [])

    def test_trades_and_route(self):
        import tempfile
        from datetime import datetime, timedelta
        from unittest import mock

        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp:
            def write(name, rows):
                with pd.ExcelWriter(Path(tmp) / name) as xw:
                    pd.DataFrame(rows).to_excel(xw, sheet_name="live_trade_events", index=False)
            pe = {"entry_time": "2026-11-16 09:24:00", "exit_time": "2026-11-16 09:40:04", "side": "PE",
                  "option_symbol": "NIFTY26N1725250PE", "entry_price": 105.4, "exit_price": 80.1,
                  "exit_reason": "EMERGENCY_SL_HIT", "pnl_rs": -1644.5, "pnl_points": -25.3}
            ce = dict(pe, entry_time="2026-11-16 09:44:00", exit_time="2026-11-16 09:59:36", side="CE",
                      option_symbol="NIFTY26N1725300CE", exit_reason="TRAILING_STOP_HIT", pnl_rs=965.25)
            write("papertrade_16112026_092500_mtf5min_mfe_max2_sl25_v30.xlsx", [pe, ce])
            write("papertrade_16112026_092500_mtf5min_mfe_max2_sl25_v28.xlsx", [pe])
            write("livetrade_16112026_090012_mtf5min_mfe_max2_sl25_v30.xlsx", [ce])
            write("papertrade_13112026_090012_mtf5min_mfe_max2_sl25_v30.xlsx", [dict(pe, entry_time="2026-11-13 09:30:00")])
            try:
                trades = webapp.nifty_chart_trades(tmp, "2026-11-16")
                self.assertEqual([(t["variant"], t["source"], t["side"], t["entry_time"], t["exit_time"], t["pnl_rs"]) for t in trades], [
                    ("v28", "paper", "PE", "09:24", "09:40", -1644.5), ("v30", "paper", "PE", "09:24", "09:40", -1644.5),
                    ("v30", "live", "CE", "09:44", "09:59", 965.25), ("v30", "paper", "CE", "09:44", "09:59", 965.25)])
                self.assertEqual(webapp.nifty_chart_trades(tmp, "2026-11-14"), [])

                asked = []

                class FakeKite:
                    def historical_data(self, token, start, end, interval, continuous=False, oi=False):
                        asked.append((token, start.date().isoformat(), interval))
                        if start.date().isoformat() != "2026-11-16":
                            return []                                   # only the 16th has candles
                        return [{"date": start, "open": 25240.0, "high": 25252.0, "low": 25238.0, "close": 25250.5}]

                client = webapp.app.test_client()
                self.assertEqual(client.get("/api/market/nifty-chart").status_code, 401)
                with client.session_transaction() as s:
                    s["logged_in"] = True
                with mock.patch.object(webapp, "LOGS_DIR", tmp), \
                        mock.patch.object(webapp, "_build_kite_client_from_env", return_value=(FakeKite(), "")):
                    webapp._NIFTY_CHART_CACHE.clear()
                    j = client.get("/api/market/nifty-chart?date=2026-11-16").get_json()
                    self.assertEqual((j["ok"], j["date"], j["candles"], len(j["trades"])),
                                     (True, "2026-11-16", [["09:15", 25240.0, 25252.0, 25238.0, 25250.5]], 4))
                    self.assertEqual(asked, [(256265, "2026-11-16", "minute")])
                    client.get("/api/market/nifty-chart?date=2026-11-16")
                    self.assertEqual(len(asked), 1)                      # second call within 20 s: from the cache
                    # a day without candles: empty chart, no error; a future date is not asked from Kite
                    webapp._NIFTY_CHART_CACHE.clear()
                    self.assertEqual(client.get("/api/market/nifty-chart?date=2026-11-15").get_json()["candles"], [])
                    webapp._NIFTY_CHART_CACHE.clear()
                    before = len(asked)
                    future = (datetime.now(webapp.IST).date() + timedelta(days=3)).isoformat()
                    self.assertEqual(client.get(f"/api/market/nifty-chart?date={future}").get_json()["candles"], [])
                    self.assertEqual(len(asked), before)
                    # no date: walks back over the days without candles and stops (weekends are skipped)
                    webapp._NIFTY_CHART_CACHE.clear()
                    j = client.get("/api/market/nifty-chart").get_json()
                    self.assertTrue(j["ok"])
                    self.assertLessEqual(len(asked) - before, webapp.NIFTY_CHART_LOOKBACK_DAYS + 1)
                with mock.patch.object(webapp, "_build_kite_client_from_env", return_value=(None, "missing_access_token")):
                    webapp._NIFTY_CHART_CACHE.clear()
                    self.assertEqual(client.get("/api/market/nifty-chart").status_code, 400)
            finally:
                webapp._NIFTY_CHART_CACHE.clear()
                webapp._PERF_CACHE.clear()


class LiveAutostartSwitchTests(unittest.TestCase):
    def test_switch_is_off_by_default_and_needs_a_strategy(self):
        import tempfile
        from datetime import datetime
        from unittest import mock

        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

        client = webapp.app.test_client()
        self.assertEqual(client.get("/api/live-autostart").status_code, 401)
        self.assertEqual(client.post("/api/live-autostart", json={"enabled": True, "strategy": "v30"}).status_code, 401)
        with client.session_transaction() as s:
            s["logged_in"] = True
        with tempfile.TemporaryDirectory() as tmp:
            settings = os.path.join(tmp, "live_autostart.json")
            with mock.patch.object(webapp, "LIVE_AUTOSTART_SETTINGS", settings), \
                    mock.patch.object(webapp, "LIVE_AUTOSTART_LOG", os.path.join(tmp, "scheduled_live_start.log")):
                live = client.get("/api/live-autostart").get_json()["live_autostart"]
                self.assertEqual((live["enabled"], live["strategy"], live["status"]), (False, "", "OFF"))
                self.assertFalse(os.path.exists(settings))                      # reading never creates the switch
                for bad in ({"enabled": True}, {"enabled": True, "strategy": "v99"}, {"enabled": False, "strategy": "x"}):
                    self.assertEqual(client.post("/api/live-autostart", json=bad).status_code, 400)
                self.assertFalse(os.path.exists(settings))
                # "enabled" must be exactly true
                live = client.post("/api/live-autostart", json={"enabled": "yes", "strategy": "v30"}).get_json()["live_autostart"]
                self.assertFalse(live["enabled"])
                live = client.post("/api/live-autostart", json={"enabled": True, "strategy": "v30"}).get_json()["live_autostart"]
                self.assertEqual((live["enabled"], live["strategy"], live["status"]), (True, "v30", "NOT_RUN"))
                # the cron script reads the same file
                import scheduled_live_start as sls
                self.assertEqual(sls.load_settings(Path(settings)), {"enabled": True, "strategy": "v30"})
                self.assertEqual(client.get("/api/today").get_json()["live_autostart"]["strategy"], "v30")
                # started today: shown as started with the strategy from the marker
                Path(tmp, f"live_autostart_{TODAY}.done").write_text("2026 v30 started:1\n", encoding="utf-8")
                today = datetime.now(webapp.IST).date().isoformat()
                Path(tmp, f"live_autostart_{today}.done").write_text("2026 v30 started:1\n", encoding="utf-8")
                live = client.get("/api/live-autostart").get_json()["live_autostart"]
                self.assertEqual((live["status"], list(live["started"])), ("STARTED", ["v30"]))
                live = client.post("/api/live-autostart", json={"enabled": False, "strategy": ""}).get_json()["live_autostart"]
                self.assertEqual((live["enabled"], live["strategy"]), (False, ""))


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
        self.assertEqual(status([f"[{TODAY} 09:00:01 IST] live auto-start is off: nothing to do"]), "OFF")
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

    def test_session_health(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            def log(name):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                    f.write("x")
            log("papertrade_09112026_084734_v28.txt")
            log("papertrade_09112026_112806_v28.txt")      # restart after 09:20
            log("papertrade_09112026_084736_v30.txt")
            log("papertrade_08112026_101500_v30.txt")      # other day
            h = webapp.today_session_health(tmp, "v28", "2026-11-09", True, "12:00")
            self.assertEqual((h["starts"], h["late"], h["restarted"], h["stopped_at"]),
                             (["08:47", "11:28"], ["11:28"], True, ""))
            ok = webapp.today_session_health(tmp, "v30", "2026-11-09", True, "12:00")
            self.assertEqual((ok["late"], ok["restarted"], ok["stopped_at"]), ([], False, ""))
            # not running during market hours -> stopped; after the close it is normal
            self.assertRegex(webapp.today_session_health(tmp, "v30", "2026-11-09", False, "12:00")["stopped_at"], r"^\d\d:\d\d$")
            self.assertEqual(webapp.today_session_health(tmp, "v30", "2026-11-09", False, "15:45")["stopped_at"], "")
            # no session today -> nothing to warn about
            self.assertEqual(webapp.today_session_health(tmp, "v28", "2026-11-10", False, "12:00"),
                             {"starts": [], "late": [], "restarted": False, "stopped_at": ""})
            late = webapp.today_session_health(tmp, "v30", "2026-11-08", True, "12:00")
            self.assertEqual((late["late"], late["restarted"]), (["10:15"], False))   # one late start

    def test_parity_run_endpoint(self):
        import json
        import tempfile
        from unittest import mock

        client = webapp.app.test_client()
        with client.session_transaction() as s:
            s["logged_in"] = True
        with tempfile.TemporaryDirectory() as tmp:
            paths = {"PARITY_DIR": tmp, "PARITY_RUN_LOG": os.path.join(tmp, "parity_run.log"),
                     "PARITY_RUN_META": os.path.join(tmp, "parity_run.json")}
            fake_proc = mock.Mock(pid=4321)
            with mock.patch.multiple(webapp, **paths), \
                    mock.patch.object(webapp, "kite_token_status", return_value={"state": "valid", "message": ""}), \
                    mock.patch.object(webapp.subprocess, "Popen", return_value=fake_proc) as popen, \
                    mock.patch.object(webapp, "is_pid_running", return_value=True):
                self.assertEqual(client.post("/api/parity/run", json={"date": "13-11-2026"}).status_code, 400)
                self.assertEqual(client.post("/api/parity/run", json={"date": "2999-01-01"}).status_code, 400)
                r = client.post("/api/parity/run", json={"date": "2026-11-13"})
                self.assertEqual(r.status_code, 200, r.get_json())
                cmd = popen.call_args[0][0]
                self.assertEqual(cmd[-3:], [webapp.PARITY_SCRIPT, "--date", "2026-11-13"][-3:])
                with open(paths["PARITY_RUN_META"], encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["date"], "2026-11-13")
                status = client.get("/api/parity/status").get_json()
                self.assertTrue(status["running"])
                self.assertEqual(status["date"], "2026-11-13")
                # a second start while the first runs is refused
                self.assertEqual(client.post("/api/parity/run", json={}).status_code, 409)
            with mock.patch.multiple(webapp, **paths), mock.patch.object(webapp, "is_pid_running", return_value=False):
                self.assertFalse(client.get("/api/parity/status").get_json()["running"])
                self.assertIn("parity_run", client.get("/api/today").get_json())

    def test_panel_on_dashboard(self):
        client = webapp.app.test_client()
        with client.session_transaction() as s:
            s["logged_in"] = True
        html = client.get("/").get_data(as_text=True)
        self.assertIn('id="today-section"', html)
        self.assertIn("loadTodayPanel", html)


if __name__ == "__main__":
    unittest.main()
