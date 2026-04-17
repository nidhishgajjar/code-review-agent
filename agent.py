#!/usr/bin/env python3
import json
import os
import pathlib
import re
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


SKIP_DIR_NAMES = {".git", "node_modules", "__pycache__", "dist", "build", ".venv", "vendor", "target", ".next", ".cache"}
MAX_TOOL_OUTPUT = 12_000
MAX_REVIEW_ROUNDS = 12


def _safe_join(local_repo: pathlib.Path, sub: str) -> pathlib.Path | None:
    sub = sub.lstrip("/").strip()
    if not sub or sub == ".":
        return local_repo
    p = (local_repo / sub).resolve()
    try:
        p.relative_to(local_repo.resolve())
    except ValueError:
        return None
    return p


def tool_list_dir(local_repo: pathlib.Path, path: str) -> str:
    p = _safe_join(local_repo, path)
    if p is None:
        return f"error: path '{path}' is outside the repo"
    if not p.exists():
        return f"error: '{path}' does not exist"
    if not p.is_dir():
        return f"error: '{path}' is a file, not a directory"
    items = []
    for item in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name)):
        if item.name in SKIP_DIR_NAMES:
            continue
        items.append(f"{'dir' if item.is_dir() else 'file'}  {item.name}")
    return "\n".join(items[:200]) or "(empty)"


def tool_read_file(local_repo: pathlib.Path, path: str, offset: int = 0, limit: int = 8000) -> str:
    p = _safe_join(local_repo, path)
    if p is None:
        return f"error: path '{path}' is outside the repo"
    if not p.is_file():
        return f"error: '{path}' is not a file"
    try:
        text = p.read_text(errors="replace")
    except Exception as e:
        return f"error reading '{path}': {e}"
    end = offset + limit
    chunk = text[offset:end]
    suffix = ""
    if end < len(text):
        suffix = f"\n\n[file continues, {len(text) - end} more chars; call again with offset={end} to read more]"
    if offset > 0:
        chunk = f"[showing chars {offset}-{min(end, len(text))} of {len(text)}]\n" + chunk
    return chunk + suffix


def tool_grep(local_repo: pathlib.Path, pattern: str, path: str = ".") -> str:
    base = _safe_join(local_repo, path)
    if base is None:
        return f"error: path '{path}' is outside the repo"
    if not base.exists():
        return f"error: '{path}' does not exist"
    try:
        rgx = re.compile(pattern)
    except re.error as e:
        return f"error: bad regex: {e}"
    targets = [base] if base.is_file() else list(base.rglob("*"))
    matches = []
    for f in targets:
        if not f.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in f.parts):
            continue
        try:
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if rgx.search(line):
                    rel = f.relative_to(local_repo)
                    matches.append(f"{rel}:{i}: {line.strip()[:240]}")
                    if len(matches) >= 60:
                        return "\n".join(matches) + f"\n[stopped at 60 matches; pattern={pattern!r}]"
        except Exception:
            continue
    return "\n".join(matches) if matches else f"(no matches for {pattern!r} under {path})"


REVIEW_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List entries in a repo directory. Use to discover what the project contains.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path relative to repo root. Use '.' for root."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the repo. Returns up to 8000 chars; pass offset to page further.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "Char offset to start at, default 0"},
                    "limit": {"type": "integer", "description": "Max chars to return, default 8000"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search the repo for a regex pattern. Returns up to 60 matches as 'path:line: snippet'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regex"},
                    "path": {"type": "string", "description": "Optional sub-path to limit search. Default '.'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_review",
            "description": "Submit the final review markdown. Call exactly once when you're done exploring.",
            "parameters": {
                "type": "object",
                "properties": {"body": {"type": "string", "description": "The full review markdown to post on the PR."}},
                "required": ["body"],
            },
        },
    },
]


REVIEW_SYSTEM = """You are a senior code reviewer. You have tools to explore the repository before reviewing the PR. Use them.

Approach:
  1. Get oriented. List the repo root, find docs and config the project actually uses, read what looks load-bearing for understanding the project's purpose and conventions.
  2. Understand the change. Read the files the diff touches in full. Follow the dependencies that matter — what imports them, what they import, where they are called.
  3. Form a real opinion. Think about correctness, security, concurrency, API contract, performance, and the project's own conventions. Don't manufacture issues to look thorough.
  4. When ready, call submit_review with the final markdown.

Final review markdown structure:

## Summary
What this PR does and why, in the context of the project.

## Architecture
How it fits the codebase. Reference specific files you read.

## Issues
Bullet list. Each: **[severity]** path:line — concrete problem — concrete fix. severity is critical/warning/suggestion. If you found nothing, say so plainly.

## Cross-file impact
Things elsewhere that this change affects or could break. Reference specific files. If nothing, say so.

## Assessment
approve / request-changes / comment. One sentence why.

Cite real file paths and line numbers from what you read. Be terse — every sentence earns its place. Don't include your exploration narrative in the final review."""


REVIEW_USER = """Review this pull request.

Repository: {repo}
PR #{number}: {title}
Author: {author}

--- DIFF ---
{diff}

Explore the repo with the tools, then submit_review."""


def call_llm_chat(messages: list[dict], tools: list[dict] | None = None) -> dict:
    body: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.2,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    r = requests.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/AWLSEN/code-review-agent",
            "X-Title": "code-review-agent",
        },
        json=body,
        timeout=240,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"LLM {r.status_code}: {r.text[:500]}")
    return r.json()["choices"][0]["message"]


def run_tool(local_repo: pathlib.Path, name: str, args: dict) -> str:
    try:
        if name == "list_dir":
            out = tool_list_dir(local_repo, args.get("path", "."))
        elif name == "read_file":
            out = tool_read_file(
                local_repo,
                args["path"],
                int(args.get("offset", 0) or 0),
                int(args.get("limit", 8000) or 8000),
            )
        elif name == "grep":
            out = tool_grep(local_repo, args["pattern"], args.get("path", "."))
        else:
            out = f"error: unknown tool '{name}'"
    except Exception as e:
        out = f"error executing {name}: {e}"
    return out[:MAX_TOOL_OUTPUT]


def generate_review_with_tools(repo: str, pr: dict, diff: str, local_repo: pathlib.Path) -> str:
    messages: list[dict] = [
        {"role": "system", "content": REVIEW_SYSTEM},
        {"role": "user", "content": REVIEW_USER.format(
            repo=repo, number=pr["number"], title=pr["title"],
            author=pr["user"]["login"], diff=diff,
        )},
    ]
    for round_idx in range(MAX_REVIEW_ROUNDS):
        msg = call_llm_chat(messages, tools=REVIEW_TOOLS)
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            content = msg.get("content") or ""
            if content.strip():
                log(f"{repo}#{pr['number']} model returned content without submit_review (round {round_idx + 1}); using as review")
                return content
            log(f"{repo}#{pr['number']} model returned empty without tool calls (round {round_idx + 1}); nudging")
            messages.append({"role": "user", "content": "Continue exploring, then call submit_review with the final markdown."})
            continue
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            log(f"{repo}#{pr['number']} round {round_idx + 1}: {name}({args})")
            if name == "submit_review":
                return args.get("body", "")
            result = run_tool(local_repo, name, args)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
    raise RuntimeError(f"review hit max rounds ({MAX_REVIEW_ROUNDS}) without submit_review")


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

    review = generate_review_with_tools(repo, pr, diff, local_repo)
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
