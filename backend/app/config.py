"""Central config.

The data directory resolves to `<backend>/data` regardless of the process
working directory (running uvicorn from the repo root used to silently split
data into a second tree). `RECONOPS_DATA_DIR` overrides — the eval and the
tests set it before (re)importing app modules, so `data_dir()` is a function
that reads the environment at call time rather than a captured constant.
"""
from __future__ import annotations

import os
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    return Path(os.getenv("RECONOPS_DATA_DIR") or (_BACKEND_ROOT / "data"))
