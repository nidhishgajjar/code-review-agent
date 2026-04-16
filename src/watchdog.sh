#!/bin/bash
# Watchdog for the code review agent.
# Same pattern as SPOQ-Food: runs agent.py forever, restarts on crash.
set -uo pipefail

PROJECT_DIR="/root"
LOG_DIR="$PROJECT_DIR/data/logs"
LOCK_FILE="$LOG_DIR/watchdog.lock"
STALL_TIMEOUT=3600  # 1 hour with no new reviews = stall

mkdir -p "$LOG_DIR"

if [ -f "$LOCK_FILE" ]; then
    EXISTING_PID=$(cat "$LOCK_FILE")
    if [ "$EXISTING_PID" != "1" ] && [ "$EXISTING_PID" != "$$" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
        echo "[watchdog] Already running (PID: $EXISTING_PID). Exiting."
        exit 1
    fi
    rm -f "$LOCK_FILE"
fi
echo $$ > "$LOCK_FILE"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [watchdog] $1" | tee -a "$LOG_DIR/watchdog.log"
}

cleanup() {
    log "Shutting down..."
    if [ -f "$LOG_DIR/agent.pid" ]; then
        AGENT_PID=$(cat "$LOG_DIR/agent.pid")
        kill "$AGENT_PID" 2>/dev/null
        sleep 2
        kill -9 "$AGENT_PID" 2>/dev/null
        rm -f "$LOG_DIR/agent.pid"
    fi
    rm -f "$LOCK_FILE"
    exit 0
}
trap cleanup SIGINT SIGTERM

log "============================================"
log "Code Review Agent -- Claude Agent SDK"
log "Auto-compaction enabled. Runs indefinitely."
log "Stall detection: restart if idle >${STALL_TIMEOUT}s."
log "============================================"

while true; do
    log "Starting agent..."

    python3 "$PROJECT_DIR/src/agent.py" &
    AGENT_PID=$!
    echo "$AGENT_PID" > "$LOG_DIR/agent.pid"
    log "Agent PID: $AGENT_PID"

    # Monitor for stalls
    LAST_ACTIVITY=$(date +%s)

    while kill -0 "$AGENT_PID" 2>/dev/null; do
        sleep 60

        # Check for recent activity (new review files or log updates)
        RECENT=$(find "$PROJECT_DIR/data/reviews" -type f -mmin -5 2>/dev/null | head -1)
        RECENT_LOG=$(find "$LOG_DIR" -name "run_*.log" -mmin -5 2>/dev/null | head -1)

        if [ -n "$RECENT" ] || [ -n "$RECENT_LOG" ]; then
            LAST_ACTIVITY=$(date +%s)
        fi

        NOW=$(date +%s)
        IDLE_TIME=$(( NOW - LAST_ACTIVITY ))

        if [ "$IDLE_TIME" -ge "$STALL_TIMEOUT" ]; then
            log "STALL: No activity in $(( IDLE_TIME / 60 ))m. Restarting..."
            kill "$AGENT_PID" 2>/dev/null
            sleep 2
            kill -9 "$AGENT_PID" 2>/dev/null
            break
        fi
    done

    wait "$AGENT_PID" 2>/dev/null
    EXIT_CODE=$?
    log "Agent exited (code: $EXIT_CODE)"

    log "Restarting in 10s..."
    sleep 10
done
