"""
Diagnostic script to identify why papertrade/livetrade take no trades
while backtest shows trades.

This script helps debug the discrepancy between backtest and live modes.
"""

import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Check the backtest output to see what trades should be possible
backtest_file = "backtest_02072026.xlsx"
try:
    trades_df = pd.read_excel(backtest_file, sheet_name="Trades")
    print(f"\n✓ Backtest trades found: {len(trades_df)}")
    print("\nBacktest trade details:")
    print(trades_df[["entry_time", "entry_reason", "side", "option_symbol", "pnl_rs"]].to_string())
    
    if not trades_df.empty:
        first_trade = trades_df.iloc[0]
        print(f"\nFirst trade entry time: {first_trade.get('entry_time')}")
        print(f"Entry reason: {first_trade.get('entry_reason')}")
        print(f"Entry score: {first_trade.get('entry_score', 'N/A')}")
except Exception as e:
    print(f"✗ Could not read backtest file: {e}")

# Check papertrade output
papertrade_file = "papertrade_02072026_150000_v29_50pt_backtest.xlsx"
try:
    pt_trades = pd.read_excel(papertrade_file, sheet_name="Trades")
    print(f"\n✓ Papertrade trades found: {len(pt_trades)}")
    if not pt_trades.empty:
        print(pt_trades.to_string())
    else:
        print("  NO TRADES in papertrade output")
except Exception as e:
    print(f"✗ Papertrade: {e}")

# Check livetrade output
livetrade_file = "livetrade_02072026_110208_v29_50pt_backtest.xlsx"
try:
    lt_trades = pd.read_excel(livetrade_file, sheet_name="Trades")
    print(f"\n✓ Livetrade trades found: {len(lt_trades)}")
    if not lt_trades.empty:
        print(lt_trades.to_string())
    else:
        print("  NO TRADES in livetrade output")
except Exception as e:
    print(f"✗ Livetrade: {e}")

print("\n" + "="*80)
print("DIAGNOSIS:")
print("="*80)
print("""
Possible causes for no trades in papertrade/livetrade:

1. **MISSING HISTORICAL BOOTSTRAP**
   - When app starts mid-day, it tries to fetch full-day history
   - If fetch fails (API rate limit, network, etc.), ce_rows/pe_rows stay empty
   - Strategy eval is skipped (guard at finalize_completed_minutes)
   
2. **WEBSOCKET DATA NOT ARRIVING**
   - Even with historical data, if websocket connection is slow, 
     no ticks = no new candles = strategy never fires
   
3. **ENTRY BEFORE APP START TIME**
   - If ALLOW_ENTRY_ONLY_AFTER_APP_START=True, entries before app start are blocked
   - Backtest might find entry at 09:25, but live starts at 10:15
   
4. **INSUFFICIENT CANDLE DATA**
   - Some strategies need N candles of history to warm up indicators
   - If bootstrap is empty, warming up takes too long

5. **MARKET HOURS CHECK**
   - Live/Papertrade might detect outside market hours (9:15-15:30)
   - Backtest uses historical data regardless of time

RECOMMENDED CHECKS:
1. Look at the "Day" sheet in papertrade/livetrade Excel - does it have candle data?
2. Enable more logging to see if bootstrap fetch succeeds
3. Check if websocket connects ("KiteTicker connected" message)
4. Verify entry_time in backtest is after app start time in live session
5. Check if entry_score < ENTRY_SCORE_THRESHOLD in live data
""")
