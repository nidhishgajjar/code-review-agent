#!/usr/bin/env python3
"""
Webhook-driven code review agent.

GitHub pull_request webhook -> this Flask server (exposed via Orb public URL)
-> OpenHands review -> comment posted back on the PR.

No polling. Between webhooks the Orb sandbox freezes, so we only burn runtime
when actually reviewing.
"""
import hashlib
import hmac
import json
import os
import pathlib
import subprocess
import threading
import time
import traceback
from typing import Any

import requests
from flask import Flask, abort, request

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_WEBHOOK_SECRET = os.environ["GITHUB_WEBHOOK_SECRET"].encode()
LLM_API_KEY = os.environ["LLM_API_KEY"]
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.z.ai/api/anthropic")
LLM_MODEL = os.environ.get("LLM_MODEL", "anthropic/glm-4.6")
PORT = int(os.environ.get("PORT", "8080"))
WORKDIR = pathlib.Path(os.environ.get("WORKDIR", "/tmp/cra-work"))
STATE_DIR = pathlib.Path(os.environ.get("STATE_DIR", "./state"))
REPOS_FILE = pathlib.Path(os.environ.get("REPOS_FILE", "./repos.txt"))
OSS_REPOS_FILE = pathlib.Path(os.environ.get("OSS_REPOS_FILE", "./oss-repos.txt"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
MAX_DIFF_CHARS = 40_000

WORKDIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "agent.json"
STATE_LOCK = threading.Lock()
REVIEW_LOCK = threading.Lock()  # serialize reviews so we don't fight for RAM

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "code-review-agent",
}

TRIGGER_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


def log(msg: str) -> None:
    print(f"[agent pid={os.getpid()}] {msg}", flush=True)


def load_state() -> dict[str, Any]:
    with STATE_LOCK:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
        return {"reviewed": {}}


def save_state(state: dict[str, Any]) -> None:
    with STATE_LOCK:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(STATE_FILE)


def _read_repo_list(path: pathlib.Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln)
    return out


def _from_env(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [r.strip() for r in raw.split(",") if r.strip()]


# Per-agent repo assignment, keyed by the 8-char prefix of the Orb computer id
# (which appears in PUBLIC_URL as https://{cid8}.orbcloud.dev). We put this in
# code rather than env vars because Orb's `org_secrets` are scoped at the org
# level — meaning per-agent env vars can collide across deploys. Each agent
# reads its own PUBLIC_URL at startup and picks its slice.
#
# To re-assign: edit this map and push. The agents will pick up the new lists
# the next time they restart (which auto-deploy will do).
ASSIGNMENTS: dict[str, dict[str, list[str]]] = {
    "fee58b7d": {  # code-review-agent-1 — Python
        "own": [],
        "oss": ["simonw/datasette", "encode/httpx", "pydantic/pydantic-ai", "python-poetry/poetry"],
    },
    "ba6b1fa0": {  # code-review-agent-2 — Go
        "own": [],
        "oss": ["charmbracelet/bubbletea", "charmbracelet/lipgloss", "caddyserver/caddy", "spf13/cobra"],
    },
    "894a845c": {  # code-review-agent-3 — JS + Rust
        "own": [],
        "oss": ["pmndrs/zustand", "vercel/swr", "tokio-rs/axum", "BurntSushi/ripgrep"],
    },
}


def _my_cid8() -> str:
    """Extract the 8-char Orb computer id prefix from PUBLIC_URL."""
    if not PUBLIC_URL:
        return ""
    rest = PUBLIC_URL.split("//", 1)[-1]
    return rest.split(".", 1)[0][:8]


def _assignment(kind: str) -> list[str] | None:
    cid8 = _my_cid8()
    if cid8 and cid8 in ASSIGNMENTS:
        return ASSIGNMENTS[cid8][kind]
    return None


def own_repos() -> list[str]:
    """Repos we own/admin. Bootstrap registers GitHub webhooks here.
    Resolution order: in-code ASSIGNMENTS (by CID), then OWN_REPOS env, then repos.txt."""
    a = _assignment("own")
    if a is not None:
        return a
    env = _from_env("OWN_REPOS")
    return env if env else _read_repo_list(REPOS_FILE)


def oss_repos() -> list[str]:
    """OSS repos we don't admin. Pinged via synthetic webhook from an external poller.
    Resolution order: in-code ASSIGNMENTS (by CID), then OSS_REPOS env, then oss-repos.txt."""
    a = _assignment("oss")
    if a is not None:
        return a
    env = _from_env("OSS_REPOS")
    return env if env else _read_repo_list(OSS_REPOS_FILE)


def allowlisted_repos() -> set[str]:
    return set(own_repos()) | set(oss_repos())


def gh_get(url: str, params: dict | None = None) -> Any:
    r = requests.get(url, headers=GH_HEADERS, params=params, timeout=30)
    if r.status_code >= 400:
        log(f"GH {r.status_code} {url} body={r.text[:400]!r} headers={ {k:v for k,v in r.headers.items() if k.lower().startswith(('x-rate','x-github','retry-after'))} }")
    r.raise_for_status()
    return r.json()


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


def fetch_pr_diff(repo: str, number: int) -> str:
    url = f"{GITHUB_API}/repos/{repo}/pulls/{number}"
    r = requests.get(url, headers={**GH_HEADERS, "Accept": "application/vnd.github.v3.diff"}, timeout=60)
    r.raise_for_status()
    return r.text


def pr_already_reviewed(repo: str, pr_number: int, state: dict) -> bool:
    return f"{repo}#{pr_number}" in state["reviewed"]


def mark_reviewed(repo: str, pr_number: int, state: dict, sha: str) -> None:
    state["reviewed"][f"{repo}#{pr_number}"] = {"sha": sha, "ts": int(time.time())}
    save_state(state)


def has_our_previous_comment(repo: str, pr_number: int, login: str) -> bool:
    comments = gh_get(f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments", {"per_page": 100})
    return any(c.get("user", {}).get("login") == login for c in comments)


def our_login() -> str:
    return gh_get(f"{GITHUB_API}/user")["login"]


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
        usage_id="code-review-agent",
    )
    agent = Agent(
        llm=llm,
        tools=[Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name)],
    )
    conversation = Conversation(agent=agent, workspace=str(local_repo))
    task = REVIEW_TASK.format(
        repo=repo, number=pr["number"], title=pr["title"],
        author=pr["user"]["login"], diff=diff,
    )
    log(f"{repo}#{pr['number']} handing off to OpenHands")
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


def do_review(repo: str, pr: dict) -> None:
    number = pr["number"]
    state = load_state()
    login = our_login()

    if pr_already_reviewed(repo, number, state):
        log(f"{repo}#{number} skip: in local state")
        return
    if has_our_previous_comment(repo, number, login):
        log(f"{repo}#{number} skip: already commented on GitHub")
        mark_reviewed(repo, number, state, pr["head"]["sha"])
        return

    log(f"{repo}#{number} reviewing: {pr['title']!r}")
    local = clone_or_update(repo)
    diff = fetch_pr_diff(repo, number)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated] ...\n"

    review = generate_review_with_openhands(repo, pr, diff, local)
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


def review_worker(repo: str, pr: dict) -> None:
    with REVIEW_LOCK:
        try:
            do_review(repo, pr)
        except Exception as e:
            log(f"review crashed for {repo}#{pr.get('number')}: {e}")
            traceback.print_exc()


app = Flask(__name__)


def verify_signature(body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    mac = hmac.new(GITHUB_WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, header)


@app.get("/health")
def health():
    return {"ok": True, "allowlist": sorted(allowlisted_repos())}


@app.post("/webhook")
def webhook():
    body = request.get_data()
    if not verify_signature(body, request.headers.get("X-Hub-Signature-256")):
        log("webhook rejected: bad signature")
        abort(401)

    event = request.headers.get("X-GitHub-Event", "")
    delivery = request.headers.get("X-GitHub-Delivery", "?")

    if event == "ping":
        log(f"webhook ping {delivery}")
        return {"pong": True}

    if event != "pull_request":
        return {"ignored": event}, 202

    payload = request.get_json(silent=True) or {}
    action = payload.get("action", "")
    pr = payload.get("pull_request") or {}
    repo = (payload.get("repository") or {}).get("full_name", "")

    if action not in TRIGGER_ACTIONS:
        return {"ignored_action": action}, 202

    if pr.get("draft"):
        return {"ignored": "draft"}, 202

    allow = allowlisted_repos()
    if allow and repo not in allow:
        log(f"webhook {delivery} for non-allowlisted repo {repo!r} — dropping")
        return {"ignored": "not_allowlisted", "repo": repo}, 202

    log(f"webhook {delivery} {event}.{action} {repo}#{pr.get('number')}")
    t = threading.Thread(
        target=review_worker,
        args=(repo, pr),
        name=f"review-{repo}#{pr.get('number')}",
        daemon=True,
    )
    t.start()
    return {"accepted": True, "repo": repo, "pr": pr.get("number")}, 202


def register_webhooks_if_enabled() -> None:
    """For each owned repo, ensure a webhook points at our public URL.
    OSS repos are intentionally skipped — we don't admin them; they get
    synthetic webhooks from an external poller instead."""
    if not PUBLIC_URL:
        log("PUBLIC_URL not set, skipping webhook bootstrap")
        return
    url = f"{PUBLIC_URL.rstrip('/')}/webhook"
    secret = GITHUB_WEBHOOK_SECRET.decode()
    for repo in sorted(own_repos()):
        try:
            hooks = gh_get(f"{GITHUB_API}/repos/{repo}/hooks", {"per_page": 100})
        except Exception as e:
            log(f"bootstrap: list hooks failed for {repo}: {e}")
            continue
        existing = next((h for h in hooks if (h.get("config") or {}).get("url") == url), None)
        config = {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}
        if existing:
            # ensure it's active and only subscribes to pull_request
            try:
                gh_post(f"{GITHUB_API}/repos/{repo}/hooks/{existing['id']}/pings", {})
                log(f"bootstrap: {repo} hook already present (id={existing['id']}), pinged")
            except Exception as e:
                log(f"bootstrap: ping failed for {repo}: {e}")
            continue
        try:
            created = gh_post(f"{GITHUB_API}/repos/{repo}/hooks", {
                "name": "web",
                "active": True,
                "events": ["pull_request"],
                "config": config,
            })
            log(f"bootstrap: created hook on {repo} id={created['id']}")
        except Exception as e:
            log(f"bootstrap: create hook failed for {repo}: {e}")


def main() -> int:
    log(f"starting webhook server. model={LLM_MODEL} port={PORT} public_url={PUBLIC_URL or '(unset)'}")
    cid8 = _my_cid8()
    log(f"my cid prefix: {cid8 or '(unknown)'} {'[in ASSIGNMENTS]' if cid8 in ASSIGNMENTS else '[no assignment, using env/file fallback]'}")
    log(f"own repos (bootstrap registers webhooks): {own_repos()}")
    log(f"oss repos (external poller): {oss_repos()}")
    # Diagnostic: confirm which secrets actually made it into env (first/last chars only).
    def _peek(k: str) -> str:
        v = os.environ.get(k, "")
        if not v: return "(empty)"
        if len(v) <= 8: return f"<short:{len(v)}>"
        return f"{v[:6]}...{v[-4:]} (len={len(v)})"
    log(f"env GITHUB_TOKEN={_peek('GITHUB_TOKEN')}")
    log(f"env LLM_API_KEY={_peek('LLM_API_KEY')}")
    log(f"env GITHUB_WEBHOOK_SECRET={_peek('GITHUB_WEBHOOK_SECRET')}")
    log(f"github login: {our_login()}")
    register_webhooks_if_enabled()
    # Use waitress if available for a production-ish WSGI server; fall back to flask dev.
    try:
        from waitress import serve
        log(f"serving with waitress on 0.0.0.0:{PORT}")
        serve(app, host="0.0.0.0", port=PORT, threads=4)
    except ImportError:
        log(f"waitress not installed; using flask dev server on 0.0.0.0:{PORT}")
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
