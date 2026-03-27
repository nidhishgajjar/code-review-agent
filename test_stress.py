"""Stress test: N users, different repos, parallel. Reuses computers, never deletes."""

import json, time, httpx, random, threading, sys, os
sys.path.insert(0, ".")
from code_review_agent.orb_client import OrbClient
from dotenv import load_dotenv
load_dotenv()

orb = OrbClient()

N = int(sys.argv[1]) if len(sys.argv) > 1 else 20
REVIEWS_PER_USER = 2
MODEL = "glm/GLM-4.7"
STAGGER_SECONDS = 5  # gap between user starts

REPOS = [
    "https://github.com/pallets/flask",
    "https://github.com/fastapi/fastapi",
    "https://github.com/psf/requests",
    "https://github.com/pallets/click",
    "https://github.com/encode/httpx",
    "https://github.com/pallets/jinja",
    "https://github.com/pallets/werkzeug",
    "https://github.com/tiangolo/sqlmodel",
    "https://github.com/pydantic/pydantic",
    "https://github.com/encode/starlette",
    "https://github.com/aio-libs/aiohttp",
    "https://github.com/django/django",
    "https://github.com/tornadoweb/tornado",
    "https://github.com/boto/boto3",
    "https://github.com/psycopg/psycopg",
    "https://github.com/sqlalchemy/sqlalchemy",
    "https://github.com/celery/celery",
    "https://github.com/redis/redis-py",
    "https://github.com/pytest-dev/pytest",
    "https://github.com/Textualize/rich",
]

# Generate users
USERS = []
for i in range(N):
    USERS.append({
        "id": f"user-{i:03d}",
        "repo": REPOS[i % len(REPOS)],
        "reviews": REVIEWS_PER_USER,
    })

results = {}
lock = threading.Lock()
stats = {"provisioned": 0, "reused": 0, "reviews_ok": 0, "reviews_fail": 0}


def run_user(user, start_delay):
    uid = user["id"]
    repo = user["repo"]
    num_reviews = user["reviews"]
    user_results = {"user": uid, "repo": repo, "reviews": [], "errors": []}

    time.sleep(start_delay)

    try:
        t0 = time.time()
        comp = orb.get_or_create_computer(uid)
        comp_id = comp["id"]
        agent_url = orb.get_agent_url(comp_id)
        provision_time = round(time.time() - t0, 1)
        user_results["provision_time"] = provision_time
        user_results["computer_id"] = comp_id

        with lock:
            if provision_time < 2:
                stats["reused"] += 1
            else:
                stats["provisioned"] += 1
            print(f"  [{uid}] Ready ({provision_time}s) {agent_url}")

        # Wait for health
        for _ in range(5):
            try:
                r = httpx.get(f"{agent_url}/health", timeout=10)
                if r.status_code == 200:
                    break
            except:
                time.sleep(2)

        # Scan
        scan = orb.send_task(comp_id, {"action": "scan", "repo_url": repo}, retries=2)
        prs = scan.get("prs", [])
        user_results["total_prs"] = len(prs)

        if not prs:
            user_results["errors"].append("No PRs")
            with lock:
                results[uid] = user_results
            return

        review_prs = random.sample(prs, min(num_reviews, len(prs)))

        for i, pr in enumerate(review_prs):
            if i > 0:
                time.sleep(5)

            t0 = time.time()
            result = orb.send_task(comp_id, {
                "action": "review",
                "repo_url": repo,
                "pr_number": pr["number"],
                "model": MODEL,
            }, retries=2)
            elapsed = time.time() - t0

            if "error" in result:
                user_results["errors"].append(f"PR #{pr['number']}: {result['error'][:60]}")
                with lock:
                    stats["reviews_fail"] += 1
                    print(f"  [{uid}] PR #{pr['number']} FAIL ({elapsed:.0f}s)")
            else:
                timing = result.get("timing", {})
                rv = {"pr": pr["number"], "total_s": round(elapsed, 1),
                      "llm_s": timing.get("llm_call_seconds", "?"),
                      "chars": len(result.get("review_text", ""))}
                user_results["reviews"].append(rv)
                with lock:
                    stats["reviews_ok"] += 1
                    print(f"  [{uid}] PR #{pr['number']} OK ({elapsed:.0f}s, {rv['chars']}ch)")

        mem = orb.get_memory(comp_id)
        user_results["memory_chars"] = len(mem)

    except Exception as e:
        user_results["errors"].append(str(e)[:100])

    with lock:
        results[uid] = user_results


# Check existing
data = orb.list_computers()
existing = len(data.get("computers", []))
print(f"{'=' * 60}")
print(f"  STRESS TEST: {N} users | {REVIEWS_PER_USER} reviews each | {MODEL}")
print(f"  Existing computers: {existing}")
print(f"  Stagger: {STAGGER_SECONDS}s between starts")
print(f"{'=' * 60}\n")

t_start = time.time()
threads = []
for i, user in enumerate(USERS):
    delay = i * STAGGER_SECONDS
    t = threading.Thread(target=run_user, args=(user, delay))
    threads.append(t)
    t.start()

for t in threads:
    t.join(timeout=3600)

total_time = time.time() - t_start

# Summary
print(f"\n{'=' * 60}")
print(f"  RESULTS: {N} users")
print(f"{'=' * 60}")
print(f"  Total time: {total_time:.0f}s")
print(f"  Computers: {stats['provisioned']} new + {stats['reused']} reused")
print(f"  Reviews: {stats['reviews_ok']} OK | {stats['reviews_fail']} failed")
print(f"")

# Per-user summary
for uid in sorted(results.keys()):
    r = results[uid]
    repo_name = r.get('repo', '?').split('/')[-1]
    n_ok = len(r.get('reviews', []))
    n_err = len(r.get('errors', []))
    mem = r.get('memory_chars', 0)
    prov = r.get('provision_time', '?')
    status = "OK" if n_err == 0 else f"ERR({n_err})"
    print(f"  {uid}: {repo_name:<15} {status:<8} reviews={n_ok} mem={mem}ch prov={prov}s")

# Final computer count
data = orb.list_computers()
print(f"\n  ORB computers: {data.get('total', 0)}")
print(f"{'=' * 60}")
