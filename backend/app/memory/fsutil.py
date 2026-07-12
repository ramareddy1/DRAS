"""Atomic JSON persistence + per-account advisory locking.

Every store that does read-modify-write on a per-account file must:
  1. hold `account_lock(account_id)` across the read AND the write, and
  2. persist via `atomic_write_json` (write temp file, then os.replace)
so a crash mid-write can never leave a truncated file, and two concurrent
requests can never interleave a lost update.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from filelock import FileLock

from ..config import data_dir

DATA_DIR = data_dir()


def atomic_write_json(path: Path, payload: Any, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str, indent=indent))
        os.replace(tmp, path)  # atomic on same volume, incl. Windows
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def account_lock(account_id: str, timeout: float = 10.0) -> FileLock:
    lock_dir = DATA_DIR / "accounts" / account_id
    lock_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(str(lock_dir / ".lock"), timeout=timeout)
