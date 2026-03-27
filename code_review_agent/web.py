"""FastAPI web app — local server, dispatches to persistent ORB agents.

Each user gets an ORB computer with a deployed HTTP agent.
Tasks are sent via the agent's public URL (not exec).
ORB checkpoints the agent during LLM calls.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .persistence import UserStore

load_dotenv()

_orb_client = None
_user_store = UserStore()


def _get_orb():
    global _orb_client
    if _orb_client is None:
        from .orb_client import OrbClient
        _orb_client = OrbClient()
    return _orb_client


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

AVAILABLE_MODELS = [
    {"id": "gemini/gemini-2.5-flash", "name": "Gemini 2.5 Flash", "provider": "Google AI Studio", "context_window": "1M"},
    {"id": "gemini/gemini-2.5-pro", "name": "Gemini 2.5 Pro", "provider": "Google AI Studio", "context_window": "1M"},
    {"id": "openrouter/qwen/qwen3-coder:free", "name": "Qwen3 Coder", "provider": "OpenRouter", "context_window": "262K"},
    {"id": "groq/llama-3.3-70b-versatile", "name": "Llama 3.3 70B", "provider": "Groq", "context_window": "128K"},
    {"id": "cerebras/llama-3.3-70b", "name": "Llama 3.3 70B", "provider": "Cerebras", "context_window": "128K"},
]

# ---------------------------------------------------------------------------
# Job tracking
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class JobType(str, Enum):
    SCAN = "scan"
    REVIEW = "review"

class Job:
    __slots__ = ("id", "type", "status", "user_id", "result", "error", "created_at", "computer_id")

    def __init__(self, id: str, type: JobType, user_id: str):
        self.id = id
        self.type = type
        self.status = JobStatus.PENDING
        self.user_id = user_id
        self.result: Any = None
        self.error: str | None = None
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.computer_id: str | None = None

_jobs: dict[str, Job] = {}
_lock = threading.Lock()

def _create_job(jtype: JobType, user_id: str) -> Job:
    job = Job(id=uuid.uuid4().hex, type=jtype, user_id=user_id)
    with _lock:
        _jobs[job.id] = job
    return job

def _job_dict(job: Job) -> dict:
    d = {
        "id": job.id, "type": job.type.value, "status": job.status.value,
        "result": job.result, "error": job.error, "created_at": job.created_at,
    }
    if job.computer_id:
        d["computer_id"] = job.computer_id
    return d

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _get_user_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def _require_user(request: Request) -> tuple[str, str]:
    """Returns (user_id, computer_id). Provisions computer + deploys agent if needed."""
    token = _get_user_token(request)
    if not token:
        raise HTTPException(401, "Missing Authorization: Bearer <token>. Call POST /api/auth first.")
    user = _user_store.get_user(token)
    if not user:
        raise HTTPException(401, "Invalid token.")

    orb = _get_orb()
    comp = orb.get_or_create_computer(user.user_id)
    comp_id = comp["id"]

    if user.computer_id != comp_id:
        _user_store.set_computer(token, comp_id, comp.get("name", ""))

    return user.user_id, comp_id

# ---------------------------------------------------------------------------
# Task execution — sends to agent's public URL (not exec)
# ---------------------------------------------------------------------------

def _run_task(job: Job, comp_id: str, task: dict) -> None:
    job.status = JobStatus.RUNNING
    job.computer_id = comp_id
    try:
        orb = _get_orb()
        result = orb.send_task(comp_id, task)

        if "error" in result:
            job.error = result["error"]
            job.status = JobStatus.FAILED
        else:
            job.result = result
            job.status = JobStatus.COMPLETED
    except Exception as exc:
        job.error = str(exc)
        job.status = JobStatus.FAILED


def _start_job(job: Job, comp_id: str, task: dict) -> None:
    threading.Thread(target=_run_task, args=(job, comp_id, task), daemon=True).start()

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "gemini/gemini-2.5-flash"

class ScanRequest(BaseModel):
    repo_url: str

class ReviewRequest(BaseModel):
    repo_url: str
    pr_number: int
    model: str = Field(default=DEFAULT_MODEL)

class JobResponse(BaseModel):
    job_id: str

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Code Review Agent",
    description="Stateful AI code review — persistent ORB agents with checkpoint/restore.",
    version="0.3.0",
)

@app.get("/", response_class=HTMLResponse)
async def index():
    html = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.isfile(html):
        with open(html) as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>Code Review Agent</h1><p>Use /docs for API.</p>")

@app.get("/api/models")
async def get_models():
    return AVAILABLE_MODELS

@app.get("/api/status")
async def status():
    return {
        "mode": "stateful-orb-agents",
        "active_jobs": sum(1 for j in _jobs.values() if j.status in (JobStatus.PENDING, JobStatus.RUNNING)),
        "total_jobs": len(_jobs),
    }

# --- Auth ---

@app.post("/api/auth")
async def create_session():
    user = _user_store.create_user()
    return {"token": user.token, "user_id": user.user_id}

@app.get("/api/me")
async def get_me(request: Request):
    token = _get_user_token(request)
    if not token:
        raise HTTPException(401, "Missing token")
    user = _user_store.get_user(token)
    if not user:
        raise HTTPException(401, "Invalid token")

    result = {
        "user_id": user.user_id,
        "computer_id": user.computer_id,
        "created_at": user.created_at,
        "last_active": user.last_active,
        "computer_status": "not_provisioned",
    }
    if user.computer_id:
        try:
            orb = _get_orb()
            comp = orb.get_computer(user.computer_id)
            result["computer_status"] = comp.get("status", "unknown")
            result["agent_url"] = orb.get_agent_url(user.computer_id)
        except Exception:
            result["computer_status"] = "not_found"
    return result

# --- Core actions (send tasks to ORB agent) ---

@app.post("/api/scan", response_model=JobResponse)
async def scan(req: ScanRequest, request: Request):
    user_id, comp_id = _require_user(request)
    job = _create_job(JobType.SCAN, user_id)
    _start_job(job, comp_id, {"action": "scan", "repo_url": req.repo_url})
    return JobResponse(job_id=job.id)

@app.post("/api/review", response_model=JobResponse)
async def review(req: ReviewRequest, request: Request):
    user_id, comp_id = _require_user(request)
    job = _create_job(JobType.REVIEW, user_id)
    _start_job(job, comp_id, {
        "action": "review",
        "repo_url": req.repo_url,
        "pr_number": req.pr_number,
        "model": req.model,
    })
    return JobResponse(job_id=job.id)

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _job_dict(job)

# --- History & Memory (proxied from ORB agent) ---

@app.get("/api/reviews/{owner}/{repo}")
async def get_review_history(owner: str, repo: str, request: Request):
    user_id, comp_id = _require_user(request)
    orb = _get_orb()
    return orb.get_reviews(comp_id, f"{owner}/{repo}")

@app.get("/api/memory")
async def get_memory(request: Request):
    user_id, comp_id = _require_user(request)
    orb = _get_orb()
    return {"content": orb.get_memory(comp_id)}

# --- Computer management ---

@app.get("/api/computer")
async def get_computer(request: Request):
    user_id, comp_id = _require_user(request)
    orb = _get_orb()
    comp = orb.get_computer(comp_id)
    comp["agent_url"] = orb.get_agent_url(comp_id)
    return comp

@app.delete("/api/computer")
async def destroy_computer(request: Request):
    token = _get_user_token(request)
    if not token:
        raise HTTPException(401, "Missing token")
    user = _user_store.get_user(token)
    if not user or not user.computer_id:
        raise HTTPException(404, "No computer")
    orb = _get_orb()
    orb.destroy_computer(user.computer_id)
    _user_store.set_computer(token, None, None)
    return {"status": "destroyed"}
