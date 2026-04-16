#!/usr/bin/env python3
"""
Code Review Agent -- runs forever on Orb Cloud.
Uses Claude Agent SDK (same pattern as SPOQ-Food).
"""

import asyncio
import os
import time
import traceback
from pathlib import Path
from claude_agent_sdk import (
    query, ClaudeAgentOptions,
    AssistantMessage, ResultMessage, SystemMessage,
    TextBlock, ToolUseBlock,
)

DATA_DIR = Path("/root/data")
LOGS_DIR = DATA_DIR / "logs"
PROMPT_FILE = Path("/root/src/agent-prompt.md")
SESSION_FILE = LOGS_DIR / "last_session.txt"
AGENT_ID = os.environ.get("ORB_COMPUTER_ID", "unknown")

LOGS_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "reviews").mkdir(parents=True, exist_ok=True)
(DATA_DIR / "repos").mkdir(parents=True, exist_ok=True)


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [agent] {msg}"
    print(line, flush=True)
    with open(LOGS_DIR / "agent.log", "a") as f:
        f.write(line + "\n")


def save_session(sid):
    if sid:
        SESSION_FILE.write_text(sid)


def load_session():
    if SESSION_FILE.exists():
        sid = SESSION_FILE.read_text().strip()
        return sid or None
    return None


def clear_session():
    SESSION_FILE.unlink(missing_ok=True)


async def run_agent():
    system_prompt = PROMPT_FILE.read_text()
    session_id = load_session()
    if session_id:
        log(f"Loaded session: {session_id}")
    run_num = 0

    while True:
        run_num += 1
        reviewed = 0
        try:
            reviewed_file = DATA_DIR / "reviewed_prs.txt"
            reviewed = len(reviewed_file.read_text().strip().split("\n")) if reviewed_file.exists() and reviewed_file.read_text().strip() else 0
        except:
            pass

        log(f"=== RUN #{run_num} | Reviewed so far: {reviewed} | Agent: {AGENT_ID} ===")

        if session_id:
            prompt = (
                f"Continue your code review work. Run #{run_num}. "
                f"You have reviewed {reviewed} PRs so far. "
                f"Check all your assigned repos for new PRs. "
                f"Review any unreviewed ones. "
                f"If all repos are clear, claim a new one from the API. "
                f"After checking everything, run 'sleep 30' and start over. "
                f"Never exit."
            )
            log(f"Resuming session {session_id}...")
            options = ClaudeAgentOptions(
                allowed_tools=[
                    "Bash", "Edit", "Read", "Write", "Glob", "Grep",
                    "WebFetch", "WebSearch"
                ],
                permission_mode="bypassPermissions",
                model="claude-sonnet-4-20250514",
                resume=session_id,
                cwd=str(DATA_DIR),
            )
        else:
            prompt = (
                f"You are a code review agent. Run #{run_num}. Agent ID: {AGENT_ID}. "
                f"Read the instructions below and follow them. Never exit.\n\n"
                f"{system_prompt}"
            )
            log("Starting FRESH session...")
            options = ClaudeAgentOptions(
                allowed_tools=[
                    "Bash", "Edit", "Read", "Write", "Glob", "Grep",
                    "WebFetch", "WebSearch"
                ],
                permission_mode="bypassPermissions",
                model="claude-sonnet-4-20250514",
                system_prompt="You are a code review agent that runs forever on Orb Cloud. You review PRs on open source repos, post comments, and claim new repos when idle. Never exit.",
                cwd=str(DATA_DIR),
            )

        try:
            log_file = LOGS_DIR / f"run_{run_num}.log"
            msg_count = 0
            with open(log_file, "a") as lf:
                async for message in query(prompt=prompt, options=options):
                    msg_count += 1
                    lf.write(f"[msg#{msg_count}] {type(message).__name__}: {repr(message)[:500]}\n")
                    lf.flush()
                    if msg_count <= 5:
                        log(f"msg#{msg_count} type={type(message).__name__}")
                    if isinstance(message, ResultMessage):
                        session_id = message.session_id
                        save_session(session_id)
                        log(f"Run complete. Session: {session_id}, "
                            f"Turns: {message.num_turns}, "
                            f"Cost: ${message.total_cost_usd or 0:.2f}")
                    elif isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, ToolUseBlock):
                                lf.write(f"[tool] {block.name}: {repr(block.input)[:150]}\n")
                                lf.flush()

        except KeyboardInterrupt:
            log("Interrupted")
            raise
        except Exception as e:
            log(f"Error: {e}")
            traceback.print_exc()
            if "session" in str(e).lower() or "resume" in str(e).lower():
                log("Session error - clearing and starting fresh")
                clear_session()
                session_id = None

        log(f"Run #{run_num} ended. Restarting in 5s...")
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run_agent())
