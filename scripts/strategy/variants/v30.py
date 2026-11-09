"""v30 settings (everything else is shared in strategy/engine.py)."""

LOG_IDENTIFIER = "v30"

# TRAILING_PROFIT_STAGE1 = [(50,45),(60,55),(70,65),(80,75),(90,85),(100,95)]
# TRAILING_PROFIT_STAGE2 = [(25,20),(30,25),(40,35),(50,45),(60,55),(70,65),(80,75),(90,85),(100,95)]
TRAILING_PROFIT_STAGE1 = [(25,15),(30,20),(35,25),(40,30),(45,35),(50,40),(55,45),(60,50),(65,55),(70,60),(75,65),(80,70),(85,75),(90,80),(95,85),(100,90)]
# Keep this as the SAME list object, not a copy: the engine checks
# `active_stages is TRAILING_PROFIT_STAGE2` (status labels, stage-2 peak tracking).
TRAILING_PROFIT_STAGE2 = TRAILING_PROFIT_STAGE1

ENTRY_SCORE_THRESHOLD = 4
