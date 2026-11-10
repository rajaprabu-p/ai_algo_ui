"""v30 settings (everything else is shared in strategy/engine.py)."""

LOG_IDENTIFIER = "v30"

# TRAILING_PROFIT_STAGE1 = [(50,45),(60,55),(70,65),(80,75),(90,85),(100,95)]
# TRAILING_PROFIT_STAGE2 = [(25,20),(30,25),(40,35),(50,45),(60,55),(70,65),(80,75),(90,85),(100,95)]
TRAILING_PROFIT_STAGE1 = [(25,15),(30,20),(35,25),(40,30),(45,35),(50,40),(55,45),(60,50),(65,55),(70,60),(75,65),(80,70),(85,75),(90,80),(95,85),(100,90)]
# v30 uses the same ladder all day. Stage 2 is a separate copy (not the same list
# object) so logs label it S1 before TRAILING_PROFIT_STAGE2_TIME and S2 after.
# Verified 2026-11-10: trades identical to the shared-list version on all 24 snapshot days.
TRAILING_PROFIT_STAGE2 = list(TRAILING_PROFIT_STAGE1)

ENTRY_SCORE_THRESHOLD = 4
