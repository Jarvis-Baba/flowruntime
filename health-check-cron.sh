#!/bin/bash
# Health check cron wrapper — tracks consecutive failures
# State file: /tmp/health-check-failures.txt (just an integer)
# On 0 failures: do nothing
# On 1-2 failures: log, do nothing
# On 3 failures: push alert via signal bus
set -euo pipefail

STATE_FILE=/tmp/health-check-failures.txt
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SIGNAL_BUS="$HOME/cc-workspace/signal_bus.py"
PREV=0

# Read previous failure count
if [ -f "$STATE_FILE" ]; then
    PREV=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
fi

# Run health check
if bash "$HOME/health-check.sh" > /tmp/health-check-last.log 2>&1; then
    # Success — reset counter
    echo 0 > "$STATE_FILE"
    if [ "$PREV" -ge 3 ]; then
        python3 "$SIGNAL_BUS" write \
            --type status \
            --topic health-check \
            --from system \
            --to OC \
            --ball-with OC \
            --priority normal \
            --summary "Health check recovered after $PREV consecutive failures"
    fi
    exit 0
else
    # Failure — increment counter
    NEW=$((PREV + 1))
    echo "$NEW" > "$STATE_FILE"

    if [ "$NEW" -ge 3 ]; then
        FAIL_LOG=$(tail -20 /tmp/health-check-last.log | sed 's/"/\\"/g' | tr '\n' '|')
        python3 "$SIGNAL_BUS" write \
            --type alert \
            --topic health-check \
            --from system \
            --to OC \
            --ball-with OC \
            --priority critical \
            --summary "Health check failed $NEW times consecutively: $FAIL_LOG"
        echo "[ALERT] Health check failed $NEW times consecutively — pushed to signal bus" >&2
    else
        echo "[WARN] Health check failed ($NEW/$PREV consecutive)" >&2
    fi
    exit 1
fi
