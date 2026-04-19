#!/usr/bin/env python3
"""
Self-poller. Runs inside the Orb sandbox via /agent/.orb/cron.json.

Cron wakes the sandbox but NOT the entry agent process — so this script
imports agent.py directly and calls review_worker() in-process. The Flask
server stays checkpointed; we share state.json + cloned repos on disk with it.

When a real GitHub webhook later wakes the Flask agent, it sees the same
state.json and won't re-review what we already handled here.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent import _my_cid8, oss_repos, own_repos, review_worker  # noqa: E402

GH_TOKEN = os.environ["GITHUB_TOKEN"]
WINDOW_MIN = int(os.environ.get("POLL_WINDOW_MIN", "90"))
MAX_PER_CYCLE = int(os.environ.get("POLL_MAX_PER_CYCLE", "10"))


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


def main() -> int:
    cid = _my_cid8()
    repos = sorted(set(own_repos()) | set(oss_repos()))
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MIN)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(
        f"[{datetime.now(timezone.utc).isoformat()}] cid={cid} repos={len(repos)} "
        f"window_min={WINDOW_MIN} cap={MAX_PER_CYCLE} cutoff={cutoff_iso}",
        flush=True,
    )

    reviewed = 0
    for repo in repos:
        if reviewed >= MAX_PER_CYCLE:
            print(f"  hit cap {MAX_PER_CYCLE}, stopping", flush=True)
            break
        try:
            prs = gh_get(
                f"/repos/{repo}/pulls?state=open&sort=updated&direction=desc&per_page=10"
            )
        except Exception as e:
            print(f"  {repo}: list failed: {e}", flush=True)
            continue
        for pr in prs:
            if reviewed >= MAX_PER_CYCLE:
                break
            updated = pr.get("updated_at", "")
            if updated < cutoff_iso:
                break
            if pr.get("draft"):
                continue
            num = pr["number"]
            title = (pr.get("title") or "")[:60]
            print(f"  {repo}#{num} updated={updated} {title!r}", flush=True)
            t0 = time.time()
            review_worker(repo, pr)  # handles dedup + state internally
            print(f"    -> done in {time.time()-t0:.1f}s", flush=True)
            reviewed += 1

    print(f"=== reviewed/checked {reviewed} ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
