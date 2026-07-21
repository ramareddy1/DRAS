"""Postgres-backed job storage for the pilot."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from .db.base import session_scope
from .db.models import JobORM

JOB_TTL_SECONDS = 7 * 24 * 3600

_JOB_COLUMNS = {"account_id", "created_at", "status"}


def save_job(job_id: str, payload: Dict[str, Any]) -> None:
    flat = dict(payload)
    account_id = flat.pop("account_id", None)
    created_at = flat.pop("created_at", None)
    status = flat.pop("status", "complete")
    flat.pop("job_id", None)
    with session_scope() as s:
        row = s.get(JobORM, job_id)
        if row is None:
            row = JobORM(job_id=job_id)
            s.add(row)
        row.account_id = account_id
        row.created_at = created_at
        row.status = status
        row.payload = flat


def load_job(job_id: str) -> Optional[Dict[str, Any]]:
    with session_scope() as s:
        row = s.get(JobORM, job_id)
        if row is None:
            return None
        out = dict(row.payload or {})
        out["job_id"] = row.job_id
        out["account_id"] = row.account_id
        out["created_at"] = row.created_at
        out["status"] = row.status
        return out


def update_job(job_id: str, **fields: Any) -> None:
    """Merge fields into an existing job, preserving the rest.

    Raises FileNotFoundError if the job doesn't exist — callers only ever
    update a job they just created with save_job().
    """
    with session_scope() as s:
        row = s.get(JobORM, job_id)
        if row is None:
            raise FileNotFoundError(f"job {job_id} not found")
        payload = dict(row.payload or {})
        for k, v in fields.items():
            if k in _JOB_COLUMNS:
                setattr(row, k, v)
            else:
                payload[k] = v
        row.payload = payload


def list_jobs(account_id: str, limit: int = 50) -> list:
    """Lightweight job listing for one account, newest first."""
    with session_scope() as s:
        rows = (
            s.query(JobORM)
            .filter(JobORM.account_id == account_id)
            .order_by(JobORM.created_at.desc())
            .limit(limit)
            .all()
        )
        out = []
        for row in rows:
            payload = row.payload or {}
            summary = payload.get("summary") or {}
            cfg = payload.get("config") or {}
            out.append({
                "job_id": row.job_id,
                "account_id": row.account_id,
                "created_at": row.created_at,
                "status": row.status,
                "filenames": payload.get("filenames"),
                "recon_type": cfg.get("recon_type"),
                "label_a": cfg.get("label_a"),
                "label_b": cfg.get("label_b"),
                "matched_pct": summary.get("matched_pct"),
                "discrepancies": summary.get("discrepancies"),
                "total_discrepancy_value": summary.get("total_discrepancy_value"),
            })
        return out


def reap_stale_jobs() -> int:
    """Mark every job still 'processing' as failed.

    Called once at process startup. A background job's execution thread
    cannot survive the process that started it being killed and restarted,
    so any job still 'processing' when a fresh process boots was orphaned by
    a crash — this doesn't need a staleness/time-window check.
    """
    with session_scope() as s:
        rows = s.query(JobORM).filter(JobORM.status == "processing").all()
        count = 0
        for row in rows:
            row.status = "error"
            payload = dict(row.payload or {})
            payload["error"] = "Worker restarted while this job was processing."
            row.payload = payload
            count += 1
        return count


def cleanup() -> None:
    """Delete jobs older than JOB_TTL_SECONDS, and their S3 uploads if any."""
    from . import storage_s3

    cutoff = (datetime.utcnow() - timedelta(seconds=JOB_TTL_SECONDS)).isoformat() + "Z"
    with session_scope() as s:
        rows = s.query(JobORM).filter(JobORM.created_at < cutoff).all()
        for row in rows:
            uploaded = (row.payload or {}).get("uploaded_files") or {}
            for side in ("a", "b"):
                key = (uploaded.get(side) or {}).get("key")
                if key:
                    try:
                        storage_s3.delete_object(key)
                    except Exception:
                        pass
            s.delete(row)
