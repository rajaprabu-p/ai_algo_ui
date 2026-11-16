"""Tests for scripts/scheduled_live_start.py. No real process is started and Kite is never called.

    venv\\Scripts\\python.exe -m unittest discover -s scripts\\tests -p "test_scheduled_live_start.py" -v
"""

import contextlib
import io
import json
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

import scheduled_live_start as sls  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def fixed_datetime(value: datetime):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return value if tz is None else value.astimezone(tz)
    return Fixed


class ScheduledLiveStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="live_autostart_"))
        self.settings = self.tmp / "live_autostart.json"
        self.started = []
        self.fake_app = types.SimpleNamespace(start_named_strategy=self.fake_start)
        self.start_result = (True, "started:4242")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_start(self, name, script, args, mode=""):
        self.started.append((name, Path(script).name, tuple(args), mode))
        return self.start_result

    def switch(self, enabled=True, strategy="v30"):
        self.settings.write_text(json.dumps({"enabled": enabled, "strategy": strategy}), encoding="utf-8")

    def run_main(self, when, argv=(), login=(True, "token valid"), holidays=None, files=(True, "version test")):
        out = io.StringIO()
        patches = [
            mock.patch.object(sls, "datetime", fixed_datetime(when)),
            mock.patch.object(sls, "MARKER_DIR", self.tmp),
            mock.patch.object(sls, "SETTINGS_PATH", self.settings),
            mock.patch.object(sls, "kite_login_ok", return_value=login),
            mock.patch.object(sls, "strategy_files_ok", return_value=files),
            mock.patch.object(sls, "latest_start_time", return_value=sls.EARLIEST_START.replace(hour=12)),
            mock.patch.dict(sys.modules, {"app": self.fake_app}),
            mock.patch("daily_download.load_holidays", return_value=holidays or {}),
            mock.patch.object(sys, "argv", ["scheduled_live_start.py", *argv]),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(out))
            code = sls.main()
        return code, out.getvalue()

    def at(self, text):
        return datetime.fromisoformat(text).replace(tzinfo=IST)

    def test_off_by_default(self):
        code, out = self.run_main(self.at("2026-11-27 09:05"))            # no settings file at all
        self.assertEqual((code, self.started), (0, []))
        self.assertIn("live auto-start is off", out)
        for enabled, strategy in ((False, "v30"), (True, ""), (True, "v99"), ("yes", "v30")):
            with self.subTest(enabled=enabled, strategy=strategy):
                self.switch(enabled, strategy)
                code, out = self.run_main(self.at("2026-11-27 09:05"))
                self.assertEqual((code, self.started), (0, []))
                self.assertIn("live auto-start is off", out)
        self.settings.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.run_main(self.at("2026-11-27 09:05"))[0], 0)
        self.assertEqual(self.started, [])

    def test_starts_the_chosen_strategy_once_per_day(self):
        self.switch(True, "v30")
        code, out = self.run_main(self.at("2026-11-27 09:05"))
        self.assertEqual(code, 0)
        self.assertEqual(self.started, [("live_v30", "v30_strategy_main.py", ("--mode", "LIVE_ORDER"), "LIVE_ORDER")])
        self.assertTrue((self.tmp / "live_autostart_2026-11-27.done").exists())
        # stopped from the dashboard later: not restarted, even if the switch now names the other strategy
        self.switch(True, "v28")
        code, out = self.run_main(self.at("2026-11-27 10:30"))
        self.assertEqual((code, len(self.started)), (0, 1))
        self.assertIn("already auto-started today", out)
        # next trading day: the strategy the switch names now
        self.run_main(self.at("2026-11-30 09:00"))
        self.assertEqual(self.started[-1][0], "live_v28")

    def test_no_command_line_strategy(self):
        self.switch(True, "v30")
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_main(self.at("2026-11-27 09:05"), ["v28"])
        self.assertEqual(self.started, [])

    def test_calendar_and_window(self):
        import datetime as dt
        self.switch()
        for when, holidays, text in (("2026-11-28 09:05", None, "weekend"),
                                     ("2026-11-27 09:05", {dt.date(2026, 11, 27): "Test"}, "market holiday (Test)"),
                                     ("2026-11-27 08:55", None, "outside the start window"),
                                     ("2026-11-27 12:05", None, "outside the start window")):
            with self.subTest(when=when):
                code, out = self.run_main(self.at(when), holidays=holidays)
                self.assertEqual((code, self.started), (0, []))
                self.assertIn(text, out)

    def test_waits_for_token(self):
        self.switch()
        code, out = self.run_main(self.at("2026-11-27 09:05"), login=(False, "Kite token expired"))
        self.assertEqual((code, self.started), (2, []))
        self.assertIn("waiting for today's Kite token", out)
        self.assertFalse((self.tmp / "live_autostart_2026-11-27.done").exists())

    def test_files_must_match_the_manifest(self):
        self.switch()
        code, out = self.run_main(self.at("2026-11-27 09:05"), files=(False, "strategy/engine.py changed"))
        self.assertEqual((code, self.started), (1, []))
        self.assertIn("LIVE_ORDER not started: strategy/engine.py changed", out)
        self.assertFalse((self.tmp / "live_autostart_2026-11-27.done").exists())

    def test_unreadable_holiday_file_blocks_live(self):
        self.switch()
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sls, "datetime", fixed_datetime(self.at("2026-11-27 09:05"))))
            stack.enter_context(mock.patch.object(sls, "SETTINGS_PATH", self.settings))
            stack.enter_context(mock.patch.object(sls, "MARKER_DIR", self.tmp))
            stack.enter_context(mock.patch.dict(sys.modules, {"app": self.fake_app}))
            stack.enter_context(mock.patch("daily_download.load_holidays", side_effect=OSError("gone")))
            stack.enter_context(mock.patch.object(sys, "argv", ["scheduled_live_start.py"]))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = sls.main()
        self.assertEqual((code, self.started), (1, []))
        self.assertIn("not starting LIVE_ORDER", out.getvalue())

    def test_failed_start_is_retried_and_dry_run_starts_nothing(self):
        self.switch()
        code, out = self.run_main(self.at("2026-11-27 09:05"), ["--dry-run"])
        self.assertEqual((code, self.started), (0, []))
        self.assertIn("dry run: would start LIVE_ORDER for v30", out)
        self.start_result = (False, "boom")
        code, out = self.run_main(self.at("2026-11-27 09:05"))
        self.assertEqual(code, 1)
        self.assertIn("start failed", out)
        self.assertFalse((self.tmp / "live_autostart_2026-11-27.done").exists())

    def test_paper_scheduler_still_cannot_start_live(self):
        import scheduled_paper_start as sps
        self.assertEqual(sps.MODE, "PAPERTRADE_LIVE")


if __name__ == "__main__":
    unittest.main()
