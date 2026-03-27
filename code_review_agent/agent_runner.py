"""OpenHands agent runner — runs on ORB Cloud computer.

This script starts a full OpenHands CodeActAgent that:
1. Clones the repo
2. Checks out the PR branch
3. Explores the codebase using terminal + file editor tools
4. Reasons through the code changes step by step
5. Produces a thorough, context-aware review

Usage:
    python3 agent_runner.py review <repo_url> <pr_number> <model>
    python3 agent_runner.py scan <repo_url>

Output: JSON to stdout
"""

from __future__ import annotations

import json
import os
import sys
import ssl
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# GitHub helpers (stdlib — for PR metadata before agent runs)
# ---------------------------------------------------------------------------

def _github_headers():
    token = os.environ.get("GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github+json", "User-Agent": "code-review-agent"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        return json.loads(resp.read().decode())


def parse_repo(url):
    url = url.strip().rstrip("/")
    if not url.startswith("http") and "/" in url and "." not in url.split("/")[0]:
        return url
    parsed = urlparse(url if url.startswith("http") else f"https://{url}")
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    return f"{parts[0]}/{parts[1]}"


def list_prs(repo_url):
    slug = parse_repo(repo_url)
    url = f"https://api.github.com/repos/{slug}/pulls?state=open&sort=updated&direction=desc&per_page=100"
    prs = _http_get(url, _github_headers())
    return [
        {
            "number": p["number"],
            "title": p["title"],
            "author": p["user"]["login"],
            "branch": p["head"]["ref"],
            "base": p["base"]["ref"],
            "url": p["html_url"],
            "diff_url": p["diff_url"],
            "body": p.get("body") or "",
            "changed_files": p.get("changed_files", 0),
        }
        for p in prs
    ]


# ---------------------------------------------------------------------------
# OpenHands agent review
# ---------------------------------------------------------------------------

REVIEW_SYSTEM_PROMPT = """\
You are an expert code reviewer working inside a development environment.
You have access to a terminal and file editor. Use them to thoroughly
review the pull request.

Your workflow:
1. Run `git log --oneline {base}..{branch}` to see all commits
2. Run `git diff {base}...{branch} --stat` to see which files changed
3. For each changed file, read the relevant sections to understand context
4. If there are test files changed, read them to understand test coverage
5. Look at related code that isn't in the diff but is affected by changes
6. Check for potential issues: bugs, security, performance, readability

Produce a thorough review with:
- File-specific findings with line references
- Severity levels: critical / warning / suggestion
- Concrete fix recommendations with code snippets
- An overall assessment: approve / request-changes / comment

Be thorough but don't invent problems. If the code is good, say so.
Output your final review in markdown format.
"""


def run_agent_review(repo_url: str, pr_number: int, model: str) -> dict:
    """Run an OpenHands agent to review a PR."""
    # Redirect all logging to stderr so stdout stays clean for JSON
    import logging
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    for name in ["openhands", "litellm", "httpx", "httpcore"]:
        logging.getLogger(name).setLevel(logging.WARNING)

    from openhands.sdk import LLM, Conversation, LocalConversation
    from openhands.tools import get_default_agent

    # Get PR metadata
    slug = parse_repo(repo_url)
    prs = list_prs(repo_url)
    pr = next((p for p in prs if p["number"] == pr_number), None)
    if not pr:
        return {"error": f"PR #{pr_number} not found"}

    # Set up workspace — clone the repo
    workspace_dir = Path(f"/tmp/review-{slug.replace('/', '-')}-{pr_number}")
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Create LLM
    llm = LLM(
        model=model,
        api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY") or os.environ.get("LLM_API_KEY"),
        temperature=0.0,
    )

    # Create agent with terminal + file_editor only (no browser — Chromium not available)
    # Disable condenser to avoid "Failed to generate summary" errors
    from openhands.sdk import Agent, Tool
    agent = Agent(
        llm=llm,
        tools=[
            Tool(name="terminal"),
            Tool(name="file_editor"),
        ],
        condenser=None,
    )

    # Build the task message
    task = f"""\
Review pull request #{pr_number} in the repository {repo_url}.

PR Title: {pr['title']}
Author: @{pr['author']}
Branch: {pr['branch']} -> {pr['base']}

Description:
{pr['body']}

Steps:
1. First clone the repo: `git clone https://github.com/{slug}.git /tmp/repo && cd /tmp/repo`
2. Fetch the PR: `git fetch origin pull/{pr_number}/head:pr-{pr_number} && git checkout pr-{pr_number}`
3. See what changed: `git diff {pr['base']}...pr-{pr_number} --stat`
4. Read through each changed file and understand the context
5. Look at surrounding code that might be affected
6. Check for bugs, security issues, performance problems
7. Write a thorough code review in markdown

{REVIEW_SYSTEM_PROMPT.format(base=pr['base'], branch=f'pr-{pr_number}')}
"""

    # Run the OpenHands conversation
    conversation = Conversation(
        agent=agent,
        workspace=str(workspace_dir),
        max_iteration_per_run=30,
        visualizer=None,
    )

    try:
        # send_message queues the task, run() executes the agent loop
        conversation.send_message(task)
        conversation.run()

        # Extract the final agent response from conversation state
        # The agent uses FinishTool to signal completion with a message
        state = conversation._state if hasattr(conversation, '_state') else None
        if state and hasattr(state, 'agent_state'):
            agent_state = state.agent_state
            # Look for the finish message in agent state
            if isinstance(agent_state, dict):
                review_text = agent_state.get("finish_message", "")
                if not review_text:
                    review_text = str(agent_state)

        # Fallback: ask the agent for a summary
        if not review_text or len(review_text) < 50:
            review_text = conversation.ask_agent(
                "Based on the code review you just performed, provide your complete review findings in markdown format."
            )
    except Exception as e:
        review_text = f"Agent error: {e}"
    finally:
        try:
            conversation.close()
        except Exception:
            pass

    return {
        "pr": pr,
        "review_text": review_text,
        "posted": False,
        "mode": "openhands-agent",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 agent_runner.py <action> <repo_url> [pr_number] [model]", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]
    repo_url = sys.argv[2]
    model = "gemini/gemini-2.5-flash"

    # Output file: if set, write result there instead of stdout
    # (useful for long-running agent jobs that outlive exec timeout)
    output_file = os.environ.get("AGENT_OUTPUT_FILE")

    if action == "scan":
        result = json.dumps(list_prs(repo_url))
    elif action == "review":
        pr_number = int(sys.argv[3])
        if len(sys.argv) > 4:
            model = sys.argv[4]
        result = json.dumps(run_agent_review(repo_url, pr_number, model))
    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        sys.exit(1)

    if output_file:
        Path(output_file).write_text(result)
        print(f"Result written to {output_file}")
    else:
        print(result)
