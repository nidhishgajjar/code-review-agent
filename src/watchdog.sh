#!/bin/bash
# Watchdog for the code review agent.
# Same pattern as SPOQ-Food: loop, restart on crash, resume sessions.
set -uo pipefail

DATA_DIR="/root/data"
LOGS_DIR="$DATA_DIR/logs"
SESSION_FILE="$DATA_DIR/last_session.txt"
REVIEWED_FILE="$DATA_DIR/reviewed_prs.txt"
PROMPT_FILE="/root/src/agent-prompt.md"
STALL_THRESHOLD=3600  # 1 hour with no new reviews = stall
BACKOFF=30

mkdir -p "$LOGS_DIR" "$DATA_DIR/reviews"
touch "$REVIEWED_FILE"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOGS_DIR/watchdog.log"; }

log "Watchdog starting for repo: ${GITHUB_REPO:-unset}"

# Build the task prompt with repo-specific context
build_task() {
    local repo="$GITHUB_REPO"
    local reviewed_count=$(wc -l < "$REVIEWED_FILE" 2>/dev/null || echo 0)
    local session_id=$(cat "$SESSION_FILE" 2>/dev/null || echo "")

    if [ -n "$session_id" ]; then
        echo "You are continuing your work as a code reviewer for https://github.com/$repo. You have reviewed $reviewed_count PRs so far. Check for new open PRs, review any you haven't reviewed yet, and post comments. Check $DATA_DIR/reviewed_prs.txt to see which PRs you've already reviewed."
    else
        cat "$PROMPT_FILE" | sed "s|{GITHUB_REPO}|$repo|g"
    fi
}

run_cycle() {
    local session_id=$(cat "$SESSION_FILE" 2>/dev/null || echo "")
    local task=$(build_task)
    local logfile="$LOGS_DIR/run-$(date -u '+%Y%m%d-%H%M%S').log"

    log "Starting review cycle (session: ${session_id:-new})"

    local args=(--headless --override-with-envs)
    if [ -n "$session_id" ]; then
        args+=(--resume "$session_id")
    fi
    args+=(-t "$task")

    # Run OpenHands, capture output
    timeout 1800 openhands "${args[@]}" > "$logfile" 2>&1
    local exit_code=$?

    # Extract session ID from output for resume
    local new_session=$(grep -oP 'Conversation ID: \K[a-f0-9]+' "$logfile" | tail -1)
    if [ -n "$new_session" ]; then
        # Format as UUID for --resume
        local formatted="${new_session:0:8}-${new_session:8:4}-${new_session:12:4}-${new_session:16:4}-${new_session:20}"
        echo "$formatted" > "$SESSION_FILE"
        log "Session saved: $formatted"
    fi

    log "Cycle finished (exit=$exit_code)"
    return $exit_code
}

# Main loop
while true; do
    run_cycle
    exit_code=$?

    if [ $exit_code -eq 0 ]; then
        log "Clean exit. Sleeping 5 minutes before next PR check..."
        # This sleep is the idle window where Orb checkpoints the agent to NVMe.
        # The agent uses zero RAM during this time. When the sleep ends (or a
        # webhook wakes it), the next review cycle begins.
        sleep 300
    else
        log "Crashed (exit=$exit_code). Restarting in ${BACKOFF}s..."
        sleep $BACKOFF
    fi
done
