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


MANIFEST_NAMES = (
    "pyproject.toml", "setup.py", "setup.cfg", "package.json",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
    "Gemfile", "composer.json", "mix.exs", "Makefile",
)
README_NAMES = ("README.md", "README.rst", "README.txt", "README")


def build_project_brief(local_repo: pathlib.Path) -> str:
    parts = []
    for name in README_NAMES:
        p = local_repo / name
        if p.is_file():
            parts.append(f"--- {name} (first 3000 chars) ---\n{p.read_text(errors='replace')[:3000]}")
            break
    for name in MANIFEST_NAMES:
        p = local_repo / name
        if p.is_file():
            parts.append(f"--- {name} (first 1500 chars) ---\n{p.read_text(errors='replace')[:1500]}")
    tree = []
    try:
        for item in sorted(local_repo.iterdir()):
            if item.name.startswith(".") or item.name in {"node_modules", "__pycache__", "dist", "build", ".venv"}:
                continue
            tree.append(f"{'dir' if item.is_dir() else 'file':4}  {item.name}")
    except Exception:
        pass
    if tree:
        parts.append("--- top-level layout ---\n" + "\n".join(tree[:40]))
    return "\n\n".join(parts)[:14_000]


IMPORT_RE_PY = re.compile(r"^\s*(?:from\s+([.\w]+)\s+import|import\s+([.\w, ]+))", re.M)
IMPORT_RE_JS = re.compile(r"""(?:from|require\()\s*['\"]([^'\"]+)['\"]""", re.M)
IMPORT_RE_GO = re.compile(r"""^\s*(?:import\s+)?['\"]([\w./-]+)['\"]""", re.M)


def extract_imports(text: str, path: str) -> list[str]:
    out = []
    if path.endswith(".py"):
        for m in IMPORT_RE_PY.finditer(text):
            if m.group(1):
                out.append(m.group(1))
            elif m.group(2):
                for part in m.group(2).split(","):
                    out.append(part.strip().split(" as ")[0])
    elif path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs")):
        out = [m.group(1) for m in IMPORT_RE_JS.finditer(text)]
    elif path.endswith(".go"):
        out = [m.group(1) for m in IMPORT_RE_GO.finditer(text)]
    return [i for i in out if i and not i.startswith(("@", "http"))]


def resolve_import(local_repo: pathlib.Path, module: str, origin: pathlib.Path) -> pathlib.Path | None:
    if module.startswith("."):
        rel = origin.parent
        for _ in range(len(module) - len(module.lstrip("."))):
            rel = rel.parent
        module = module.lstrip(".")
    parts = module.replace("/", ".").split(".")
    bases = [local_repo, *[local_repo / "src" / p for p in ("", *parts[:-1])]]
    suffixes = [".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"]
    for base in bases:
        for s in suffixes:
            c = base.joinpath(*parts).with_suffix(s)
            if c.is_file():
                return c
        for s in suffixes:
            c = base.joinpath(*parts, "index").with_suffix(s)
            if c.is_file():
                return c
        c = base.joinpath(*parts, "__init__.py")
        if c.is_file():
            return c
    return None


def read_context(local_repo: pathlib.Path, files: list[str], per_file_limit: int = MAX_FILE_CHARS) -> str:
    out = []
    budget = 30_000
    seen: set[str] = set()

    for f in files[:15]:
        p = local_repo / f
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        snippet = text[:per_file_limit]
        block = f"\n--- {f} (changed file, first {len(snippet)} chars) ---\n{snippet}\n"
        if len(block) > budget:
            break
        out.append(block)
        budget -= len(block)
        seen.add(str(p.resolve()))

    for f in files[:10]:
        if budget < 2000:
            break
        p = local_repo / f
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except Exception:
            continue
        for imp in extract_imports(text, f)[:8]:
            if budget < 2000:
                break
            target = resolve_import(local_repo, imp, p)
            if target is None or str(target.resolve()) in seen:
                continue
            try:
                itext = target.read_text(errors="replace")
            except Exception:
                continue
            rel = target.relative_to(local_repo)
            snippet = itext[:3500]
            block = f"\n--- {rel} (imported by {f}, first {len(snippet)} chars) ---\n{snippet}\n"
            if len(block) > budget:
                continue
            out.append(block)
            budget -= len(block)
            seen.add(str(target.resolve()))
    return "".join(out)


REVIEW_PROMPT = """You are a senior code reviewer. Your job is to give the maintainer a review that shows you actually understood the codebase — not a surface skim of the diff.

Repository: {repo}
PR #{number}: {title}
Author: {author}

--- PROJECT BRIEF (README, manifest, layout) ---
{brief}

--- CODE CONTEXT (changed files + their imports) ---
{context}

--- DIFF ---
{diff}

Before you write anything, silently work through:
  1. What is this project? Who uses it? What are its conventions and idioms (from the README, manifest, file layout)?
  2. What do the changed files do today, and how do other files depend on them?
  3. What is this PR actually trying to change? What is the author's intent?
  4. Where could it break something — correctness, concurrency, security, API contract, performance, or a convention the codebase follows?

Only after that, produce the review. Use these sections:

## Summary
What this PR does and why, in the context of the project.

## Architecture
How it fits the codebase. Reference specific modules/files you saw in the context. Flag structural concerns if any.

## Issues
Bullet list. Each: **[severity]** file:line — concrete problem — concrete fix. Severity is critical, warning, or suggestion. If none, say so — do not invent issues.

## Cross-file impact
Things in other files (that you saw in the imports or layout) that this change affects or could break. If nothing, say so.

## Assessment
approve / request-changes / comment. One sentence why.

Be specific with file paths and line numbers. Be terse — every sentence should earn its place.
"""


def call_llm(prompt: str) -> str:
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    r = requests.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/AWLSEN/code-review-agent",
            "X-Title": "code-review-agent",
        },
        json=body,
        timeout=180,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"LLM {r.status_code}: {r.text[:400]}")
    data = r.json()
    return data["choices"][0]["message"]["content"]


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

    files = changed_files(diff)
    brief = build_project_brief(local_repo)
    context = read_context(local_repo, files)
    log(f"{repo}#{number} context: brief={len(brief)}c context={len(context)}c diff={len(diff)}c files={len(files)}")

    prompt = REVIEW_PROMPT.format(
        repo=repo, number=number, title=pr["title"],
        author=pr["user"]["login"], brief=brief, diff=diff, context=context,
    )
    review = call_llm(prompt)
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
