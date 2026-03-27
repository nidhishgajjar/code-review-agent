"""Deploy the code review agent web app to ORB Cloud.

Usage:
    source .env && python3 deploy_orb.py
"""

import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx

ORB_BASE = "https://api.orbcloud.dev"
ORB_KEY = os.environ.get("ORB_API_KEY", "")
if not ORB_KEY:
    print("ERROR: Set ORB_API_KEY in .env or environment", file=sys.stderr)
    sys.exit(1)

PROJECT_ROOT = Path(__file__).resolve().parent

# Files to upload
FILES_TO_UPLOAD = [
    "code_review_agent/__init__.py",
    "code_review_agent/standalone.py",
    "code_review_agent/web_standalone.py",
    "code_review_agent/static/index.html",
]

http = httpx.Client(
    base_url=ORB_BASE,
    headers={"Authorization": f"Bearer {ORB_KEY}", "Content-Type": "application/json"},
    timeout=600.0,
)


def api_post(path, **kwargs):
    r = http.post(path, **kwargs)
    r.raise_for_status()
    return r.json()


def api_get(path):
    r = http.get(path)
    r.raise_for_status()
    return r.json()


def api_delete(path):
    r = http.delete(path)
    r.raise_for_status()
    return r.json()


def exec_cmd(comp_id, cmd):
    return api_post(f"/v1/computers/{comp_id}/exec", json={"command": cmd})


def upload_file(comp_id, remote_path, content):
    b64 = base64.b64encode(content.encode()).decode()
    exec_cmd(comp_id, f"mkdir -p $(dirname {remote_path}) && echo '{b64}' | base64 -d > {remote_path}")


def main():
    # Clean up old computers
    print("Cleaning up old computers...")
    data = api_get("/v1/computers")
    for c in data.get("computers", []):
        if "review" in c.get("name", ""):
            api_delete(f"/v1/computers/{c['id']}")
            print(f"  Deleted {c['id']} ({c['name']})")

    # Create computer
    print("\nCreating ORB computer...")
    comp = api_post("/v1/computers", json={
        "name": "code-review-webapp",
        "runtime_mb": 2048,
        "disk_mb": 4096,
    })
    comp_id = comp["id"]
    short_id = comp_id[:8]
    print(f"  ID: {comp_id}")
    print(f"  Short ID: {short_id}")

    # Install deps (fastapi + uvicorn are lightweight)
    print("\nInstalling dependencies...")
    r = exec_cmd(comp_id, "pip install --break-system-packages fastapi uvicorn python-dotenv 2>&1 | tail -3")
    print(f"  {r.get('stdout', '').strip()}")

    # Upload source files
    print("\nUploading source files...")
    for rel_path in FILES_TO_UPLOAD:
        full_path = PROJECT_ROOT / rel_path
        if not full_path.exists():
            print(f"  SKIP {rel_path} (not found)")
            continue
        content = full_path.read_text()
        remote = f"/agent/{rel_path}"
        upload_file(comp_id, remote, content)
        print(f"  {rel_path} -> {remote}")

    # Upload env keys
    print("\nUploading API keys...")
    env_lines = []
    for key in ["GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
                 "CEREBRAS_API_KEY", "GITHUB_TOKEN", "DEEPSEEK_API_KEY", "MISTRAL_API_KEY"]:
        val = os.environ.get(key, "")
        if val:
            env_lines.append(f"export {key}='{val}'")
    env_b64 = base64.b64encode("\n".join(env_lines).encode()).decode()
    exec_cmd(comp_id, f"echo '{env_b64}' | base64 -d > /agent/.env_keys")
    print(f"  Uploaded {len(env_lines)} keys")

    # Start the web server
    print("\nStarting web server...")
    r = exec_cmd(comp_id,
        "source /agent/.env_keys 2>/dev/null; "
        "cd /agent && nohup python3 -m uvicorn code_review_agent.web_standalone:app "
        "--host 0.0.0.0 --port 8000 > /tmp/server.log 2>&1 & "
        "sleep 3 && tail -5 /tmp/server.log"
    )
    print(f"  {r.get('stdout', '').strip()}")

    # Verify
    print("\nVerifying...")
    time.sleep(2)
    r = exec_cmd(comp_id, "curl -s http://localhost:8000/api/status 2>&1")
    print(f"  Health check: {r.get('stdout', '').strip()}")

    print(f"\n{'='*60}")
    print(f"DEPLOYED SUCCESSFULLY!")
    print(f"Computer ID: {comp_id}")
    print(f"Public URL:  http://{short_id}.orbcloud.dev")
    print(f"API docs:    http://{short_id}.orbcloud.dev/docs")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
