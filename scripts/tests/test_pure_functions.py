"""Unit tests for small, pure strategy functions (v28 and v30).

Fast (~20 s, mostly the import of the two 7,000-line modules). Settings that come
from the UI's editable_config.json are pinned with mock.patch so changing
settings in the UI never breaks these tests.

    venv\\Scripts\\python.exe -m unittest scripts.tests.test_pure_functions -v
    venv\\Scripts\\python.exe -m unittest discover -s scripts\\tests -v      (also runs the 2-minute snapshot)
"""

import importlib.util
import math
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
# Offline kiteconnect first, then scripts/ so `editable_config` resolves.
for path in (str(SCRIPTS_DIR), str(TESTS_DIR / "fake_kite")):
    if path not in sys.path:
        sys.path.insert(0, path)
os.environ.setdefault("FAKE_KITE_SPOT_DIR", str(TESTS_DIR / "fixtures" / "spot"))

MODULES = {}
ENGINE_PATH = SCRIPTS_DIR / "strategy" / "engine.py"


def load_engine(variant: str):
    """Load a private copy of strategy/engine.py configured for one variant.

    The engine reads its variant once at import, so each variant gets its own
    module object (engine_v28, engine_v30) and both can be tested side by side.
    """
    from strategy import runtime

    runtime.configure(variant=variant, argv=[])
    spec = importlib.util.spec_from_file_location(f"engine_{variant}", ENGINE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setUpModule():
    for variant in ("v28", "v30"):
        MODULES[variant] = load_engine(variant)


class StrategyTestCase(unittest.TestCase):
    """Runs each test once per strategy variant."""

    def each_module(self):
        for name, module in MODULES.items():
            with self.subTest(variant=name):
                yield module


class VariantSettingsTests(unittest.TestCase):
    """The only settings that differ between variants (strategy/variants/*.py)."""

    def test_v28(self):
        m = MODULES["v28"]
        self.assertEqual(m.LOG_IDENTIFIER, "v28")
        self.assertEqual(m.ENTRY_SCORE_THRESHOLD, 5)
        self.assertEqual(m.TRAILING_PROFIT_STAGE1[0], (30, 10))
        self.assertEqual(m.TRAILING_PROFIT_STAGE2[0], (25, 20))
        self.assertIsNot(m.TRAILING_PROFIT_STAGE1, m.TRAILING_PROFIT_STAGE2)

    def test_v30(self):
        m = MODULES["v30"]
        self.assertEqual(m.LOG_IDENTIFIER, "v30")
        self.assertEqual(m.ENTRY_SCORE_THRESHOLD, 4)
        self.assertEqual(m.TRAILING_PROFIT_STAGE1[:2], [(25, 15), (30, 20)])
        self.assertEqual(len(m.TRAILING_PROFIT_STAGE1), 16)
        # Same values all day, but a separate list so logs show S1 before 10:30 and S2 after.
        self.assertEqual(m.TRAILING_PROFIT_STAGE1, m.TRAILING_PROFIT_STAGE2)
        self.assertIsNot(m.TRAILING_PROFIT_STAGE1, m.TRAILING_PROFIT_STAGE2)
        self.assertEqual(m.get_trailing_profit_status(26.0, "10:00"), "S1:1:25->15")
        self.assertEqual(m.get_trailing_profit_status(26.0, "10:45"), "S2:1:25->15")

    def test_shared_settings_identical(self):
        differing = {"LOG_IDENTIFIER", "ENTRY_SCORE_THRESHOLD", "TRAILING_PROFIT_STAGE1", "TRAILING_PROFIT_STAGE2", "_VARIANT", "_cli"}
        a, b = vars(MODULES["v28"]), vars(MODULES["v30"])
        for key in sorted(set(a) - differing):
            value = a[key]
            if key.startswith("__") or callable(value) or isinstance(value, type(os)):
                continue
            with self.subTest(setting=key):
                self.assertEqual(repr(value), repr(b[key]))

    def test_unknown_variant_rejected(self):
        from strategy import variants

        with self.assertRaises(ValueError):
            variants.load("../evil")
        with self.assertRaises(ModuleNotFoundError):
            variants.load("v99")


LADDER_S1 = [(30, 10), (50, 45), (60, 55)]
LADDER_S2 = [(25, 20), (40, 35)]


class TrailingStopTests(StrategyTestCase):
    def patched(self, m):
        return mock.patch.multiple(
            m, TRAILING_PROFIT_STAGE1=LADDER_S1, TRAILING_PROFIT_STAGE2=LADDER_S2, TRAILING_PROFIT_STAGE2_TIME="10:30"
        )

    def test_no_change_below_first_rung(self):
        for m in self.each_module():
            with self.patched(m):
                self.assertEqual(m.get_trailing_stop_for_profit(100.0, 29.95, 75.0), 75.0)

    def test_highest_reached_rung_wins(self):
        for m in self.each_module():
            with self.patched(m):
                self.assertEqual(m.get_trailing_stop_for_profit(100.0, 30.0, 75.0), 110.0)
                self.assertEqual(m.get_trailing_stop_for_profit(100.0, 55.0, 75.0), 145.0)
                self.assertEqual(m.get_trailing_stop_for_profit(100.0, 99.0, 75.0), 155.0)

    def test_never_lowers_an_existing_stop(self):
        for m in self.each_module():
            with self.patched(m):
                self.assertEqual(m.get_trailing_stop_for_profit(100.0, 30.0, 130.0), 130.0)

    def test_stage2_ladder_from_switch_time(self):
        for m in self.each_module():
            with self.patched(m):
                before = pd.Timestamp("2026-10-14 10:29")
                at_switch = pd.Timestamp("2026-10-14 10:30")
                self.assertIs(m.get_active_trailing_stages(before), LADDER_S1)
                self.assertIs(m.get_active_trailing_stages(at_switch), LADDER_S2)
                self.assertIs(m.get_active_trailing_stages(None), LADDER_S1)
                # 25 pts is below stage 1's first rung but reaches stage 2's.
                self.assertEqual(m.get_trailing_stop_for_profit(100.0, 25.0, 75.0, before), 75.0)
                self.assertEqual(m.get_trailing_stop_for_profit(100.0, 25.0, 75.0, at_switch), 120.0)

    def test_status_lists_triggered_rungs(self):
        for m in self.each_module():
            with self.patched(m):
                self.assertEqual(m.get_trailing_profit_status(10.0), "")
                self.assertEqual(m.get_trailing_profit_status(52.0), "S1:1:30->10,S1:2:50->45")
                self.assertEqual(m.get_trailing_profit_status(26.0, "11:00"), "S2:1:25->20")

    def test_stage2_peak_only_counts_after_switch(self):
        for m in self.each_module():
            with self.patched(m):
                self.assertIsNone(m.update_stage2_peak(None, 150.0, "10:00"))
                self.assertEqual(m.update_stage2_peak(None, 150.0, "10:31"), 150.0)
                self.assertEqual(m.update_stage2_peak(160.0, 150.0, "10:31"), 160.0)
                self.assertEqual(m.get_trailing_peak_points(100.0, 170.0, 150.0, "10:31"), 50.0)
                self.assertEqual(m.get_trailing_peak_points(100.0, 170.0, None, "10:31"), 0.0)
                self.assertEqual(m.get_trailing_peak_points(100.0, 170.0, 150.0, "10:00"), 70.0)


class EntryStopTests(StrategyTestCase):
    def test_fixed_points_below_entry(self):
        for m in self.each_module():
            with mock.patch.object(m, "FIXED_SL_POINTS", 25):
                self.assertEqual(m.get_entry_stop_price(165.15), 140.15)
                self.assertEqual(m.get_entry_stop_price(100.0), 75.0)

    def test_floors_at_one_tick(self):
        for m in self.each_module():
            with mock.patch.object(m, "FIXED_SL_POINTS", 25):
                self.assertEqual(m.get_entry_stop_price(10.0), 0.05)

    def test_invalid_entry_or_disabled_sl(self):
        for m in self.each_module():
            with mock.patch.object(m, "FIXED_SL_POINTS", 25):
                self.assertEqual(m.get_entry_stop_price(0), 0.0)
                self.assertEqual(m.get_entry_stop_price(None), 0.0)
            with mock.patch.object(m, "FIXED_SL_POINTS", 0):
                self.assertEqual(m.get_entry_stop_price(100.0), 0.05)  # 0 stop, then floored by round_to_tick

    def test_round_to_tick(self):
        for m in self.each_module():
            self.assertEqual(m.round_to_tick(100.03), 100.05)
            self.assertEqual(m.round_to_tick(100.02), 100.0)
            self.assertEqual(m.round_to_tick(0), 0.05)


class GapAndContextTests(StrategyTestCase):
    def test_gap_pct(self):
        for m in self.each_module():
            self.assertAlmostEqual(m.calculate_gap_pct(25250.0, 25000.0), 1.0)
            self.assertAlmostEqual(m.calculate_gap_pct(24900.0, 25000.0), -0.4)
            self.assertIsNone(m.calculate_gap_pct(25000.0, 0))
            self.assertIsNone(m.calculate_gap_pct(0, 25000.0))

    def test_gap_entry_policy(self):
        base = pd.Timestamp("2026-10-14 09:15", tz="Asia/Kolkata")
        delayed = pd.Timestamp("2026-10-14 09:31", tz="Asia/Kolkata")
        for m in self.each_module():
            with mock.patch.multiple(m, MTF_MODE="MANUAL", GAP_DELAY_THRESHOLD_PCT=0.40, GAP_DELAY_ENTRY_TIME="09:30"):
                self.assertEqual(m.apply_gap_entry_policy(None, base, "2026-10-14"), base)
                self.assertEqual(m.apply_gap_entry_policy(0.39, base, "2026-10-14"), base)
                self.assertEqual(m.apply_gap_entry_policy(0.40, base, "2026-10-14"), delayed)
                self.assertEqual(m.apply_gap_entry_policy(-0.75, base, "2026-10-14"), delayed)
                late_start = pd.Timestamp("2026-10-14 10:00", tz="Asia/Kolkata")
                self.assertEqual(m.apply_gap_entry_policy(1.2, late_start, "2026-10-14"), late_start)

    def test_auto_mtf_interval_from_gap(self):
        for m in self.each_module():
            with mock.patch.multiple(m, MTF_MODE="AUTO", MTF_INTERVAL="5min", GAP_CONFIRMATION_THRESHOLD_PCT=0.20):
                self.assertEqual(m.set_mtf_interval_from_gap(0.1), "1min")
                self.assertEqual(m.set_mtf_interval_from_gap(-0.3), "5min")
                self.assertEqual(m.set_mtf_interval_from_gap(None), "5min")
            with mock.patch.multiple(m, MTF_MODE="MANUAL", MTF_INTERVAL="5min"):
                self.assertEqual(m.set_mtf_interval_from_gap(0.1), "5min")

    def test_market_context_gap_and_cpr(self):
        day = pd.DataFrame({
            "spot_open": [25100.0, 25120.0], "spot_high": [25150.0, 25200.0],
            "spot_low": [25050.0, 25080.0], "spot_close": [25120.0, 25180.0],
        })
        for m in self.each_module():
            ctx = m.build_market_context(day, prev_high=25100.0, prev_low=24900.0, prev_close=25000.0)
            self.assertEqual(ctx["gap_type"], "GAP_UP")
            self.assertEqual(ctx["gap_points"], 100.0)
            self.assertAlmostEqual(ctx["gap_pct"], 0.4)
            self.assertEqual(ctx["nifty_change"], "+180 pts (+0.72%)")
            self.assertEqual((ctx["cpr_pivot"], ctx["cpr_bc"], ctx["cpr_tc"]), (25000.0, 25000.0, 25000.0))
            self.assertEqual(ctx["cpr_type"], "NARROW")
            self.assertEqual(m.build_market_context(None)["gap_type"], "")

    def test_market_context_prefers_true_day_open(self):
        # The merged table starts at 09:19; the backtest stores the true 09:15 open in attrs.
        day = pd.DataFrame({
            "spot_open": [22620.95], "spot_high": [22718.0], "spot_low": [22546.0], "spot_close": [22603.0],
        })
        day.attrs.update(nifty_day_open=22690.45, nifty_day_high=22760.0, nifty_day_low=22540.0)
        for m in self.each_module():
            ctx = m.build_market_context(day, prev_high=22800.0, prev_low=22600.0, prev_close=22776.0)
            self.assertEqual(ctx["nifty_open"], 22690)
            self.assertEqual((ctx["nifty_high"], ctx["nifty_low"]), (22760, 22540))
            self.assertAlmostEqual(ctx["gap_pct"], (22690 - 22776) / 22776 * 100)  # -0.38%, not -0.68%
            self.assertLess(abs(ctx["gap_pct"]), m.GAP_DELAY_THRESHOLD_PCT)  # so no 09:31 delay
            day_without = day.copy()
            day_without.attrs = {}
            self.assertEqual(m.build_market_context(day_without, prev_close=22776.0)["nifty_open"], 22621)


class SymbolHelperTests(StrategyTestCase):
    def test_extract_option_strike(self):
        for m in self.each_module():
            self.assertEqual(m.extract_option_strike("NIFTY26O1922700CE"), 22700)
            self.assertEqual(m.extract_option_strike("NIFTY2660923500PE"), 23500)
            self.assertEqual(m.extract_option_strike("nifty26n0925050pe"), 25050)
            self.assertEqual(m.extract_option_strike("NIFTY26NOVFUT"), 0)
            self.assertEqual(m.extract_option_strike(None), 0)

    def test_round_to_step(self):
        for m in self.each_module():
            self.assertEqual(m.round_to_step(25024.9, 50), 25000)
            self.assertEqual(m.round_to_step(25025.1, 50), 25050)
            with self.assertRaises(ValueError):
                m.round_to_step(25000, 0)

    def test_safe_number_parsing(self):
        for m in self.each_module():
            self.assertEqual(m.safe_float("12.5"), 12.5)
            self.assertEqual(m.safe_float(None, 7.0), 7.0)
            self.assertEqual(m.safe_float(float("nan"), 3.0), 3.0)
            self.assertEqual(m.safe_float("abc", 1.0), 1.0)
            self.assertEqual(m.safe_int("12.9"), 12)
            self.assertEqual(m.safe_int(None, 4), 4)


class ExpirySelectionTests(StrategyTestCase):
    """Expiry choice on an expiry day (TRADE_SAME_DAY_EXPIRY)."""

    EXPIRY_DAY = "2026-11-03"
    NEXT_EXPIRY = "2026-11-09"

    def instruments(self):
        rows = []
        token = 1000
        for expiry, code in ((self.EXPIRY_DAY, "26N03"), (self.NEXT_EXPIRY, "26N09")):
            for strike in (23750.0, 23800.0, 23850.0):
                for kind in ("CE", "PE"):
                    token += 1
                    rows.append({
                        "name": "NIFTY", "segment": "NFO-OPT", "instrument_type": kind,
                        "tradingsymbol": f"NIFTY{code}{int(strike)}{kind}", "instrument_token": token,
                        "strike": strike, "expiry": pd.Timestamp(expiry),
                    })
        return pd.DataFrame(rows)

    def select_live(self, m, trade_date, same_day):
        with mock.patch.multiple(m, TRADE_SAME_DAY_EXPIRY=same_day, RUN_MODE="BACKTEST", TRADE_DATE=trade_date,
                                 AUTO_SELECT_EXPIRY="", _LAST_AUTOSELECT_EXPIRY_LOG_KEY=None,
                                 _LAST_AUTOSELECT_SYMBOLS_LOG_KEY=None), \
                mock.patch.object(m, "get_instruments_df", return_value=self.instruments()), \
                mock.patch("builtins.print"):
            return m.auto_select_ce_pe_symbols(kite=None, spot_ltp=23810.0)

    def test_cutoff(self):
        import datetime as dt
        day = dt.date(2026, 11, 3)
        for m in self.each_module():
            with mock.patch.object(m, "TRADE_SAME_DAY_EXPIRY", True):
                self.assertEqual(m.option_expiry_cutoff(day), dt.date(2026, 11, 2))
            with mock.patch.object(m, "TRADE_SAME_DAY_EXPIRY", False):
                self.assertEqual(m.option_expiry_cutoff(day), day)

    def test_default_is_same_day(self):
        for m in self.each_module():
            self.assertTrue(m.TRADE_SAME_DAY_EXPIRY)

    def test_live_selection_on_expiry_day(self):
        for m in self.each_module():
            ce, pe, _, _ = self.select_live(m, self.EXPIRY_DAY, same_day=True)
            self.assertEqual((ce, pe), ("NIFTY26N0323800CE", "NIFTY26N0323800PE"))
            ce, pe, _, _ = self.select_live(m, self.EXPIRY_DAY, same_day=False)
            self.assertEqual((ce, pe), ("NIFTY26N0923800CE", "NIFTY26N0923800PE"))

    def test_live_selection_on_normal_day_unchanged(self):
        for m in self.each_module():
            for same_day in (True, False):
                ce, _, _, _ = self.select_live(m, "2026-11-02", same_day)
                self.assertEqual(ce, "NIFTY26N0323800CE")  # nearest expiry either way

    def test_backtest_csv_selection_on_expiry_day(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            for expiry, code in ((self.EXPIRY_DAY, "26N03"), (self.NEXT_EXPIRY, "26N09")):
                rows = [{
                    "timestamp": f"{self.EXPIRY_DAY}T09:15:00+05:30", "tradingsymbol": f"NIFTY{code}{s}{k}",
                    "strike": float(s), "option_type": k, "expiry": expiry,
                    "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1,
                } for s in (23750, 23800, 23850) for k in ("CE", "PE")]
                pd.DataFrame(rows).to_csv(Path(tmp) / f"nifty_weekly_{expiry}_{self.EXPIRY_DAY}.csv", index=False)
            spot = pd.DataFrame({"ts": [pd.Timestamp(f"{self.EXPIRY_DAY} 09:15", tz="Asia/Kolkata")], "close": [23810.0]})
            for m in self.each_module():
                for same_day, code in ((True, "26N03"), (False, "26N09")):
                    with mock.patch.multiple(m, TRADE_SAME_DAY_EXPIRY=same_day, OPTION_DAILY_DIR=Path(tmp), AUTO_SELECT_EXPIRY=""), \
                            mock.patch("builtins.print"):
                        ce, pe = m.select_downloaded_option_symbols(spot, self.EXPIRY_DAY)
                    with self.subTest(same_day=same_day):
                        self.assertEqual((ce, pe), (f"NIFTY{code}23800CE", f"NIFTY{code}23800PE"))


class ExpiryDayTradeLimitTests(StrategyTestCase):
    """EXPIRY_DAY_MAX_TRADES caps entries on an expiry day."""

    def make_files(self, tmp, names):
        from pathlib import Path

        for name in names:
            (Path(tmp) / name).write_text("timestamp\n", encoding="utf-8")

    def check(self, m, tmp, trade_date, kite=None, **settings):
        from pathlib import Path

        patches = dict(OPTION_DAILY_DIR=Path(tmp), _EXPIRY_DAY_CACHE={}, MAX_TRADES=2, EXPIRY_DAY_MAX_TRADES=1)
        patches.update(settings)
        with mock.patch.multiple(m, **patches), mock.patch.dict(vars(m), {"kite": kite}), mock.patch("builtins.print"):
            return m.is_expiry_day(trade_date), m.get_effective_max_trades(trade_date)

    def test_downloaded_files_decide(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.make_files(tmp, ["nifty_weekly_2026-11-03_2026-11-03.csv", "nifty_weekly_2026-11-09_2026-11-03.csv",
                                  "nifty_weekly_2026-11-09_2026-11-04.csv"])
            for m in self.each_module():
                self.assertEqual(self.check(m, tmp, "2026-11-03"), (True, 1))
                self.assertEqual(self.check(m, tmp, "2026-11-04"), (False, 2))

    def test_kite_instruments_when_no_files(self):
        import tempfile

        class FakeKite:
            def instruments(self, exchange):
                return [
                    {"name": "NIFTY", "segment": "NFO-OPT", "expiry": "2026-11-17", "tradingsymbol": "X", "instrument_token": 1, "strike": 1},
                    {"name": "BANKNIFTY", "segment": "NFO-OPT", "expiry": "2026-11-12", "tradingsymbol": "Y", "instrument_token": 2, "strike": 1},
                ]

        with tempfile.TemporaryDirectory() as tmp:
            for m in self.each_module():
                with mock.patch.dict(m._instruments_cache, {}, clear=True):
                    self.assertEqual(self.check(m, tmp, "2026-11-17", kite=FakeKite()), (True, 1))
                with mock.patch.dict(m._instruments_cache, {}, clear=True):
                    self.assertEqual(self.check(m, tmp, "2026-11-12", kite=FakeKite()), (False, 2))  # BANKNIFTY only

    def test_unknown_falls_back_to_max_trades(self):
        import tempfile

        class BrokenKite:
            def instruments(self, exchange):
                raise RuntimeError("down")

        with tempfile.TemporaryDirectory() as tmp:
            for m in self.each_module():
                self.assertEqual(self.check(m, tmp, "2026-11-17"), (False, 2))  # no files, no kite
                with mock.patch.dict(m._instruments_cache, {}, clear=True):
                    self.assertEqual(self.check(m, tmp, "2026-11-17", kite=BrokenKite()), (False, 2))

    def test_limit_settings(self):
        for m in self.each_module():
            self.assertEqual(m.EXPIRY_DAY_MAX_TRADES, 1)
            with mock.patch.multiple(m, MAX_TRADES=2, EXPIRY_DAY_MAX_TRADES=0):
                self.assertEqual(m.get_effective_max_trades_for(True), 2)   # 0 disables the expiry limit
            with mock.patch.multiple(m, MAX_TRADES=1, EXPIRY_DAY_MAX_TRADES=3):
                self.assertEqual(m.get_effective_max_trades_for(True), 1)   # never raises MAX_TRADES
            with mock.patch.multiple(m, MAX_TRADES=0, EXPIRY_DAY_MAX_TRADES=1):
                self.assertEqual(m.get_effective_max_trades_for(True), 1)
                self.assertEqual(m.get_effective_max_trades_for(False), 0)


class EntryDiagnosticsDedupeTests(StrategyTestCase):
    def capture(self, m, rebuilds, dedupe=True):
        import contextlib
        import io

        out = io.StringIO()
        with mock.patch.object(m, "ENTRY_DIAGNOSTICS_DEDUPE", dedupe), \
                mock.patch.object(m, "_ENTRY_DIAG_SEEN", set()), contextlib.redirect_stdout(out):
            for lines in rebuilds:
                for line in lines:
                    m.print_entry_diag(line)
        return out.getvalue().splitlines()

    def test_live_rebuilds_print_each_candle_once(self):
        # Each live rebuild replays the whole day so far, plus one new candle.
        rebuilds = [
            ["[ENTRY PARITY] spot_ts=09:20 bull=0"],
            ["[ENTRY PARITY] spot_ts=09:20 bull=0", "[ENTRY PARITY] spot_ts=09:21 bull=4"],
            ["[ENTRY PARITY] spot_ts=09:20 bull=0", "[ENTRY PARITY] spot_ts=09:21 bull=4", "[ENTRY BLOCK] premium=288.90 time=09:22"],
        ]
        for m in self.each_module():
            self.assertEqual(self.capture(m, rebuilds), [
                "[ENTRY PARITY] spot_ts=09:20 bull=0",
                "[ENTRY PARITY] spot_ts=09:21 bull=4",
                "[ENTRY BLOCK] premium=288.90 time=09:22",
            ])

    def test_changed_values_print_again(self):
        for m in self.each_module():
            lines = self.capture(m, [["[ENTRY PARITY] spot_ts=09:20 bull=3"], ["[ENTRY PARITY] spot_ts=09:20 bull=4"]])
            self.assertEqual(len(lines), 2)

    def test_dedupe_can_be_turned_off(self):
        for m in self.each_module():
            self.assertEqual(len(self.capture(m, [["x"], ["x"]], dedupe=False)), 2)


class GiftNiftyTests(StrategyTestCase):
    class OkKite:
        def __init__(self):
            self.calls = 0

        def quote(self, keys):
            self.calls += 1
            return {"NSEIX:GIFT NIFTY": {"last_price": 25120.5, "ohlc": {"close": 25000.0}}}

    class BrokenKite:
        def quote(self, keys):
            raise RuntimeError("boom")

    def reset_cache(self, m):
        m._gift_nifty_cache.update({"snapshot": None, "fetched_at": 0.0, "last_error_log_at": 0.0})

    def test_snapshot_values_and_cache(self):
        for m in self.each_module():
            self.reset_cache(m)
            kite = self.OkKite()
            snap = m.get_gift_nifty_snapshot(kite, spot_price=25080.0)
            self.assertEqual(snap, {
                "gift_nifty_ltp": 25120.5, "gift_nifty_change_pts": 120.5,
                "gift_nifty_change_pct": 0.482, "gift_nifty_premium": 40.5,
            })
            m.get_gift_nifty_snapshot(kite, spot_price=25080.0)
            self.assertEqual(kite.calls, 1, "second call within cache window should not hit the API")

    def test_failure_returns_blanks_and_never_raises(self):
        for m in self.each_module():
            self.reset_cache(m)
            with mock.patch("builtins.print"):
                snap = m.get_gift_nifty_snapshot(self.BrokenKite(), spot_price=25080.0)
            self.assertTrue(all(math.isnan(v) for v in snap.values()))
            with mock.patch.object(m, "ENABLE_GIFT_NIFTY_LOG", False):
                self.reset_cache(m)
                kite = self.OkKite()
                self.assertTrue(math.isnan(m.get_gift_nifty_snapshot(kite)["gift_nifty_ltp"]))
                self.assertEqual(kite.calls, 0)


if __name__ == "__main__":
    unittest.main()
