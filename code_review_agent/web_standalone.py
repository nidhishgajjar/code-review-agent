"""Standalone FastAPI web app — zero heavy dependencies.

Uses standalone.py for GitHub + LLM calls (stdlib only).
Only needs: fastapi, uvicorn, python-dotenv.
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

# Import the standalone agent functions
from .standalone import (
    list_prs,
    get_pr_diff,
    call_llm,
    REVIEW_PROMPT,
    TRIAGE_PROMPT,
)

load_dotenv("/agent/.env_keys")
load_dotenv()

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

AVAILABLE_MODELS = [
    {"id": "gemini/gemini-2.5-flash", "name": "Gemini 2.5 Flash", "provider": "Google AI Studio", "context_window": "1M"},
    {"id": "openrouter/qwen/qwen3-coder:free", "name": "Qwen3 Coder", "provider": "OpenRouter", "context_window": "262K"},
    {"id": "groq/llama-3.3-70b-versatile", "name": "Llama 3.3 70B", "provider": "Groq", "context_window": "128K"},
    {"id": "cerebras/llama-3.3-70b", "name": "Llama 3.3 70B", "provider": "Cerebras", "context_window": "128K"},
    {"id": "openrouter/nousresearch/hermes-3-llama-3.1-405b:free", "name": "Hermes 3 405B", "provider": "OpenRouter", "context_window": "131K"},
    {"id": "openrouter/meta-llama/llama-3.3-70b-instruct:free", "name": "Llama 3.3 70B Instruct", "provider": "OpenRouter", "context_window": "128K"},
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
    REVIEW_ALL = "review_all"
    TRIAGE = "triage"

class Job:
    def __init__(self, id: str, type: JobType, status: JobStatus = JobStatus.PENDING):
        self.id = id
        self.type = type
        self.status = status
        self.result: Any = None
        self.error: str | None = None
        self.created_at = datetime.now(timezone.utc).isoformat()

_jobs: dict[str, Job] = {}
_lock = threading.Lock()

def _create_job(jtype: JobType) -> Job:
    job = Job(id=uuid.uuid4().hex, type=jtype)
    with _lock:
        _jobs[job.id] = job
    return job

def _job_dict(job: Job) -> dict:
    return {
        "id": job.id, "type": job.type.value, "status": job.status.value,
        "result": job.result, "error": job.error, "created_at": job.created_at,
    }

# ---------------------------------------------------------------------------
# Background runners (use standalone.py functions directly)
# ---------------------------------------------------------------------------

def _run_scan(job: Job, repo_url: str) -> None:
    job.status = JobStatus.RUNNING
    try:
        job.result = list_prs(repo_url)
        job.status = JobStatus.COMPLETED
    except Exception as e:
        job.error = str(e)
        job.status = JobStatus.FAILED

def _run_review(job: Job, repo_url: str, pr_number: int, model: str) -> None:
    job.status = JobStatus.RUNNING
    try:
        prs = list_prs(repo_url)
        pr = next((p for p in prs if p["number"] == pr_number), None)
        if not pr:
            raise ValueError(f"PR #{pr_number} not found")
        diff = get_pr_diff(repo_url, pr_number)
        prompt = (
            f"## PR #{pr['number']}: {pr['title']}\n"
            f"**Author:** @{pr['author']}\n"
            f"**Branch:** {pr['branch']} -> {pr['base']}\n\n"
            f"### Description\n{pr['body']}\n\n"
            f"### Diff\n```diff\n{diff}\n```"
        )
        review_text = call_llm(model, REVIEW_PROMPT, prompt)
        job.result = {"pr": pr, "review_text": review_text, "posted": False}
        job.status = JobStatus.COMPLETED
    except Exception as e:
        job.error = str(e)
        job.status = JobStatus.FAILED

def _run_review_all(job: Job, repo_url: str, model: str) -> None:
    job.status = JobStatus.RUNNING
    try:
        prs = list_prs(repo_url)
        results = []
        for pr in prs:
            diff = get_pr_diff(repo_url, pr["number"])
            prompt = (
                f"## PR #{pr['number']}: {pr['title']}\n"
                f"**Author:** @{pr['author']}\n"
                f"**Branch:** {pr['branch']} -> {pr['base']}\n\n"
                f"### Description\n{pr['body']}\n\n"
                f"### Diff\n```diff\n{diff}\n```"
            )
            review_text = call_llm(model, REVIEW_PROMPT, prompt)
            results.append({"pr": pr, "review_text": review_text, "posted": False})
        job.result = results
        job.status = JobStatus.COMPLETED
    except Exception as e:
        job.error = str(e)
        job.status = JobStatus.FAILED

def _run_triage(job: Job, repo_url: str, model: str) -> None:
    job.status = JobStatus.RUNNING
    try:
        prs = list_prs(repo_url)
        if not prs:
            job.result = "No open PRs found."
        else:
            summary = "\n".join(
                f"- PR #{p['number']}: {p['title']} by @{p['author']} ({p['changed_files']} files)"
                for p in prs
            )
            job.result = call_llm(model, TRIAGE_PROMPT, summary)
        job.status = JobStatus.COMPLETED
    except Exception as e:
        job.error = str(e)
        job.status = JobStatus.FAILED

def _bg(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "gemini/gemini-2.5-flash"

class ScanRequest(BaseModel):
    repo_url: str
    model: str = Field(default=DEFAULT_MODEL)

class ReviewRequest(BaseModel):
    repo_url: str
    pr_number: int
    model: str = Field(default=DEFAULT_MODEL)

class ReviewAllRequest(BaseModel):
    repo_url: str
    model: str = Field(default=DEFAULT_MODEL)

class TriageRequest(BaseModel):
    repo_url: str
    model: str = Field(default=DEFAULT_MODEL)

class JobResponse(BaseModel):
    job_id: str

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Code Review Agent", version="0.1.0")

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.isfile(html_path):
        with open(html_path) as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Code Review Agent</h1><p>Frontend not found. Use /docs for API.</p>")

@app.get("/api/models")
async def get_models():
    return AVAILABLE_MODELS

@app.get("/api/status")
async def status():
    return {
        "mode": "orb-cloud",
        "active_jobs": sum(1 for j in _jobs.values() if j.status in (JobStatus.PENDING, JobStatus.RUNNING)),
        "total_jobs": len(_jobs),
    }

@app.post("/api/scan", response_model=JobResponse)
async def scan(req: ScanRequest):
    job = _create_job(JobType.SCAN)
    _bg(_run_scan, job, req.repo_url)
    return JobResponse(job_id=job.id)

@app.post("/api/review", response_model=JobResponse)
async def review(req: ReviewRequest):
    job = _create_job(JobType.REVIEW)
    _bg(_run_review, job, req.repo_url, req.pr_number, req.model)
    return JobResponse(job_id=job.id)

@app.post("/api/review-all", response_model=JobResponse)
async def review_all(req: ReviewAllRequest):
    job = _create_job(JobType.REVIEW_ALL)
    _bg(_run_review_all, job, req.repo_url, req.model)
    return JobResponse(job_id=job.id)

@app.post("/api/triage", response_model=JobResponse)
async def triage(req: TriageRequest):
    job = _create_job(JobType.TRIAGE)
    _bg(_run_triage, job, req.repo_url, req.model)
    return JobResponse(job_id=job.id)

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return _job_dict(job)
