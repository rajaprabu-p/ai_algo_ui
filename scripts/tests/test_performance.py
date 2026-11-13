"""Tests for the performance page (/performance, /api/performance and its pure helpers).

    venv\\Scripts\\python.exe -m unittest discover -s scripts\\tests -p "test_performance.py" -v
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import app as webapp  # noqa: E402


def t(pnl, net=None, reason="TRAILING_STOP_HIT", entry="2026-10-05 09:30:00"):
    return {"entry_time": entry, "side": "CE", "symbol": "X", "exit_reason": reason,
            "pnl_rs": pnl, "pnl_points": pnl / 65, "net_pnl_rs": net}


class ClassifyTests(unittest.TestCase):
    def test_file_names(self):
        c = webapp.classify_result_file
        self.assertEqual(c("backtest_30102026_mtf5min_mfe_max2_sl25_v28_csv.xlsx"),
                         {"source": "backtest", "date": "2026-10-30", "variant": "v28", "settings": "mtf5min_mfe_max2_sl25 (csv)"})
        self.assertEqual(c("backtest_01102026_v28_mtf5min_sl25.xlsx")["settings"], "mtf5min_sl25")
        self.assertEqual(c("backtest_01092026_v30_main_mtf1min_strict1_sl50.xlsx")["variant"], "v30")
        self.assertEqual(c("papertrade_21102026_102400_mtf5min_max2_sl25_v28.xlsx"),
                         {"source": "paper", "date": "2026-10-21", "variant": "v28", "settings": "mtf5min_max2_sl25"})
        self.assertEqual(c("livetrade_11112026_090012_mtf5min_mfe_max2_sl25_v28.xlsx"),
                         {"source": "live", "date": "2026-11-11", "variant": "v28", "settings": "mtf5min_mfe_max2_sl25"})
        self.assertEqual(c("livetrade_11112026_090012_v30.xlsx")["source"], "live")
        self.assertIsNone(c("papertrade_09112026_112809_v30.txt"))
        self.assertIsNone(c("strategy_v28.log"))
        self.assertIsNone(c("backtest_31022026_v28_x.xlsx"))  # invalid date


class ComputeTests(unittest.TestCase):
    def test_kpis_equity_drawdown(self):
        days = [
            {"date": "2026-10-05", "trades": [t(2000), t(-1625, reason="EMERGENCY_SL_HIT")], "gap_type": "GAP_UP", "cpr_type": "NARROW"},
            {"date": "2026-10-06", "trades": [t(-3250, reason="EMERGENCY_SL_HIT")], "gap_type": "GAP_DOWN", "cpr_type": "WIDE"},
            {"date": "2026-10-07", "trades": [t(5000)], "gap_type": "GAP_UP", "cpr_type": "WIDE"},
        ]
        r = webapp.compute_performance(days)
        k = r["kpis"]
        self.assertEqual(k["basis"], "gross")
        self.assertEqual((k["net_pnl"], k["trades"], k["trading_days"], k["losing_days"]), (2125, 4, 3, 1))
        self.assertEqual(k["win_rate"], 50.0)
        self.assertEqual(k["profit_factor"], round(7000 / 4875, 2))
        self.assertEqual([d["equity"] for d in r["daily"]], [375, -2875, 2125])
        self.assertEqual([d["drawdown"] for d in r["daily"]], [0, -3250, 0])
        self.assertEqual(k["max_drawdown"], -3250)
        weekdays = [g["label"] for g in r["breakdowns"]["weekday"]]
        self.assertEqual(weekdays, ["Mon", "Tue", "Wed"])  # weekday order, not by count
        gap = {g["label"]: g for g in r["breakdowns"]["gap_type"]}
        self.assertEqual((gap["GAP_UP"]["count"], gap["GAP_UP"]["total"]), (2, 5375))

    def test_net_basis_only_when_every_trade_has_it(self):
        net_days = [{"date": "2026-10-05", "trades": [t(1000, 930), t(-500, -570)]}]
        k = webapp.compute_performance(net_days)["kpis"]
        self.assertEqual((k["basis"], k["net_pnl"], k["charges"]), ("net", 360, 140))
        mixed = [{"date": "2026-10-05", "trades": [t(1000, 930), t(-500, None)]}]
        k = webapp.compute_performance(mixed)["kpis"]
        self.assertEqual((k["basis"], k["net_pnl"], k["charges"]), ("gross", 500, None))

    def test_empty(self):
        r = webapp.compute_performance([])
        self.assertEqual((r["kpis"]["trades"], r["daily"]), (0, []))

    def test_day_trade_list_for_the_calendar(self):
        trades = [
            {**t(-1266, -1340, reason="PROFIT_LOCK_HIT"), "entry_time": "2026-11-06 09:53:00", "exit_time": "2026-11-06 09:57:00",
             "side": "CE", "symbol": "NIFTY26N0924650CE", "entry_price": 164.6, "exit_price": 146.2, "pnl_points": -18.4},
            {**t(2784, 2700), "entry_time": "2026-11-06 09:34:00", "exit_time": "2026-11-06 09:49:00",
             "side": "CE", "symbol": "NIFTY26N0924550CE", "entry_price": 151.4, "exit_price": 195.4, "pnl_points": 44.0},
        ]
        day = webapp.compute_performance([{"date": "2026-11-06", "trades": trades}])["daily"][0]
        tl = day["trade_list"]
        self.assertEqual([x["entry_time"] for x in tl], ["09:34", "09:53"])          # sorted by entry time
        self.assertEqual((tl[0]["exit_time"], tl[0]["entry_price"], tl[0]["exit_price"]), ("09:49", 151.4, 195.4))
        self.assertEqual([x["pnl"] for x in tl], [2700, -1340])                       # same (net) basis as the day total
        self.assertEqual(sum(x["pnl"] for x in tl), day["pnl"])


class LiveFilesTests(unittest.TestCase):
    def test_live_sessions_merged_per_day(self):
        import shutil
        import tempfile

        import pandas as pd

        tmp = tempfile.mkdtemp(prefix="perf_live_")
        try:
            def write(name, rows):
                with pd.ExcelWriter(Path(tmp) / name) as xw:
                    pd.DataFrame([{"metric": "gap_type", "value": "GAP_UP"}, {"metric": "cpr_type", "value": "WIDE"}]).to_excel(
                        xw, sheet_name="summary", index=False)
                    pd.DataFrame(rows).to_excel(xw, sheet_name="live_trade_events", index=False)
            a = {"entry_time": "2026-11-11 09:25:00", "side": "CE", "option_symbol": "NIFTY26N1724900CE",
                 "exit_reason": "TRAILING_STOP_HIT", "pnl_rs": 1950.0, "pnl_points": 30.0}
            b = {"entry_time": "2026-11-11 10:40:00", "side": "PE", "option_symbol": "NIFTY26N1724900PE",
                 "exit_reason": "EMERGENCY_SL_HIT", "pnl_rs": -1625.0, "pnl_points": -25.0}
            write("livetrade_11112026_090012_mtf5min_mfe_max2_sl25_v28.xlsx", [a])
            write("livetrade_11112026_101500_mtf5min_mfe_max2_sl25_v28.xlsx", [a, b])  # restart repeats trade a
            write("papertrade_11112026_090012_mtf5min_mfe_max2_sl25_v28.xlsx", [b])    # paper: not live
            days = webapp.collect_performance_days(tmp, "live", "v28", "mtf5min_mfe_max2_sl25")
            r = webapp.compute_performance(days)
            self.assertEqual((r["kpis"]["trades"], r["kpis"]["net_pnl"], r["kpis"]["basis"]), (2, 325.0, "gross"))
            self.assertEqual(r["daily"][0]["gap_type"], "GAP_UP")
        finally:
            webapp._PERF_CACHE.clear()
            shutil.rmtree(tmp, ignore_errors=True)


class PaperVsBacktestTests(unittest.TestCase):
    def test_common_days_and_same_basis(self):
        paper = [{"date": "2026-11-12", "trades": [t(887.0)]}, {"date": "2026-11-13", "trades": [t(-741.0)]},
                 {"date": "2026-11-14", "trades": [t(100.0)]}]                       # no backtest that day
        backtest = [{"date": "2026-11-05", "trades": [t(500.0, 450.0)]},             # before paper started
                    {"date": "2026-11-12", "trades": [t(1000.0, 920.0)]},
                    {"date": "2026-11-13", "trades": [t(-780.0, -927.0)]}]
        r = webapp.compare_paper_backtest(paper, backtest)
        self.assertEqual((r["common_days"], r["only_paper"], r["only_backtest"]), (2, ["2026-11-14"], []))
        # paper has no charges, so both sides are gross
        self.assertEqual(r["basis"], "gross")
        self.assertEqual((r["paper"]["kpis"]["net_pnl"], r["backtest"]["kpis"]["net_pnl"]), (146.0, 220.0))
        self.assertEqual(r["backtest"]["kpis"]["basis"], "gross")
        # with charges on both sides, both are net
        paper_net = [dict(d, trades=[t(x["pnl_rs"], x["pnl_rs"] - 70)]) for d, x in ((d, d["trades"][0]) for d in paper)]
        self.assertEqual(webapp.compare_paper_backtest(paper_net, backtest)["basis"], "net")
        self.assertIsNone(webapp.compare_paper_backtest(paper[2:], backtest)["paper"])   # no common day

    def test_backtest_days_inside_the_paper_span_are_listed(self):
        paper = [{"date": "2026-11-11", "trades": []}, {"date": "2026-11-13", "trades": [t(10.0)]}]
        backtest = [{"date": d, "trades": [t(5.0)]} for d in ("2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13")]
        r = webapp.compare_paper_backtest(paper, backtest)
        self.assertEqual((r["common_days"], r["only_backtest"]), (2, ["2026-11-12"]))

    def test_late_paper_start_left_out_unless_asked(self):
        # 9 Nov: paper restarted at 11:28 and missed the backtest's 09:25 trade; 13 Nov restarted
        # before the open (harmless); 12 Nov has no session logs (kept).
        paper = [{"date": "2026-11-09", "sessions": ["08:47", "11:28"], "trades": [t(3253.0)]},
                 {"date": "2026-11-12", "trades": []},
                 {"date": "2026-11-13", "sessions": ["08:50", "09:12"], "trades": [t(-702.0)]}]
        backtest = [{"date": "2026-11-09", "trades": [t(-1690.0)]}, {"date": "2026-11-12", "trades": []},
                    {"date": "2026-11-13", "trades": [t(-455.0)]}]
        r = webapp.compare_paper_backtest(paper, backtest)
        self.assertEqual((r["common_days"], r["late_start"]), (2, [{"date": "2026-11-09", "start": "08:47, 11:28"}]))
        self.assertEqual(r["paper"]["kpis"]["net_pnl"], -702.0)
        r = webapp.compare_paper_backtest(paper, backtest, include_late=True)
        self.assertEqual((r["common_days"], r["paper"]["kpis"]["net_pnl"]), (3, 2551.0))

    def test_first_session_from_file_names(self):
        import shutil
        import tempfile

        import pandas as pd

        tmp = tempfile.mkdtemp(prefix="perf_pvb_")
        try:
            row = {"entry_time": "2026-11-09 11:28:00", "side": "CE", "option_symbol": "X", "exit_reason": "TRAILING_STOP_HIT",
                   "pnl_rs": 3253.25, "pnl_points": 50.05}
            with pd.ExcelWriter(Path(tmp) / "papertrade_09112026_113819_mtf5min_mfe_max2_sl25_v28.xlsx") as xw:
                pd.DataFrame([row]).to_excel(xw, sheet_name="live_trade_events", index=False)
            for name in ("papertrade_09112026_112806_v28.txt", "papertrade_09112026_084734_v28.txt",
                         "papertrade_09112026_084736_v30.txt", "papertrade_01092026_091006_v28_main.txt"):
                (Path(tmp) / name).write_text("log", encoding="utf-8")
            days = webapp.collect_performance_days(tmp, "paper", "v28", "mtf5min_mfe_max2_sl25")
            self.assertEqual(days[0]["sessions"], ["08:47", "11:28"])   # the Excel's 11:38 is not a session start
            self.assertEqual(webapp.session_starts(tmp, "paper", "v28")["2026-09-01"], ["09:10"])
        finally:
            webapp._PERF_CACHE.clear()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_route(self):
        user = webapp.app.test_client()
        with user.session_transaction() as s:
            s["logged_in"] = True
        j = user.get("/api/performance/paper-vs-backtest?variant=v30&from=bad").get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["selected"]["from"] if j["selected"] else "", "")
        self.assertEqual(webapp.app.test_client().get("/api/performance/paper-vs-backtest").status_code, 401)


class RouteTests(unittest.TestCase):
    def test_login_required_and_page(self):
        anon = webapp.app.test_client()
        self.assertEqual(anon.get("/api/performance").status_code, 401)
        self.assertEqual(anon.get("/performance").status_code, 302)
        user = webapp.app.test_client()
        with user.session_transaction() as s:
            s["logged_in"] = True
        html = user.get("/performance").get_data(as_text=True)
        self.assertIn("Equity curve", html)
        j = user.get("/api/performance?source=backtest&variant=v28&from=bad-date").get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["selected"]["from"], "")  # invalid dates are ignored
        self.assertIn('href="/performance"', user.get("/").get_data(as_text=True))
        self.assertEqual(j["holidays"].get("2026-11-10"), "Diwali-Balipratipada")


class HolidayTests(unittest.TestCase):
    def test_load_market_holidays(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "h.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# header\n\n2026-11-10  # Diwali\n2026-12-25\nnot-a-date  # junk\n")
            self.assertEqual(webapp.load_market_holidays(path),
                             {"2026-11-10": "Diwali", "2026-12-25": "Market holiday"})
            self.assertEqual(webapp.load_market_holidays(os.path.join(tmp, "missing.txt")), {})


if __name__ == "__main__":
    unittest.main()
