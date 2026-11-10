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
