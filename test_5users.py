"""5-user parallel test. Reuses existing computers, never deletes. Uses GLM-4.7."""

import json, time, httpx, random, threading, base64, os, sys
sys.path.insert(0, ".")
from code_review_agent.orb_client import OrbClient

from dotenv import load_dotenv
load_dotenv()

orb = OrbClient()

USERS = [
    {"id": "alice", "repo": "https://github.com/pallets/flask", "reviews": 2},
    {"id": "bob", "repo": "https://github.com/fastapi/fastapi", "reviews": 2},
    {"id": "carol", "repo": "https://github.com/psf/requests", "reviews": 3},
    {"id": "dave", "repo": "https://github.com/pallets/click", "reviews": 2},
    {"id": "eve", "repo": "https://github.com/encode/httpx", "reviews": 2},
]

MODEL = "glm/GLM-4.7"

results = {}
lock = threading.Lock()


def run_user(user, start_delay):
    uid = user["id"]
    repo = user["repo"]
    num_reviews = user["reviews"]
    user_results = {"user": uid, "repo": repo, "reviews": [], "errors": []}

    time.sleep(start_delay)
    print(f"  [{uid}] Starting (delayed {start_delay}s)")

    try:
        t0 = time.time()
        comp = orb.get_or_create_computer(uid)
        comp_id = comp["id"]
        agent_url = orb.get_agent_url(comp_id)
        user_results["provision_time"] = round(time.time() - t0, 1)
        user_results["computer_id"] = comp_id
        user_results["agent_url"] = agent_url
        print(f"  [{uid}] Computer: {agent_url} ({user_results['provision_time']}s)")

        for _ in range(5):
            try:
                r = httpx.get(f"{agent_url}/health", timeout=10)
                if r.status_code == 200:
                    break
            except:
                time.sleep(2)

        scan = orb.send_task(comp_id, {"action": "scan", "repo_url": repo}, retries=2)
        prs = scan.get("prs", [])
        user_results["total_prs"] = len(prs)
        print(f"  [{uid}] {len(prs)} PRs")

        if not prs:
            user_results["errors"].append("No PRs")
            with lock:
                results[uid] = user_results
            return

        review_prs = random.sample(prs, min(num_reviews, len(prs)))

        for i, pr in enumerate(review_prs):
            if i > 0:
                time.sleep(10)  # small gap between own reviews
            print(f"  [{uid}] PR #{pr['number']} (review {i+1}/{len(review_prs)})")

            t0 = time.time()
            result = orb.send_task(comp_id, {
                "action": "review",
                "repo_url": repo,
                "pr_number": pr["number"],
                "model": MODEL,
            }, retries=2)
            elapsed = time.time() - t0

            if "error" in result:
                user_results["errors"].append(f"PR #{pr['number']}: {result['error'][:80]}")
                print(f"  [{uid}] PR #{pr['number']} ERROR: {result['error'][:50]}")
            else:
                timing = result.get("timing", {})
                rv = {"pr": pr["number"], "title": pr["title"][:40],
                      "total_s": round(elapsed, 1), "llm_s": timing.get("llm_call_seconds", "?"),
                      "chars": len(result.get("review_text", ""))}
                user_results["reviews"].append(rv)
                print(f"  [{uid}] PR #{pr['number']} — {elapsed:.1f}s | {rv['chars']} chars")

        mem = orb.get_memory(comp_id)
        user_results["memory_chars"] = len(mem)

    except Exception as e:
        user_results["errors"].append(str(e)[:200])

    with lock:
        results[uid] = user_results


# Check existing computers
data = orb.list_computers()
existing = {c["name"].replace("review-", ""): c for c in data.get("computers", [])}
print(f"Existing computers: {len(existing)}")
for name, c in existing.items():
    print(f"  {name}: {c['id'][:8]}... ({c['status']})")
print(f"Model: {MODEL}")
print()

# Stagger starts — 15s apart (GLM has no tight rate limits)
print("Starting 5 users (staggered 15s apart)...\n")
threads = []
for i, user in enumerate(USERS):
    delay = i * 15
    t = threading.Thread(target=run_user, args=(user, delay))
    threads.append(t)
    t.start()

for t in threads:
    t.join(timeout=1800)

# Results
print(f"\n{'=' * 70}")
print(f"  5-USER TEST — {MODEL}")
print(f"{'=' * 70}")
for uid in ["alice", "bob", "carol", "dave", "eve"]:
    r = results.get(uid, {})
    print(f"\n  {uid.upper()} — {r.get('repo','?').split('/')[-1]}")
    print(f"  Computer: {r.get('computer_id','?')[:8]}... | Provision: {r.get('provision_time','?')}s | PRs: {r.get('total_prs','?')} | Memory: {r.get('memory_chars',0)} chars")
    for rv in r.get("reviews", []):
        print(f"    PR #{rv['pr']}: {rv['total_s']}s | LLM {rv['llm_s']}s | {rv['chars']} chars | {rv['title']}")
    for err in r.get("errors", []):
        print(f"    ERROR: {err}")

total_reviews = sum(len(r.get("reviews", [])) for r in results.values())
total_errors = sum(len(r.get("errors", [])) for r in results.values())
data = orb.list_computers()
print(f"\n{'=' * 70}")
print(f"  {total_reviews} reviews | {total_errors} errors | {data.get('total', 0)} computers")
for c in data.get("computers", []):
    print(f"    {c['id'][:8]}... {c['name']} ({c['status']})")
print(f"{'=' * 70}")
