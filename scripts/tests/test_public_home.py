"""Public pages: only the login page (which also describes the site) and robots.txt are public.
Every other route must still need login.

    venv\\Scripts\\python.exe -m unittest discover -s scripts\\tests -p "test_public_home.py" -v
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import app as webapp  # noqa: E402

PUBLIC = {"/robots.txt", "/login"}
DASHBOARD_MARKERS = ("loadDailyDownloadStatus", "start-v28", "/api/start", "editable-config")


class PublicPagesTests(unittest.TestCase):
    def setUp(self):
        self.anon = webapp.app.test_client()
        self.user = webapp.app.test_client()
        with self.user.session_transaction() as s:
            s["logged_in"] = True

    def test_root_redirects_visitors_to_login(self):
        r = self.anon.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers.get("Location", ""))

    def test_login_page_describes_the_site(self):
        r = self.anon.get("/login")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Personal Algorithmic Trading Dashboard", html)
        self.assertIn('name="description"', html)
        self.assertIn("not a public service", html)
        self.assertIn('name="password"', html)  # still the login form
        for marker in DASHBOARD_MARKERS:
            self.assertNotIn(marker, html, f"dashboard content leaked to the login page: {marker}")

    def test_dashboard_for_signed_in_user(self):
        r = self.user.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("loadDailyDownloadStatus", r.get_data(as_text=True))

    def test_robots_txt(self):
        r = self.anon.get("/robots.txt")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.mimetype.startswith("text/plain"))
        self.assertEqual(r.get_data(as_text=True), "User-agent: *\nAllow: /login$\nDisallow: /\n")

    def test_home_template_removed(self):
        self.assertFalse((PROJECT_ROOT / "templates" / "home.html").exists())

    def test_every_other_route_requires_login(self):
        checked = 0
        for rule in webapp.app.url_map.iter_rules():
            if rule.endpoint == "static" or rule.arguments:
                continue
            path = str(rule.rule)
            if path in PUBLIC:
                continue
            for method in sorted((rule.methods or set()) & {"GET", "POST"}):
                with self.subTest(route=path, method=method):
                    r = self.anon.open(path, method=method)
                    location = r.headers.get("Location", "")
                    blocked = r.status_code == 401 or (r.status_code in (301, 302, 303, 307, 308) and "/login" in location)
                    self.assertTrue(blocked, f"{method} {path} answered {r.status_code} without login")
                    checked += 1
        self.assertGreater(checked, 30, "expected to check the dashboard's many routes")

    def test_public_endpoints_are_exactly_these(self):
        self.assertEqual(webapp.PUBLIC_ENDPOINTS, {"login_page", "static", "robots_txt"})


if __name__ == "__main__":
    unittest.main()
