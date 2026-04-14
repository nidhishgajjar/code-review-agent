#!/usr/bin/env python3
"""Deploy code review agents to Orb Cloud.

Creates one computer per repo, uploads config, builds, and deploys.

Usage:
    python3 scripts/deploy.py                # deploy all 10 repos
    python3 scripts/deploy.py facebook/react # deploy one repo
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ORB_API = os.environ["ORB_API_URL"]
ORB_KEY = os.environ["ORB_API_KEY"]
GLM_KEY = os.environ["GLM_API_KEY"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

# 10 popular repos with active PRs
DEFAULT_REPOS = [
    "NousResearch/hermes-agent",
    "All-Hands-AI/OpenHands",
    "langchain-ai/langchain",
    "vercel/next.js",
    "facebook/react",
    "nodejs/node",
    "fastapi/fastapi",
    "anthropics/anthropic-cookbook",
    "huggingface/transformers",
    "microsoft/autogen",
]

ORB_TOML = Path(__file__).parent.parent / "orb.toml"


def api(method, path, data=None):
    url = f"{ORB_API}{path}"
    headers = {
        "Authorization": f"Bearer {ORB_KEY}",
        "Content-Type": "application/json",
    }
    body = json.dumps(data if data is not None else {}).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.readable() else ""
        print(f"  API error {e.code}: {error_body[:200]}")
        raise


def api_toml(path, toml_bytes):
    url = f"{ORB_API}{path}"
    req = urllib.request.Request(
        url, data=toml_bytes,
        headers={
            "Authorization": f"Bearer {ORB_KEY}",
            "Content-Type": "application/toml",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def deploy_repo(repo: str):
    slug = repo.replace("/", "-")
    print(f"\n{'='*60}")
    print(f"Deploying: {repo}")
    print(f"{'='*60}")

    # 1. Create computer
    print("  Creating computer...")
    computer = api("POST", "/computers", {"name": f"reviewer-{slug}"})
    cid = computer["id"]
    print(f"  Computer: {cid[:8]}")

    # 2. Upload config with repo-specific env var
    print("  Uploading config...")
    toml_content = ORB_TOML.read_text()
    # Inject the repo as a build-time env override isn't possible,
    # so we modify the TOML to set GITHUB_REPO directly
    toml_content = toml_content.replace(
        'GITHUB_REPO      = "${GITHUB_REPO}"',
        f'GITHUB_REPO      = "{repo}"',
    )
    api_toml(f"/computers/{cid}/config", toml_content.encode())

    # 3. Build
    print("  Building (installing OpenHands)...")
    build = api("POST", f"/computers/{cid}/build", {})
    if not build.get("success"):
        failed = [s for s in build.get("steps", []) if s.get("exit_code", 0) != 0]
        for s in failed:
            print(f"  FAILED: {s.get('step', '')[:80]}")
            print(f"  STDERR: {s.get('stderr', '')[:200]}")
        return None

    print(f"  Build OK ({len(build.get('steps', []))} steps)")

    # 4. Deploy agent
    print("  Deploying agent...")
    result = api("POST", f"/computers/{cid}/agents", {
        "task": "start",
        "org_secrets": {
            "GLM_API_KEY": GLM_KEY,
            "GITHUB_TOKEN": GITHUB_TOKEN,
            "GITHUB_REPO": repo,
        },
    })
    deployed = result.get("deployed", 0)
    print(f"  Deployed: {deployed} agent(s)")

    return {
        "repo": repo,
        "computer_id": cid,
        "short_id": cid[:8],
        "deployed": deployed,
    }


def main():
    repos = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_REPOS

    print(f"Deploying {len(repos)} code review agents to Orb Cloud")
    print(f"API: {ORB_API}")
    print(f"LLM: GLM 5.1 via api.z.ai")
    print()

    results = []
    for repo in repos:
        try:
            result = deploy_repo(repo)
            if result:
                results.append(result)
        except Exception as e:
            print(f"  FAILED: {e}")

    # Summary
    print(f"\n{'='*60}")
    print(f"DEPLOYMENT SUMMARY")
    print(f"{'='*60}")
    for r in results:
        print(f"  {r['short_id']} | {r['repo']} | agents={r['deployed']}")

    # Save manifest
    manifest_path = Path(__file__).parent.parent / "data" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(results, indent=2))
    print(f"\nManifest saved: {manifest_path}")


if __name__ == "__main__":
    main()
