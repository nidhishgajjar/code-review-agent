"""ORB Cloud client — deploys persistent agent processes.

Each user gets a computer with a long-running HTTP server (standalone.py)
deployed via POST /agents. Tasks are sent to the agent's public URL.
ORB checkpoints the agent during LLM calls.
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import httpx

ORB_BASE = "https://api.orbcloud.dev"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ORB_TOML_PATH = _PROJECT_ROOT / "orb.toml"
_STANDALONE_PATH = Path(__file__).resolve().parent / "standalone.py"


class OrbClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("ORB_API_KEY", "")
        if not self.api_key:
            raise ValueError("ORB_API_KEY is required")
        self._http = httpx.Client(
            base_url=ORB_BASE,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            timeout=600.0,
        )

    # ------------------------------------------------------------------
    # Low-level ORB API
    # ------------------------------------------------------------------

    def _post(self, path: str, **kwargs) -> dict:
        resp = self._http.post(path, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str) -> dict:
        resp = self._http.get(path)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> dict:
        resp = self._http.delete(path)
        resp.raise_for_status()
        return resp.json()

    def create_computer(self, name: str, runtime_mb: int = 8192, disk_mb: int = 16384) -> dict:
        return self._post("/v1/computers", json={"name": name, "runtime_mb": runtime_mb, "disk_mb": disk_mb})

    def exec_command(self, computer_id: str, command: str) -> dict:
        return self._post(f"/v1/computers/{computer_id}/exec", json={"command": command})

    def get_computer(self, computer_id: str) -> dict:
        return self._get(f"/v1/computers/{computer_id}")

    def delete_computer(self, computer_id: str) -> dict:
        return self._delete(f"/v1/computers/{computer_id}")

    def list_computers(self) -> dict:
        return self._get("/v1/computers")

    def find_computer_by_name(self, name: str) -> dict | None:
        data = self.list_computers()
        for comp in data.get("computers", []):
            if comp.get("name") == name:
                return comp
        return None

    # ------------------------------------------------------------------
    # Deploy agent as persistent process
    # ------------------------------------------------------------------

    def deploy_agent(self, computer_id: str) -> dict:
        """Deploy the agent process via POST /agents. Returns agent info."""
        return self._post(f"/v1/computers/{computer_id}/agents", json={})

    def list_agents(self, computer_id: str) -> dict:
        return self._get(f"/v1/computers/{computer_id}/agents")

    def get_agent_url(self, computer_id: str) -> str:
        """Get the public URL for the agent's HTTP server."""
        short_id = computer_id[:8]
        return f"https://{short_id}.orbcloud.dev"

    # ------------------------------------------------------------------
    # Provision: create, build, upload, deploy
    # ------------------------------------------------------------------

    def get_or_create_computer(self, user_id: str) -> dict:
        """Get existing computer or provision a new one with agent deployed."""
        name = f"review-{user_id}"

        # Check for existing computer
        existing = self.find_computer_by_name(name)
        if existing and existing.get("status") in ("running", "ready", "created", "active"):
            # Verify agent is reachable via public URL
            agent_url = self.get_agent_url(existing["id"])
            try:
                r = httpx.get(f"{agent_url}/health", timeout=5)
                if r.status_code == 200:
                    return existing
            except Exception:
                pass
            # Agent unreachable — try redeploying
            try:
                self.deploy_agent(existing["id"])
                time.sleep(5)
                r = httpx.get(f"{agent_url}/health", timeout=5)
                if r.status_code == 200:
                    return existing
            except Exception:
                pass
            # Still unreachable — return it anyway, don't delete state
            return existing

        # Create new computer
        comp = self.create_computer(name=name)
        comp_id = comp["id"]

        # Upload orb.toml
        toml_content = _ORB_TOML_PATH.read_text()
        self._http.post(
            f"/v1/computers/{comp_id}/config",
            content=toml_content.encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/toml"},
        ).raise_for_status()

        # Build — clones repo from GitHub + installs openhands-ai
        self._http.post(
            f"/v1/computers/{comp_id}/build",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        ).raise_for_status()

        # Upload API keys via exec (only thing not in the repo)
        env_lines = []
        for key in ["GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
                     "CEREBRAS_API_KEY", "GITHUB_TOKEN", "DEEPSEEK_API_KEY", "MISTRAL_API_KEY",
                     "GLM_API_KEY"]:
            val = os.environ.get(key, "")
            if val:
                env_lines.append(f"export {key}='{val}'")
        if env_lines:
            env_b64 = base64.b64encode("\n".join(env_lines).encode()).decode()
            for attempt in range(3):
                try:
                    self.exec_command(comp_id, f"echo '{env_b64}' | base64 -d > /agent/code/.env_keys")
                    break
                except Exception:
                    time.sleep(5)

        # Deploy agent as persistent process
        self.deploy_agent(comp_id)

        # Wait for agent to start
        agent_url = self.get_agent_url(comp_id)
        for _ in range(10):
            time.sleep(3)
            try:
                r = httpx.get(f"{agent_url}/health", timeout=5)
                if r.status_code == 200:
                    break
            except Exception:
                continue

        return comp

    # ------------------------------------------------------------------
    # Send tasks to the agent's HTTP server
    # ------------------------------------------------------------------

    def send_task(self, computer_id: str, task: dict, retries: int = 3) -> dict:
        """POST a task to the agent. For reviews, polls for completion."""
        import time as _time
        agent_url = self.get_agent_url(computer_id)

        # Submit task
        last_err = None
        for attempt in range(retries):
            try:
                resp = httpx.post(agent_url, json=task, timeout=30.0)
                if resp.status_code in (502, 503) and attempt < retries - 1:
                    _time.sleep(5)
                    continue
                try:
                    data = resp.json()
                except Exception:
                    if resp.status_code >= 400:
                        return {"error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
                    return {"error": resp.text}

                # If it returned a job_id, poll for completion
                if "job_id" in data:
                    job_id = data["job_id"]
                    for _ in range(180):  # poll for up to 15 minutes
                        _time.sleep(5)
                        try:
                            r = httpx.get(f"{agent_url}/jobs/{job_id}", timeout=10.0)
                            job = r.json()
                            if job.get("status") == "completed":
                                return job.get("result", job)
                            elif job.get("status") == "failed":
                                return {"error": job.get("error", "Job failed")}
                        except Exception:
                            continue
                    return {"error": "Job timed out after 15 minutes"}

                # Direct response (scan, history)
                return data

            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    _time.sleep(5)
        return {"error": str(last_err)}

    def get_memory(self, computer_id: str) -> str:
        """Get agent's accumulated memory via its HTTP API."""
        agent_url = self.get_agent_url(computer_id)
        try:
            resp = httpx.get(f"{agent_url}/memory", timeout=10)
            return resp.json().get("content", "")
        except Exception:
            return ""

    def get_reviews(self, computer_id: str, repo_slug: str) -> list[dict]:
        """Get review history via the agent's HTTP API."""
        agent_url = self.get_agent_url(computer_id)
        dir_name = repo_slug.replace("/", "-")
        try:
            resp = httpx.get(f"{agent_url}/reviews/{dir_name}", timeout=10)
            return resp.json().get("reviews", [])
        except Exception:
            return []

    def destroy_computer(self, computer_id: str) -> dict:
        return self.delete_computer(computer_id)
