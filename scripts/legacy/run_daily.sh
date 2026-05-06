#!/bin/bash
# Trading bot daily runner
# Use separate cron entries for each script at its target time:
#   35 9  * * 1-5  /path/to/run_daily.sh research
#   45 9  * * 1-5  /path/to/run_daily.sh trade
#   05 16 * * 1-5  /path/to/run_daily.sh manage
#   15 16 * * 1-5  /path/to/run_daily.sh journal
#
# Or run all sequentially (for manual use only):
#   /path/to/run_daily.sh all

DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$DIR/venv/bin/python"
LOG="$DIR/journal/cron_$(date +%Y-%m-%d).log"

STEP="${1:-all}"

log_run() {
    echo "=== $(date) - $1 ===" >> "$LOG"
    "$VENV" "$DIR/scripts/$1.py" >> "$LOG" 2>&1
    echo "Exit code: $?" >> "$LOG"
}

case "$STEP" in
    research)    log_run research ;;
    trade)       log_run trade ;;
    manage)      log_run manage ;;
    performance) log_run performance ;;
    journal)     log_run journal ;;
    all)
        log_run research
        log_run trade
        log_run manage
        log_run performance
        log_run journal
        ;;
    *)
        echo "Usage: $0 {research|trade|manage|performance|journal|all}"
        exit 1
        ;;
esac
