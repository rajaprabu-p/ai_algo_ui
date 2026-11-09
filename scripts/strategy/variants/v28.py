"""v28 settings (everything else is shared in strategy/engine.py)."""

LOG_IDENTIFIER = "v28"

# (30,10): lock +10 once a trade reaches +30, so early runners that reverse do not become full losses.
TRAILING_PROFIT_STAGE1 = [(30,10),(50,45),(60,55),(70,65),(80,75),(90,85),(100,95)]
TRAILING_PROFIT_STAGE2 = [(25,20),(30,25),(40,35),(50,45),(60,55),(70,65),(80,75),(90,85),(100,95)]
# TRAILING_PROFIT_STAGE1 = [(25,15),(30,20),(35,25),(40,30),(45,35),(50,40),(55,45),(60,50),(65,55),(70,60),(75,65),(80,70),(85,75),(90,80),(95,85),(100,90)]
# TRAILING_PROFIT_STAGE2 = TRAILING_PROFIT_STAGE1

ENTRY_SCORE_THRESHOLD = 5
