"""Tests for scripts/scheduled_paper_start.py. No real process is started and Kite is never called.

    venv\\Scripts\\python.exe -m unittest discover -s scripts\\tests -p "test_scheduled_paper_start.py" -v
"""

import contextlib
import io
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import scheduled_paper_start as sps  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def fixed_datetime(value: datetime):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return value if tz is None else value.astimezone(tz)
    return Fixed


class ScheduledPaperStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="autostart_"))
        self.started = []
        self.fake_app = types.SimpleNamespace(start_named_strategy=self.fake_start)
        self.start_result = (True, "started:4242")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_start(self, name, script, args, mode=""):
        self.started.append((name, Path(script).name, tuple(args), mode))
        return self.start_result

    def run_main(self, when, argv, login=(True, "token valid"), holidays=None):
        out = io.StringIO()
        patches = [
            mock.patch.object(sps, "datetime", fixed_datetime(when)),
            mock.patch.object(sps, "MARKER_DIR", self.tmp),
            mock.patch.object(sps, "kite_login_ok", return_value=login),
            mock.patch.object(sps, "latest_start_time", return_value=sps.dt_time(12, 0)),
            mock.patch.dict(sys.modules, {"app": self.fake_app}),
            mock.patch("daily_download.load_holidays", return_value=holidays or {}),
            mock.patch.object(sys, "argv", ["scheduled_paper_start.py", *argv]),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(out))
            code = sps.main()
        return code, out.getvalue()

    def at(self, text):
        return datetime.fromisoformat(text).replace(tzinfo=IST)

    def test_weekend_does_nothing(self):
        code, out = self.run_main(self.at("2026-11-14 09:00"), ["v28"])  # Saturday
        self.assertEqual((code, self.started), (0, []))
        self.assertIn("weekend", out)

    def test_holiday_does_nothing(self):
        import datetime as dt
        code, out = self.run_main(self.at("2026-11-10 09:00"), ["v28"], holidays={dt.date(2026, 11, 10): "Diwali"})
        self.assertEqual((code, self.started), (0, []))
        self.assertIn("market holiday (Diwali)", out)

    def test_outside_window_does_nothing(self):
        for when in ("2026-11-11 08:30", "2026-11-11 08:55", "2026-11-11 12:05"):
            with self.subTest(when=when):
                code, out = self.run_main(self.at(when), ["v28"])
                self.assertEqual((code, self.started), (0, []))
                self.assertIn("outside the start window", out)

    def test_starts_at_nine(self):
        code, _ = self.run_main(self.at("2026-11-11 09:00"), ["v28"])
        self.assertEqual((code, [s[0] for s in self.started]), (0, ["v28"]))
        self.assertEqual(sps.EARLIEST_START, sps.dt_time(9, 0))

    def test_waits_for_token(self):
        code, out = self.run_main(self.at("2026-11-11 09:00"), ["v28"], login=(False, "Kite token expired"))
        self.assertEqual((code, self.started), (2, []))
        self.assertIn("waiting for today's Kite token", out)
        self.assertFalse(any(self.tmp.iterdir()), "no marker while waiting, so a later run retries")

    def test_starts_paper_mode_once_per_day(self):
        code, out = self.run_main(self.at("2026-11-11 09:05"), ["v28", "v30"])
        self.assertEqual(code, 0)
        self.assertEqual(self.started, [
            ("v28", "v28_strategy_main.py", ("--mode", "PAPERTRADE_LIVE"), "PAPERTRADE_LIVE"),
            ("v30", "v30_strategy_main.py", ("--mode", "PAPERTRADE_LIVE"), "PAPERTRADE_LIVE"),
        ])
        self.assertTrue((self.tmp / "autostart_v28_2026-11-11.done").exists())
        # Later the same day (e.g. after you stopped it in the dashboard): not restarted.
        code, out = self.run_main(self.at("2026-11-11 10:30"), ["v28", "v30"])
        self.assertEqual((code, len(self.started)), (0, 2))
        self.assertIn("already auto-started today", out)
        # Next trading day starts again.
        self.run_main(self.at("2026-11-12 09:00"), ["v28"])
        self.assertEqual(len(self.started), 3)

    def test_failed_start_is_retried(self):
        self.start_result = (False, "boom")
        code, out = self.run_main(self.at("2026-11-11 09:00"), ["v28"])
        self.assertEqual(code, 1)
        self.assertIn("start failed", out)
        self.assertFalse((self.tmp / "autostart_v28_2026-11-11.done").exists())

    def test_dry_run_starts_nothing(self):
        code, out = self.run_main(self.at("2026-11-11 09:00"), ["v28", "--dry-run"])
        self.assertEqual((code, self.started), (0, []))
        self.assertIn("dry run: would start PAPERTRADE_LIVE for v28", out)

    def test_mode_is_paper_only(self):
        self.assertEqual(sps.MODE, "PAPERTRADE_LIVE")
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_main(self.at("2026-11-11 09:00"), ["live_v28"])  # unknown strategy name rejected


if __name__ == "__main__":
    unittest.main()
