#!/bin/bash
# Smart trading bot runner — automatically picks the right routine based on current time.
# Usage: ./run.sh [growth|conservative]  (defaults to growth)

cd "$(dirname "$0")"

BOT="${1:-growth}"
HOUR=$(TZ="America/New_York" date +%H)
MIN=$(TZ="America/New_York" date +%M)
NOW_ET=$(TZ="America/New_York" date +%H:%M)
TIME_MINS=$((10#$HOUR * 60 + 10#$MIN))

echo "┌─────────────────────────────────────┐"
echo "│  Trading Bot Runner                 │"
echo "│  Bot: $BOT"
echo "│  ET Time: $NOW_ET"

# Before 9:30 ET
if [ $TIME_MINS -lt 570 ]; then
    echo "│  ⏳ Market not open yet             │"
    echo "└─────────────────────────────────────┘"
    exit 0

# 9:30 - 11:00 ET → Morning routine (research + trade)
elif [ $TIME_MINS -lt 660 ]; then
    echo "│  🌅 Running: MORNING routine        │"
    echo "└─────────────────────────────────────┘"
    ANTHROPIC_API_KEY="" python3 scripts/orchestrator.py morning "$BOT"

# 11:00 - 16:00 ET → Midday manage
elif [ $TIME_MINS -lt 960 ]; then
    echo "│  📈 Running: MIDDAY manage          │"
    echo "└─────────────────────────────────────┘"
    python3 scripts/manage_growth.py

# 16:00+ ET → EOD routine (manage + performance + journal)
else
    echo "│  🌆 Running: EOD routine            │"
    echo "└─────────────────────────────────────┘"
    ANTHROPIC_API_KEY="" python3 scripts/orchestrator.py afternoon "$BOT"
fi

