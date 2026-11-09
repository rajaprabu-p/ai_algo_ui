"""Offline stand-in for the kiteconnect package, used only by the snapshot tests.

The test runner puts scripts/tests/fake_kite first on PYTHONPATH, so the
strategy's `from kiteconnect import KiteConnect` loads this module instead of
the real one. It serves saved NIFTY spot candles from FAKE_KITE_SPOT_DIR and
refuses anything that would touch a real account (orders, positions, ticker).
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from .exceptions import DataException, InputException, PermissionException  # noqa: F401

__version__ = "fake"
_SPOT_TOKENS = {256265}


class KiteConnect:
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    def __init__(self, api_key: str = "", *args, **kwargs):
        self.api_key = api_key
        self.access_token = ""
        self._spot_dir = Path(os.environ["FAKE_KITE_SPOT_DIR"])

    def set_access_token(self, access_token: str) -> None:
        self.access_token = access_token

    def profile(self) -> dict:
        return {"user_name": "snapshot-test", "user_id": "TEST"}

    def historical_data(self, instrument_token, from_date, to_date, interval="minute", continuous=False, oi=False):
        if int(instrument_token) not in _SPOT_TOKENS:
            raise RuntimeError(f"fake kite: no fixture for token {instrument_token} (only NIFTY 50 spot is saved)")
        if interval != "minute":
            raise RuntimeError(f"fake kite: only minute candles are saved, got {interval!r}")
        trade_date = str(from_date)[:10]
        path = self._spot_dir / f"{trade_date}.csv"
        if not path.exists():
            return []  # same as Kite on a holiday / weekend
        frame = pd.read_csv(path)
        frame["date"] = pd.to_datetime(frame["date"])
        start, end = pd.Timestamp(str(from_date)), pd.Timestamp(str(to_date))
        naive = frame["date"].dt.tz_localize(None)
        frame = frame[(naive >= start) & (naive <= end)]
        return frame.to_dict("records")

    def __getattr__(self, name):
        # Anything else (orders, positions, quote, ltp, instruments...) must not be
        # needed by a DOWNLOADED backtest; fail loudly if the strategy starts using it.
        raise RuntimeError(f"fake kite: KiteConnect.{name} is not available in snapshot tests")


class KiteTicker:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("fake kite: KiteTicker is not available in snapshot tests")
