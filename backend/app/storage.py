"""Filesystem-backed job storage for the pilot."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import data_dir
from .memory.fsutil import atomic_write_json

DATA_DIR = data_dir()
JOBS_DIR = DATA_DIR / "jobs"
UPLOADS_DIR = DATA_DIR / "uploads"

JOB_TTL_SECONDS = 7 * 24 * 3600
UPLOAD_TTL_SECONDS = 24 * 3600


def ensure_dirs():
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def job_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def save_job(job_id: str, payload: Dict[str, Any]) -> None:
    ensure_dirs()
    atomic_write_json(job_path(job_id), payload)


def load_job(job_id: str) -> Optional[Dict[str, Any]]:
    p = job_path(job_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list_jobs(account_id: str, limit: int = 50) -> list:
    """Lightweight job listing for one account, newest first.

    O(n) scan over the jobs dir — fine at pilot scale (jobs expire after 7
    days); Postgres replaces this in Phase 2.
    """
    ensure_dirs()
    out = []
    for p in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("account_id") != account_id:
            continue
        s = job.get("summary") or {}
        out.append({
            "job_id": job.get("job_id"),
            "account_id": job.get("account_id"),
            "created_at": job.get("created_at"),
            "status": job.get("status", "complete"),
            "filenames": job.get("filenames"),
            "recon_type": (job.get("config") or {}).get("recon_type"),
            "label_a": (job.get("config") or {}).get("label_a"),
            "label_b": (job.get("config") or {}).get("label_b"),
            "matched_pct": s.get("matched_pct"),
            "discrepancies": s.get("discrepancies"),
            "total_discrepancy_value": s.get("total_discrepancy_value"),
        })
    out.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return out[:limit]


def cleanup():
    ensure_dirs()
    now = time.time()
    for p in UPLOADS_DIR.glob("*"):
        try:
            if now - p.stat().st_mtime > UPLOAD_TTL_SECONDS:
                p.unlink()
        except OSError:
            pass
    for p in JOBS_DIR.glob("*.json"):
        try:
            if now - p.stat().st_mtime > JOB_TTL_SECONDS:
                p.unlink()
        except OSError:
            pass
