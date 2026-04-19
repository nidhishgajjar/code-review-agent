#!/usr/bin/env python3
"""
Self-poller. Runs inside the Orb sandbox via /agent/.orb/cron.json.

Reads agent.py's per-CID ASSIGNMENTS, asks GitHub for recently-updated open
PRs in this agent's repo set, and POSTs synthetic pull_request webhooks to
http://127.0.0.1:8080/webhook (same path GitHub uses — exercises the full
HMAC + dedup pipeline).
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent import _my_cid8, oss_repos, own_repos  # noqa: E402

GH_TOKEN = os.environ["GITHUB_TOKEN"]
SECRET = os.environ["GITHUB_WEBHOOK_SECRET"].encode()
WINDOW_MIN = int(os.environ.get("POLL_WINDOW_MIN", "90"))
MAX_PER_CYCLE = int(os.environ.get("POLL_MAX_PER_CYCLE", "10"))
TARGET = os.environ.get("POLL_TARGET", "http://127.0.0.1:8080/webhook")
HEALTH = os.environ.get("POLL_HEALTH", "http://127.0.0.1:8080/health")


def wait_for_agent(timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def gh_get(path: str):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cra-self-poller",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def post_webhook(payload: dict):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    repo_slug = payload["repository"]["full_name"].replace("/", "_")
    delivery = f"selfpoll-{int(time.time())}-{repo_slug}-{payload['pull_request']['number']}"
    req = urllib.request.Request(
        TARGET,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": delivery,
            "User-Agent": "cra-self-poller",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]
    except urllib.error.URLError as e:
        return -1, f"connection error: {e}"


def main() -> int:
    cid = _my_cid8()
    repos = sorted(set(own_repos()) | set(oss_repos()))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MIN)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(
        f"[{datetime.now(timezone.utc).isoformat()}] cid={cid} repos={len(repos)} "
        f"window_min={WINDOW_MIN} cap={MAX_PER_CYCLE} cutoff={cutoff_iso}"
    )

    if not wait_for_agent():
        print("agent /health never responded — abort")
        return 1

    posted = 0
    for repo in repos:
        if posted >= MAX_PER_CYCLE:
            print(f"  hit cap {MAX_PER_CYCLE}, stopping")
            break
        try:
            prs = gh_get(
                f"/repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page=10"
            )
        except Exception as e:
            print(f"  {repo}: list failed: {e}")
            continue
        for pr in prs:
            if posted >= MAX_PER_CYCLE:
                break
            updated = pr.get("updated_at", "")
            if updated < cutoff_iso:
                break
            if pr.get("draft"):
                continue
            payload = {
                "action": "opened",
                "pull_request": pr,
                "repository": {"full_name": repo},
                "_synthetic": True,
            }
            num = pr["number"]
            title = (pr.get("title") or "")[:60]
            print(f"  {repo}#{num} updated={updated} {title!r}")
            status, body = post_webhook(payload)
            print(f"    -> {status} {body}")
            posted += 1

    print(f"=== posted {posted} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
