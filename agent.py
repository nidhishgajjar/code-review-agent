#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys
import time
import traceback
from typing import Any

import requests

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "google/gemini-2.5-flash")
AGENT_ID = int(os.environ.get("AGENT_ID", "1"))
NUM_AGENTS = int(os.environ.get("NUM_AGENTS", "2"))
ONE_SHOT = os.environ.get("ONE_SHOT") == "1"
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
WORKDIR = pathlib.Path(os.environ.get("WORKDIR", "/tmp/cra-work"))
STATE_DIR = pathlib.Path(os.environ.get("STATE_DIR", "./state"))
REPOS_FILE = pathlib.Path(os.environ.get("REPOS_FILE", "./repos.txt"))
MAX_DIFF_CHARS = 40_000
MAX_FILE_CHARS = 6_000

WORKDIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / f"agent-{AGENT_ID}.json"

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": f"code-review-agent/{AGENT_ID}",
}


def log(msg: str) -> None:
    print(f"[agent-{AGENT_ID} pid={os.getpid()}] {msg}", flush=True)


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        log(f"loaded state from {STATE_FILE}: {len(data.get('reviewed', {}))} reviewed")
        return data
    log(f"no state at {STATE_FILE}, starting fresh")
    return {"reviewed": {}}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)
    log(f"saved state to {STATE_FILE} ({len(state['reviewed'])} reviewed)")


def claim_repos() -> list[str]:
    lines = [ln.strip() for ln in REPOS_FILE.read_text().splitlines()]
    repos = [ln for ln in lines if ln and not ln.startswith("#")]
    return [r for i, r in enumerate(repos) if i % NUM_AGENTS == (AGENT_ID - 1) % NUM_AGENTS]


def gh_get(url: str, params: dict | None = None) -> Any:
    r = requests.get(url, headers=GH_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def gh_get_raw(url: str) -> str:
    r = requests.get(url, headers=GH_HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def gh_post(url: str, body: dict) -> Any:
    r = requests.post(url, headers=GH_HEADERS, json=body, timeout=30)
    if r.status_code >= 400:
        log(f"POST {url} -> {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json()


def clone_or_update(repo: str) -> pathlib.Path:
    local = WORKDIR / repo.replace("/", "__")
    if local.exists():
        try:
            subprocess.run(
                ["git", "-C", str(local), "fetch", "--depth=1", "origin"],
                check=True, capture_output=True, timeout=120,
            )
            subprocess.run(
                ["git", "-C", str(local), "reset", "--hard", "origin/HEAD"],
                check=False, capture_output=True, timeout=60,
            )
        except subprocess.CalledProcessError:
            subprocess.run(["rm", "-rf", str(local)], check=False)
    if not local.exists():
        subprocess.run(
            ["git", "clone", "--depth=1", f"https://github.com/{repo}.git", str(local)],
            check=True, capture_output=True, timeout=300,
        )
    return local


def list_open_prs(repo: str) -> list[dict]:
    return gh_get(f"{GITHUB_API}/repos/{repo}/pulls", {"state": "open", "per_page": 20})


def fetch_pr_diff(repo: str, number: int) -> str:
    url = f"{GITHUB_API}/repos/{repo}/pulls/{number}"
    r = requests.get(url, headers={**GH_HEADERS, "Accept": "application/vnd.github.v3.diff"}, timeout=60)
    r.raise_for_status()
    return r.text


def pr_already_reviewed(repo: str, pr_number: int, state: dict) -> bool:
    key = f"{repo}#{pr_number}"
    return key in state["reviewed"]


def mark_reviewed(repo: str, pr_number: int, state: dict, sha: str) -> None:
    state["reviewed"][f"{repo}#{pr_number}"] = {"sha": sha, "ts": int(time.time())}
    save_state(state)


def has_our_previous_comment(repo: str, pr_number: int, login: str) -> bool:
    comments = gh_get(f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments", {"per_page": 100})
    return any(c.get("user", {}).get("login") == login for c in comments)


def our_login() -> str:
    return gh_get(f"{GITHUB_API}/user")["login"]


def changed_files(diff: str) -> list[str]:
    files = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:])
    return files


REVIEW_TASK = """You are a senior code reviewer. Review this pull request thoroughly.

Repository: {repo}
PR #{number}: {title}
Author: {author}

--- DIFF ---
{diff}

You have terminal and file-editor tools. Use them to:
  1. Get oriented in the repo (the CWD is the cloned repo). Read docs/configs the project actually uses.
  2. Read the files the diff touches, in full. Follow the dependencies that matter, both what they import and what imports them.
  3. Form a real opinion: correctness, security, concurrency, API contract, performance, and the project's own conventions. Do not invent issues.

When you are done exploring, write the final review markdown to a file named REVIEW.md at the repo root, using exactly this structure:

## Summary
What this PR does and why, in the context of the project.

## Architecture
How it fits the codebase. Reference specific files you read.

## Issues
Bullet list. Each: **[severity]** path:line — concrete problem — concrete fix. severity is critical/warning/suggestion. If none, say so plainly.

## Cross-file impact
Things elsewhere that this change affects or could break. Reference specific files. If nothing, say so.

## Assessment
approve / request-changes / comment. One sentence why.

Be terse. Cite real file paths and line numbers. Do NOT include your exploration narrative in REVIEW.md, only the final review.

Do not commit, push, or modify any tracked files other than REVIEW.md. Do not run network commands beyond what the tools provide."""


def generate_review_with_openhands(repo: str, pr: dict, diff: str, local_repo: pathlib.Path) -> str:
    from openhands.sdk import LLM, Agent, Conversation, Tool
    from openhands.tools.file_editor import FileEditorTool
    from openhands.tools.terminal import TerminalTool

    review_path = local_repo / "REVIEW.md"
    if review_path.exists():
        review_path.unlink()

    llm = LLM(
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        usage_id=f"code-review-agent/{AGENT_ID}",
    )
    agent = Agent(
        llm=llm,
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
        ],
    )
    conversation = Conversation(agent=agent, workspace=str(local_repo))
    task = REVIEW_TASK.format(
        repo=repo, number=pr["number"], title=pr["title"],
        author=pr["user"]["login"], diff=diff,
    )
    log(f"{repo}#{pr['number']} handing off to OpenHands agent")
    conversation.send_message(task)
    conversation.run()

    if not review_path.exists():
        raise RuntimeError("OpenHands finished without writing REVIEW.md")
    review = review_path.read_text(errors="replace").strip()
    try:
        review_path.unlink()
    except Exception:
        pass
    return review


def post_review_comment(repo: str, pr_number: int, body: str) -> dict:
    url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
    return gh_post(url, {"body": body})


def review_pr(repo: str, pr: dict, local_repo: pathlib.Path, state: dict, login: str) -> bool:
    number = pr["number"]
    if pr_already_reviewed(repo, number, state):
        log(f"{repo}#{number} skip: in local state")
        return False
    if has_our_previous_comment(repo, number, login):
        log(f"{repo}#{number} skip: already commented on GitHub")
        mark_reviewed(repo, number, state, pr["head"]["sha"])
        return False

    log(f"{repo}#{number} reviewing: {pr['title']!r}")
    diff = fetch_pr_diff(repo, number)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated] ...\n"

    review = generate_review_with_openhands(repo, pr, diff, local_repo)
    if not review.strip():
        raise RuntimeError("empty review generated")
    footer = (
        "\n\n---\n_Automated review by "
        "[code-review-agent](https://github.com/AWLSEN/code-review-agent). "
        "May contain mistakes. Ignore or rebut as you see fit._"
    )
    post_review_comment(repo, number, review.strip() + footer)
    mark_reviewed(repo, number, state, pr["head"]["sha"])
    log(f"{repo}#{number} reviewed and posted")
    return True


def cycle(state: dict, login: str) -> int:
    repos = claim_repos()
    log(f"claimed {len(repos)} repos: {repos}")
    posted = 0
    for repo in repos:
        try:
            local = clone_or_update(repo)
            prs = list_open_prs(repo)
            log(f"{repo}: {len(prs)} open PRs")
            for pr in prs:
                try:
                    if review_pr(repo, pr, local, state, login):
                        posted += 1
                        if ONE_SHOT:
                            return posted
                except Exception as e:
                    log(f"{repo}#{pr['number']} error: {e}")
                    traceback.print_exc()
        except Exception as e:
            log(f"{repo} error: {e}")
            traceback.print_exc()
    return posted


def main() -> int:
    log(f"starting. model={LLM_MODEL} num_agents={NUM_AGENTS} one_shot={ONE_SHOT} state_dir={STATE_DIR} workdir={WORKDIR}")
    state = load_state()
    login = our_login()
    log(f"github login: {login}")
    cycle_num = 0
    while True:
        cycle_num += 1
        log(f"--- cycle {cycle_num} begin ---")
        try:
            posted = cycle(state, login)
        except Exception as e:
            log(f"cycle crashed: {e}")
            traceback.print_exc()
            posted = 0
        log(f"--- cycle {cycle_num} done, posted={posted} ---")
        if ONE_SHOT:
            return 0 if posted > 0 else 2
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
