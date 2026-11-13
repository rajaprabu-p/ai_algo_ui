"""Tests for the deploy manifest check (strategy/deploy_check.py). Fast, no strategy import.

    venv\\Scripts\\python.exe -m unittest discover -s scripts\\tests -p "test_deploy_check.py" -v
"""

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from strategy import deploy_check  # noqa: E402


class DeployCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deploy_check_"))
        self.scripts = self.tmp / "scripts"
        (self.scripts / "strategy" / "variants").mkdir(parents=True)
        for rel, text in {
            "strategy/__init__.py": "",
            "strategy/engine.py": "def main():\n    return 1\n",
            "strategy/variants/v28.py": "LOG_IDENTIFIER = 'v28'\n",
            "v28_strategy_main.py": "print('launcher')\n",
            "editable_config.py": "DEFAULTS = {}\n",
        }.items():
            (self.scripts / rel).write_text(text, encoding="utf-8", newline="\r\n")
        (self.scripts / "editable_config.json").write_text("{}", encoding="utf-8")
        deploy_check.write_manifest(self.scripts, version="test-1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_verify(self, mode, environ=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                deploy_check.verify_or_exit(mode, self.scripts, environ or {})
                code = None
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_manifest_lists_expected_files_only(self):
        files = deploy_check.build_manifest(self.scripts, version="x")["files"]
        self.assertEqual(sorted(files), [
            "editable_config.py", "strategy/__init__.py", "strategy/engine.py",
            "strategy/variants/v28.py", "v28_strategy_main.py",
        ])  # editable_config.json is UI state on the server, never fingerprinted

    def test_dashboard_files_in_their_own_section(self):
        (self.tmp / "templates").mkdir()
        (self.tmp / "app.py").write_text("app = 1\n", encoding="utf-8", newline="\r\n")
        (self.tmp / "templates" / "index.html").write_text("<html>\n", encoding="utf-8")
        (self.scripts / "paper_vs_backtest.py").write_text("x = 1\n", encoding="utf-8")
        _, manifest = deploy_check.write_manifest(self.scripts, version="test-2")
        self.assertEqual(sorted(manifest["app_files"]), ["app.py", "scripts/paper_vs_backtest.py", "templates/index.html"])
        self.assertNotIn("app.py", manifest["files"])
        app = deploy_check.check_app(self.scripts)
        self.assertTrue(app.ok, app.problems())
        self.assertEqual((app.version, app.checked), ("test-2", 3))
        # FileZilla line endings still match; a changed template does not
        (self.tmp / "app.py").write_bytes(b"app = 1\n")
        (self.tmp / "templates" / "index.html").write_text("<html>new\n", encoding="utf-8")
        app = deploy_check.check_app(self.scripts)
        self.assertEqual(app.changed, ["templates/index.html"])
        # a dashboard file never blocks LIVE_ORDER: the engine checks "files" only
        self.assertTrue(deploy_check.check(self.scripts).ok)

    def test_old_manifest_without_dashboard_files(self):
        import json
        path = self.scripts / "strategy" / "MANIFEST.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest.pop("app_files")
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertIn("older make_manifest.py", deploy_check.check_app(self.scripts).error)

    def test_all_files_match(self):
        result = deploy_check.check(self.scripts)
        self.assertTrue(result.ok, result.problems())
        self.assertEqual(result.version, "test-1")
        code, out = self.run_verify("LIVE_ORDER")
        self.assertIsNone(code)
        self.assertIn("[DEPLOY CHECK] version=test-1, 5 files OK", out)

    def test_filezilla_ascii_mode_line_endings_still_match(self):
        engine = self.scripts / "strategy" / "engine.py"
        engine.write_bytes(engine.read_bytes().replace(b"\r\n", b"\n"))
        self.assertTrue(deploy_check.check(self.scripts).ok)

    def test_changed_file_blocks_live_order(self):
        (self.scripts / "strategy" / "engine.py").write_text("def main():\n    return 2\n", encoding="utf-8")
        result = deploy_check.check(self.scripts)
        self.assertEqual(result.changed, ["strategy/engine.py"])
        code, out = self.run_verify("LIVE_ORDER")
        self.assertEqual(code, deploy_check.EXIT_CODE_BLOCKED)
        self.assertIn("strategy/engine.py", out)
        self.assertIn("LIVE_ORDER blocked", out)

    def test_missing_file(self):
        (self.scripts / "strategy" / "variants" / "v28.py").unlink()
        result = deploy_check.check(self.scripts)
        self.assertEqual(result.missing, ["strategy/variants/v28.py"])
        self.assertEqual(self.run_verify("LIVE_ORDER")[0], deploy_check.EXIT_CODE_BLOCKED)

    def test_leftover_file_not_in_manifest(self):
        (self.scripts / "strategy" / "old_module.py").write_text("x = 1\n", encoding="utf-8")
        result = deploy_check.check(self.scripts)
        self.assertEqual(result.extra, ["strategy/old_module.py"])
        self.assertFalse(result.ok)

    def test_pycache_is_ignored(self):
        cache = self.scripts / "strategy" / "__pycache__"
        cache.mkdir()
        (cache / "engine.cpython-312.py").write_text("junk", encoding="utf-8")
        self.assertTrue(deploy_check.check(self.scripts).ok)

    def test_no_manifest(self):
        (self.scripts / "strategy" / deploy_check.MANIFEST_NAME).unlink()
        result = deploy_check.check(self.scripts)
        self.assertIn("make_manifest.py", result.error)
        self.assertEqual(self.run_verify("LIVE_ORDER")[0], deploy_check.EXIT_CODE_BLOCKED)

    def test_corrupt_manifest(self):
        (self.scripts / "strategy" / deploy_check.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        self.assertIn("cannot read", deploy_check.check(self.scripts).error)

    def test_paper_and_backtest_only_warn(self):
        (self.scripts / "strategy" / "engine.py").write_text("changed\n", encoding="utf-8")
        for mode in ("PAPERTRADE_LIVE", "BACKTEST"):
            with self.subTest(mode=mode):
                code, out = self.run_verify(mode)
                self.assertIsNone(code)
                self.assertIn("continues", out)

    def test_skip_override_lets_live_order_start(self):
        (self.scripts / "strategy" / "engine.py").write_text("changed\n", encoding="utf-8")
        code, out = self.run_verify("LIVE_ORDER", {"SKIP_DEPLOY_CHECK": "1"})
        self.assertIsNone(code)
        self.assertIn("SKIP_DEPLOY_CHECK is set", out)
        self.assertEqual(self.run_verify("LIVE_ORDER", {"SKIP_DEPLOY_CHECK": "0"})[0], deploy_check.EXIT_CODE_BLOCKED)


class RealProjectManifestTest(unittest.TestCase):
    def test_real_tree_is_covered(self):
        files = deploy_check.tracked_files()
        for rel in ("strategy/engine.py", "strategy/deploy_check.py", "strategy/runtime.py",
                    "strategy/variants/v28.py", "strategy/variants/v30.py",
                    "v28_strategy_main.py", "v30_strategy_main.py", "editable_config.py"):
            self.assertIn(rel, files)
        self.assertFalse(any(f.startswith("tests/") for f in files))


if __name__ == "__main__":
    unittest.main()
