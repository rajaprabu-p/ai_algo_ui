"""Per-variant settings. Each module defines only what differs between variants:

    LOG_IDENTIFIER           appended to log/Excel file names and thread names
    TRAILING_PROFIT_STAGE1   [(profit_points, lock_points), ...] before TRAILING_PROFIT_STAGE2_TIME
    TRAILING_PROFIT_STAGE2   ladder from TRAILING_PROFIT_STAGE2_TIME onwards
    ENTRY_SCORE_THRESHOLD    minimum entry score for a CE/PE buy
    TRAIL_ON_CLOSE           optional (default False): drive the trailing ladder from the highest
                             completed candle close instead of the intra-minute high

To add a variant: copy v28.py to vNN.py, change the values, and add a
scripts/vNN_strategy_main.py launcher (and allow it in app.py's STRATEGY_SCRIPTS).
"""

from __future__ import annotations

import importlib
import re
from types import ModuleType

REQUIRED = ("LOG_IDENTIFIER", "TRAILING_PROFIT_STAGE1", "TRAILING_PROFIT_STAGE2", "ENTRY_SCORE_THRESHOLD")


def load(name: str) -> ModuleType:
    if not re.fullmatch(r"v\d+", str(name or "")):
        raise ValueError(f"invalid strategy variant name: {name!r}")
    module = importlib.import_module(f"{__name__}.{name}")
    missing = [key for key in REQUIRED if not hasattr(module, key)]
    if missing:
        raise AttributeError(f"strategy variant {name} is missing: {', '.join(missing)}")
    return module
