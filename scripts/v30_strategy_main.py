"""v30 strategy launcher.

All strategy logic lives in strategy/engine.py (shared with v28); the settings
that make this v30 live in strategy/variants/v30.py.

    py scripts/v30_strategy_main.py --mode BACKTEST --date 2026-10-14
    py scripts/v30_strategy_main.py --mode PAPERTRADE_LIVE
"""

import sys

if __name__ == "__main__":
    from strategy import runtime

    runtime.configure(variant="v30", argv=sys.argv[1:])
    from strategy import engine

    engine.main()
