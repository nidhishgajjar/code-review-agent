"""Stateful code review agent — async HTTP server on ORB Cloud.

Long-running reviews run in a background thread. The HTTP server stays responsive.

    POST /              — submit task, returns {"job_id": "..."}
    GET  /jobs/<id>     — poll job status
    GET  /health        — health check
    GET  /memory        — accumulated learnings
    GET  /reviews/<repo> — saved review history

OpenHands agent mode: clones repo, explores files, multi-step reasoning.
Simple mode: single LLM call on the diff (fast fallback).
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import urlparse

AGENT_DIR = Path("/agent/code")
REPOS_DIR = AGENT_DIR / "repos"
REVIEWS_DIR = AGENT_DIR / "reviews"
MEMORY_FILE = AGENT_DIR / "memory.md"
PORT = 8000

# ---------------------------------------------------------------------------
# Job tracking (in-memory, survives across requests)
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def create_job(action: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "action": action,
            "status": "running",
            "result": None,
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    return job_id


def complete_job(job_id: str, result: dict) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "completed"
            _jobs[job_id]["result"] = result


def fail_job(job_id: str, error: str) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = error


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)


# ---------------------------------------------------------------------------
# OpenHands availability
# ---------------------------------------------------------------------------

_OPENHANDS_AVAILABLE = None


def _check_openhands() -> bool:
    global _OPENHANDS_AVAILABLE
    if _OPENHANDS_AVAILABLE is None:
        try:
            from openhands.sdk import LLM, Agent, Conversation, Tool
            from openhands.tools import register_default_tools
            register_default_tools()
            _OPENHANDS_AVAILABLE = True
        except ImportError:
            _OPENHANDS_AVAILABLE = False
    return _OPENHANDS_AVAILABLE


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, headers: dict | None = None) -> dict | str:
    req = urllib.request.Request(url, headers=headers or {})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
        body = resp.read().decode()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def _http_post(url: str, data: dict, headers: dict | None = None) -> dict:
    payload = json.dumps(data).encode()
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=payload, headers=hdrs, method="POST")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, context=ctx, timeout=300) as resp:
        return json.loads(resp.read().decode())


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

def _github_headers() -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    h = {"Accept": "application/vnd.github+json", "User-Agent": "code-review-agent"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def parse_repo(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http") and "/" in url and "." not in url.split("/")[0]:
        return url
    parsed = urlparse(url if url.startswith("http") else f"https://{url}")
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    return f"{parts[0]}/{parts[1]}"


def repo_dir_name(slug: str) -> str:
    return slug.replace("/", "-")


def list_prs(repo_url: str) -> list[dict]:
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


def get_pr_diff(repo_url: str, pr_number: int) -> str:
    slug = parse_repo(repo_url)
    url = f"https://api.github.com/repos/{slug}/pulls/{pr_number}"
    headers = {**_github_headers(), "Accept": "application/vnd.github.diff"}
    return _http_get(url, headers)


# ---------------------------------------------------------------------------
# Persistent repo management
# ---------------------------------------------------------------------------

def ensure_repo_cloned(repo_url: str) -> Path:
    slug = parse_repo(repo_url)
    repo_path = REPOS_DIR / repo_dir_name(slug)
    if (repo_path / ".git").exists():
        subprocess.run(["git", "fetch", "--all"], cwd=repo_path,
                       capture_output=True, timeout=60)
    else:
        repo_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", f"https://github.com/{slug}.git", str(repo_path)],
            capture_output=True, timeout=120,
        )
    return repo_path


def get_local_diff(repo_path: Path, base: str, pr_number: int) -> str:
    subprocess.run(
        ["git", "fetch", "origin", f"pull/{pr_number}/head:pr-{pr_number}"],
        cwd=repo_path, capture_output=True, timeout=60,
    )
    stat = subprocess.run(
        ["git", "diff", f"origin/{base}...pr-{pr_number}", "--stat"],
        cwd=repo_path, capture_output=True, text=True, timeout=30,
    ).stdout
    diff = subprocess.run(
        ["git", "diff", f"origin/{base}...pr-{pr_number}"],
        cwd=repo_path, capture_output=True, text=True, timeout=30,
    ).stdout
    return f"Changed files:\n{stat}\n\nDiff:\n{diff}"


# ---------------------------------------------------------------------------
# State: memory + reviews
# ---------------------------------------------------------------------------

def load_memory() -> str:
    if MEMORY_FILE.exists():
        return MEMORY_FILE.read_text().strip()
    return ""


def append_memory(new_learnings: str) -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MEMORY_FILE, "a") as f:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        f.write(f"\n---\n**[{ts}]**\n{new_learnings}\n")


def save_review(repo_url: str, pr_number: int, pr: dict, review_text: str, model: str) -> None:
    slug = parse_repo(repo_url)
    review_dir = REVIEWS_DIR / repo_dir_name(slug)
    review_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    (review_dir / f"pr-{pr_number}.md").write_text(
        f"# Review: PR #{pr_number} — {pr['title']}\n"
        f"**Author:** @{pr['author']}  \n"
        f"**Reviewed:** {ts}  \n"
        f"**Model:** {model}  \n\n"
        f"{review_text}\n"
    )
    (review_dir / f"pr-{pr_number}.json").write_text(json.dumps({
        "pr_number": pr_number, "title": pr["title"], "author": pr["author"],
        "model": model, "reviewed_at": ts, "review_length": len(review_text),
    }, indent=2))


def load_past_reviews(repo_url: str, limit: int = 3) -> str:
    slug = parse_repo(repo_url)
    review_dir = REVIEWS_DIR / repo_dir_name(slug)
    if not review_dir.exists():
        return ""
    json_files = sorted(review_dir.glob("pr-*.json"), reverse=True)[:limit]
    if not json_files:
        return ""
    summaries = []
    for jf in json_files:
        try:
            meta = json.loads(jf.read_text())
            summaries.append(
                f"- PR #{meta['pr_number']}: {meta['title']} "
                f"(reviewed {meta['reviewed_at']}, {meta['review_length']} chars)"
            )
        except (json.JSONDecodeError, KeyError):
            continue
    return "**Past reviews for this repo:**\n" + "\n".join(summaries)


def get_review_history(repo_url: str) -> list[dict]:
    slug = parse_repo(repo_url)
    review_dir = REVIEWS_DIR / repo_dir_name(slug)
    if not review_dir.exists():
        return []
    reviews = []
    for jf in sorted(review_dir.glob("pr-*.json"), reverse=True):
        try:
            reviews.append(json.loads(jf.read_text()))
        except (json.JSONDecodeError, KeyError):
            continue
    return reviews


# ---------------------------------------------------------------------------
# LLM API (direct — used by simple mode)
# ---------------------------------------------------------------------------

MODEL_CONFIGS = {
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        "key_env": "GEMINI_API_KEY",
    },
    "groq": {"url": "https://api.groq.com/openai/v1/chat/completions", "key_env": "GROQ_API_KEY"},
    "cerebras": {"url": "https://api.cerebras.ai/v1/chat/completions", "key_env": "CEREBRAS_API_KEY"},
    "openrouter": {"url": "https://openrouter.ai/api/v1/chat/completions", "key_env": "OPENROUTER_API_KEY"},
    "deepseek": {"url": "https://api.deepseek.com/chat/completions", "key_env": "DEEPSEEK_API_KEY"},
    "mistral": {"url": "https://api.mistral.ai/v1/chat/completions", "key_env": "MISTRAL_API_KEY"},
    "glm": {"url": "https://api.z.ai/api/coding/paas/v4/chat/completions", "key_env": "GLM_API_KEY"},
}


def call_llm(model: str, system: str, user: str) -> str:
    import time
    provider = model.split("/")[0]

    t_start = time.time()
    if provider == "gemini":
        cfg = MODEL_CONFIGS["gemini"]
        key = os.environ.get(cfg["key_env"], "")
        model_name = model.replace("gemini/", "")
        url = cfg["url"].format(model=model_name, key=key)
        data = {
            "contents": [{"parts": [{"text": f"{system}\n\n{user}"}]}],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8192},
        }
        resp = _http_post(url, data)
        text = resp["candidates"][0]["content"]["parts"][0]["text"]
    else:
        cfg = MODEL_CONFIGS.get(provider, MODEL_CONFIGS["openrouter"])
        key = os.environ.get(cfg["key_env"], "")
        url = cfg["url"]
        api_model = model.replace("openrouter/", "") if provider == "openrouter" else model.replace(f"{provider}/", "")
        data = {
            "model": api_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "max_tokens": 8192,
        }
        headers = {"Authorization": f"Bearer {key}"}
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/code-review-agent"
        resp = _http_post(url, data, headers)
        text = resp["choices"][0]["message"]["content"]

    return text


# ---------------------------------------------------------------------------
# Review prompts
# ---------------------------------------------------------------------------

REVIEW_SYSTEM_PROMPT = """\
You are an expert code reviewer with a persistent memory.
You build up knowledge over time across reviews.

Focus on:
1. Bugs & correctness — logic errors, off-by-one, null/undefined
2. Security — injection, auth, secrets, OWASP top-10
3. Performance — unnecessary allocations, N+1 queries
4. Readability — naming, complexity
5. Best practices — error handling, testing gaps

For each issue: file, line range, severity (critical/warning/suggestion), explanation, fix.
If the PR looks good, say so — don't invent problems.
End with a summary and assessment (approve / request-changes / comment).

IMPORTANT: At the very end, add a section called '## Learnings'
with new patterns or conventions you noticed about this codebase."""


def build_simple_prompt(pr: dict, diff: str, repo_url: str) -> tuple[str, str]:
    memory = load_memory()
    past_reviews = load_past_reviews(repo_url)
    system_parts = [REVIEW_SYSTEM_PROMPT]
    if memory:
        system_parts.extend(["", "--- YOUR ACCUMULATED MEMORY ---", memory, "--- END MEMORY ---"])
    if past_reviews:
        system_parts.extend(["", past_reviews])
    system = "\n".join(system_parts)
    user = (
        f"## PR #{pr['number']}: {pr['title']}\n"
        f"**Author:** @{pr['author']}\n"
        f"**Branch:** {pr['branch']} -> {pr['base']}\n\n"
        f"### Description\n{pr['body']}\n\n"
        f"### Diff\n```diff\n{diff}\n```"
    )
    return system, user


def build_agent_task(slug: str, pr: dict, repo_url: str) -> str:
    memory = load_memory()
    past_reviews = load_past_reviews(repo_url)
    repo_path = REPOS_DIR / repo_dir_name(slug)

    parts = [
        f"Review pull request #{pr['number']} in https://github.com/{slug}.",
        f"",
        f"PR Title: {pr['title']}",
        f"Author: @{pr['author']}",
        f"Branch: {pr['branch']} -> {pr['base']}",
        f"",
        f"Description: {pr['body']}",
        f"",
        f"Steps:",
        f"1. cd {repo_path}",
        f"2. git fetch origin pull/{pr['number']}/head:pr-{pr['number']}",
        f"3. git diff origin/{pr['base']}...pr-{pr['number']} --stat",
        f"4. Read changed files to understand context",
        f"5. Look at surrounding code affected by changes",
        f"6. Write a thorough code review",
        f"",
        REVIEW_SYSTEM_PROMPT,
    ]
    if memory:
        parts.extend(["", "--- YOUR ACCUMULATED MEMORY ---", memory, "--- END MEMORY ---"])
    if past_reviews:
        parts.extend(["", past_reviews])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# OpenHands agent review
# ---------------------------------------------------------------------------

def _create_openhands_llm(model: str):
    from openhands.sdk import LLM
    from pydantic import SecretStr

    provider = model.split("/")[0]
    model_name = model.replace(f"{provider}/", "")

    if provider == "glm":
        return LLM(
            model=f"openai/{model_name}",
            base_url="https://api.z.ai/api/coding/paas/v4",
            api_key=SecretStr(os.environ.get("GLM_API_KEY", "")),
            temperature=0.0,
        )
    elif provider == "gemini":
        return LLM(model=model, api_key=SecretStr(os.environ.get("GEMINI_API_KEY", "")), temperature=0.0)
    else:
        cfg = MODEL_CONFIGS.get(provider, {})
        key = os.environ.get(cfg.get("key_env", "LLM_API_KEY"), "")
        return LLM(model=model, api_key=SecretStr(key), temperature=0.0)


def _extract_agent_response(conversation) -> str:
    try:
        return conversation.ask_agent(
            "Output your complete code review in markdown. Include all issues, fixes, summary, assessment, and ## Learnings."
        )
    except Exception:
        pass
    try:
        state = getattr(conversation, '_state', None)
        if state and hasattr(state, 'agent_state'):
            if isinstance(state.agent_state, dict):
                msg = state.agent_state.get("finish_message", "")
                if msg and len(msg) > 50:
                    return msg
    except Exception:
        pass
    return "Agent completed but could not extract review text."


def do_review_agent(repo_url: str, pr_number: int, model: str) -> dict:
    import time
    import logging
    for name in ["openhands", "litellm", "httpx", "httpcore"]:
        logging.getLogger(name).setLevel(logging.WARNING)

    from openhands.sdk import Agent, Conversation, Tool

    slug = parse_repo(repo_url)
    prs = list_prs(repo_url)
    pr = next((p for p in prs if p["number"] == pr_number), None)
    if pr is None:
        return {"error": f"PR #{pr_number} not found"}

    try:
        ensure_repo_cloned(repo_url)
    except Exception:
        pass

    llm = _create_openhands_llm(model)
    agent = Agent(
        llm=llm,
        tools=[Tool(name="terminal"), Tool(name="file_editor")],
        condenser=None,
    )

    workspace_dir = REPOS_DIR / repo_dir_name(slug)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    task = build_agent_task(slug, pr, repo_url)
    t_start = time.time()

    conversation = Conversation(
        agent=agent,
        workspace=str(workspace_dir),
        max_iteration_per_run=10,
        visualizer=None,
        delete_on_close=False,  # preserve persistent repos
    )

    review_text = ""
    try:
        conversation.send_message(task)
        conversation.run()

        # Try to get the finish message directly (no extra LLM call)
        try:
            state = getattr(conversation, '_state', None)
            if state and hasattr(state, 'agent_state'):
                if isinstance(state.agent_state, dict):
                    review_text = state.agent_state.get("finish_message", "")
        except Exception:
            pass

        # NO ask_agent fallback — it triggers another full loop
    except Exception as e:
        review_text = f"Agent error: {e}"
    finally:
        try:
            conversation.close()
        except Exception:
            pass
        import gc
        gc.collect()

    t_elapsed = time.time() - t_start

    # If agent produced nothing, fall back to simple mode
    if not review_text or len(review_text) < 50:
        try:
            # Try local diff first (repo already cloned by agent), then API
            try:
                repo_path = REPOS_DIR / repo_dir_name(slug)
                diff = get_local_diff(repo_path, pr["base"], pr_number)
            except Exception:
                diff = get_pr_diff(repo_url, pr_number)
            system, user = build_simple_prompt(pr, diff, repo_url)
            review_text = call_llm(model, system, user)
            review_text = f"[simple-fallback]\n\n{review_text}"
        except Exception as e:
            review_text = f"Agent produced no review. Fallback also failed: {e}"

    save_review(repo_url, pr_number, pr, review_text, model)
    if "## Learnings" in review_text:
        learnings = review_text.split("## Learnings", 1)[1].strip()
        if learnings:
            append_memory(f"[{slug} PR #{pr_number}] {learnings}")

    return {
        "pr": pr, "review_text": review_text, "posted": False,
        "mode": "openhands-agent", "repo": slug,
        "timing": {"total_seconds": round(t_elapsed, 2), "model": model},
    }


# ---------------------------------------------------------------------------
# Simple review
# ---------------------------------------------------------------------------

def do_review_simple(repo_url: str, pr_number: int, model: str) -> dict:
    import time
    slug = parse_repo(repo_url)
    prs = list_prs(repo_url)
    pr = next((p for p in prs if p["number"] == pr_number), None)
    if pr is None:
        return {"error": f"PR #{pr_number} not found"}

    try:
        repo_path = ensure_repo_cloned(repo_url)
        diff = get_local_diff(repo_path, pr["base"], pr_number)
    except Exception:
        diff = get_pr_diff(repo_url, pr_number)

    system, user = build_simple_prompt(pr, diff, repo_url)
    t0 = time.time()
    review_text = call_llm(model, system, user)
    llm_time = time.time() - t0

    save_review(repo_url, pr_number, pr, review_text, model)
    if "## Learnings" in review_text:
        learnings = review_text.split("## Learnings", 1)[1].strip()
        if learnings:
            append_memory(f"[{slug} PR #{pr_number}] {learnings}")

    return {
        "pr": pr, "review_text": review_text, "posted": False,
        "mode": "simple", "repo": slug,
        "timing": {"llm_call_seconds": round(llm_time, 2), "model": model},
    }


# ---------------------------------------------------------------------------
# Actions (run in background thread for long-running tasks)
# ---------------------------------------------------------------------------

def do_review(repo_url: str, pr_number: int, model: str, mode: str = "agent") -> dict:
    if mode == "agent" and _check_openhands():
        try:
            return do_review_agent(repo_url, pr_number, model)
        except Exception:
            pass
    return do_review_simple(repo_url, pr_number, model)


def _run_review_job(job_id: str, repo_url: str, pr_number: int, model: str, mode: str) -> None:
    """Background thread for reviews."""
    try:
        result = do_review(repo_url, pr_number, model, mode)
        if "error" in result:
            fail_job(job_id, result["error"])
        else:
            complete_job(job_id, result)
    except Exception as e:
        fail_job(job_id, str(e))


# ---------------------------------------------------------------------------
# HTTP Server — stays responsive while reviews run in background
# ---------------------------------------------------------------------------

class AgentHandler(BaseHTTPRequestHandler):
    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json({
                "status": "ok",
                "openhands": _check_openhands(),
                "active_jobs": sum(1 for j in _jobs.values() if j["status"] == "running"),
            })
        elif self.path == "/memory":
            self._send_json({"content": load_memory()})
        elif self.path.startswith("/reviews/"):
            dir_name = self.path.split("/reviews/", 1)[1].strip("/")
            slug = dir_name.replace("-", "/", 1)
            self._send_json({"reviews": get_review_history(f"https://github.com/{slug}")})
        elif self.path.startswith("/jobs/"):
            job_id = self.path.split("/jobs/", 1)[1].strip("/")
            job = get_job(job_id)
            if job:
                self._send_json(job)
            else:
                self._send_json({"error": "Job not found"}, 404)
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        action = body.get("action", "")
        repo_url = body.get("repo_url", "")

        if not action:
            self._send_json({"error": "Missing 'action' field"}, 400)
            return

        try:
            if action == "scan":
                # Scan is fast — return directly
                self._send_json({"prs": list_prs(repo_url)})

            elif action == "review":
                pr_number = body.get("pr_number")
                model = body.get("model", "glm/GLM-4.7")
                mode = body.get("mode", "agent")
                if not pr_number:
                    self._send_json({"error": "Missing 'pr_number'"}, 400)
                    return

                # Start review in background, return job ID immediately
                job_id = create_job(f"review-{pr_number}")
                threading.Thread(
                    target=_run_review_job,
                    args=(job_id, repo_url, int(pr_number), model, mode),
                    daemon=True,
                ).start()
                self._send_json({"job_id": job_id, "status": "running"})

            elif action == "history":
                self._send_json({"reviews": get_review_history(repo_url)})

            else:
                self._send_json({"error": f"Unknown action: {action}"}, 400)

        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def log_message(self, format, *args):
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    if not MEMORY_FILE.exists():
        MEMORY_FILE.touch()

    oh = "available" if _check_openhands() else "not installed (simple mode)"
    print(f"Code Review Agent on port {PORT} | OpenHands: {oh}", flush=True)

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadingHTTPServer(("0.0.0.0", PORT), AgentHandler)
    server.serve_forever()
