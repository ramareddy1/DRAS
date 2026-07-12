"""Filesystem-backed job storage for the pilot."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

DATA_DIR = Path(os.getenv("RECONOPS_DATA_DIR", "data"))
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
    job_path(job_id).write_text(json.dumps(payload, default=str), encoding="utf-8")


def load_job(job_id: str) -> Optional[Dict[str, Any]]:
    p = job_path(job_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


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
