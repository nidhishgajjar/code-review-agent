#!/bin/bash
# Watchdog for the code review agent.
# Runs OpenHands in a loop. Each cycle: check PRs, review, exit.
# Restarts with --resume so the agent keeps its memory across cycles.
set -uo pipefail

DATA_DIR="/root/data"
LOGS_DIR="$DATA_DIR/logs"
SESSION_FILE="$DATA_DIR/last_session.txt"
PROMPT_FILE="/root/src/agent-prompt.md"

mkdir -p "$LOGS_DIR" "$DATA_DIR/reviews"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*" | tee -a "$LOGS_DIR/watchdog.log"; }

log "Watchdog starting for repo: ${GITHUB_REPO:-unset}"

build_task() {
    local repo="$GITHUB_REPO"
    local session_id=$(cat "$SESSION_FILE" 2>/dev/null || echo "")

    if [ -n "$session_id" ]; then
        echo "Continue reviewing https://github.com/$repo. Check for new open PRs you haven't reviewed yet. If there are none, say 'No new PRs to review' and finish."
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

    timeout 1800 openhands "${args[@]}" > "$logfile" 2>&1
    local exit_code=$?

    # Extract session ID for resume
    local new_session=$(grep -oP 'Conversation ID: \K[a-f0-9]+' "$logfile" | tail -1)
    if [ -n "$new_session" ]; then
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

    log "Restarting in 30s..."
    sleep 30
done
