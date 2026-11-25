"""Tests for how the dashboard launches strategy processes (app.py).

Item 2 of the LIVE_ORDER readiness review: on the systemd server, restarting the
dashboard's service must not stop running strategies.

    venv\\Scripts\\python.exe -m unittest discover -s scripts\\tests -p "test_strategy_launch.py" -v
"""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import app as webapp  # noqa: E402

CMD = ["/root/ai_algo_ui/venv/bin/python", "-u", "scripts/v30_strategy_main.py", "--mode", "LIVE_ORDER"]


class SystemdScopeTests(unittest.TestCase):
    def setUp(self):
        self._saved = webapp._SYSTEMD_SCOPE_OK
        webapp._SYSTEMD_SCOPE_OK = None

    def tearDown(self):
        webapp._SYSTEMD_SCOPE_OK = self._saved

    def linux(self, which="/usr/bin/systemd-run", systemd_dir=True, probe_rc=0):
        return (
            mock.patch.object(webapp.os, "name", "posix"),
            mock.patch.object(webapp.shutil, "which", return_value=which),
            mock.patch.object(webapp.os.path, "isdir", return_value=systemd_dir),
            mock.patch.object(webapp.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], probe_rc)),
        )

    def test_systemd_server_runs_strategy_in_its_own_scope(self):
        a, b, c, d = self.linux()
        with a, b, c, d as run:
            wrapped = webapp.detached_strategy_command(CMD, "live_v30")
            self.assertEqual(wrapped[:4], ["/usr/bin/systemd-run", "--scope", "--quiet", "--collect"])
            self.assertTrue(wrapped[4].startswith("--unit=ai-algo-live_v30-"))
            self.assertEqual(wrapped[5:], CMD)
            webapp.detached_strategy_command(CMD, "v28")
            self.assertEqual(run.call_count, 1)     # probed once, then cached

    def test_unit_name_is_sanitised(self):
        a, b, c, d = self.linux()
        with a, b, c, d:
            unit = webapp.detached_strategy_command(CMD, "my strat/1")[4]
        self.assertRegex(unit, r"^--unit=ai-algo-my_strat_1-\d{8}-\d{6}$")

    def test_falls_back_to_plain_command(self):
        for kwargs in (dict(which=None), dict(systemd_dir=False), dict(probe_rc=1)):
            with self.subTest(**kwargs):
                webapp._SYSTEMD_SCOPE_OK = None
                a, b, c, d = self.linux(**kwargs)
                with a, b, c, d:
                    self.assertEqual(webapp.detached_strategy_command(CMD, "v30"), CMD)

    def test_probe_error_falls_back(self):
        a, b, c, _ = self.linux()
        with a, b, c, mock.patch.object(webapp.subprocess, "run", side_effect=OSError("no dbus")):
            self.assertEqual(webapp.detached_strategy_command(CMD, "v30"), CMD)

    def test_windows_unchanged(self):
        with mock.patch.object(webapp.os, "name", "nt"):
            self.assertEqual(webapp.detached_strategy_command(CMD, "v30"), CMD)
            self.assertIn("creationflags", webapp.strategy_process_options())

    def test_posix_strategy_gets_its_own_session(self):
        with mock.patch.object(webapp.os, "name", "posix"):
            self.assertEqual(webapp.strategy_process_options(), {"start_new_session": True})


class StartNamedStrategyTests(unittest.TestCase):
    def test_start_uses_wrapped_command_and_records_strategy_pid(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            fake_proc = mock.Mock(pid=4242)
            wrapped = ["/usr/bin/systemd-run", "--scope", "--quiet", "--collect", "--unit=x"] + CMD
            with mock.patch.object(webapp, "LOGS_DIR", tmp), \
                    mock.patch.object(webapp, "pid_path_for", return_value=str(Path(tmp) / "v30.pid")), \
                    mock.patch.object(webapp, "set_active_log_path"), \
                    mock.patch.object(webapp, "write_mode"), \
                    mock.patch.object(webapp, "live_strategy_lock_is_held", return_value=False), \
                    mock.patch.object(webapp, "detached_strategy_command", return_value=wrapped) as wrap, \
                    mock.patch.object(webapp.subprocess, "Popen", return_value=fake_proc) as popen:
                ok, msg = webapp.start_named_strategy("v30", "scripts/v30_strategy_main.py",
                                                      ["--mode", "LIVE_ORDER"], mode="LIVE_ORDER")
            self.assertEqual((ok, msg), (True, "started:4242"))
            self.assertEqual(wrap.call_args.args[1], "v30")
            self.assertEqual(popen.call_args.args[0], wrapped)
            self.assertEqual((Path(tmp) / "v30.pid").read_text(encoding="utf-8"), "4242")


if __name__ == "__main__":
    unittest.main()
