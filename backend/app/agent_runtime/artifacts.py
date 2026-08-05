"""Durable dataset handles for agent runs.

A run can suspend on a question and resume in a different process, so a
`dataset_id` cannot be a key into an in-memory dict (spec 1.4). Frames
persist as Parquet — which preserves dtypes, unlike CSV — and row-level data
never crosses the model boundary. Tools receive the id; only deterministic
code inside the process ever sees the frame.

Keys live under `accounts/{account_id}/` so the existing
`storage_s3.delete_prefix` account purge covers them.
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

import pandas as pd
from sqlalchemy import select

from .. import storage_s3
from ..db.base import session_scope
from ..db.models import RunArtifactORM
from ..models import RunArtifact


def storage_key(*, dataset_id: str, account_id: str) -> str:
    return f"accounts/{account_id}/artifacts/{dataset_id}.parquet"


def put_dataset(
    *, run_id: str, account_id: str, df: pd.DataFrame, label: str,
) -> str:
    dataset_id = str(uuid.uuid4())
    key = storage_key(dataset_id=dataset_id, account_id=account_id)

    buffer = io.BytesIO()
    df.to_parquet(buffer, index=False)
    storage_s3.put_object(key, buffer.getvalue())

    artifact = RunArtifact(
        id=dataset_id,
        run_id=run_id,
        account_id=account_id,
        kind="dataset",
        label=label,
        storage_key=key,
        row_count=int(len(df)),
        columns=[str(c) for c in df.columns],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    with session_scope() as s:
        s.add(RunArtifactORM(
            id=artifact.id, run_id=run_id, account_id=account_id,
            kind=artifact.kind, payload=artifact.model_dump(mode="json"),
        ))
    return dataset_id


def _load_row(*, dataset_id: str, account_id: str) -> RunArtifact:
    with session_scope() as s:
        row = s.scalar(
            select(RunArtifactORM).where(
                RunArtifactORM.id == dataset_id,
                RunArtifactORM.account_id == account_id,
            )
        )
        if row is None:
            raise KeyError(f"dataset {dataset_id} not found for this account")
        return RunArtifact.model_validate(row.payload)


def get_dataset(*, dataset_id: str, account_id: str) -> pd.DataFrame:
    artifact = _load_row(dataset_id=dataset_id, account_id=account_id)
    raw = storage_s3.get_object(artifact.storage_key)
    return pd.read_parquet(io.BytesIO(raw))


def describe(*, dataset_id: str, account_id: str) -> Dict[str, Any]:
    """Bounded summary safe to return to the model — no row data."""
    artifact = _load_row(dataset_id=dataset_id, account_id=account_id)
    return {
        "dataset_id": artifact.id,
        "label": artifact.label,
        "row_count": artifact.row_count,
        "columns": artifact.columns,
    }
