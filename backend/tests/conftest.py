"""Shared Postgres test fixture — every backend test shares one database.

Individual account/job/rule/etc. isolation used to come from `tmp_path` +
`RECONOPS_DATA_DIR`; now that those stores live in Postgres, isolation comes
from truncating every ORM-mapped table after each test instead. Existing
tests that still `monkeypatch.setenv("RECONOPS_DATA_DIR", ...)` and
`importlib.reload(...)` these modules keep working unchanged — that env var
now only affects lock-file location (harmless), not data.
"""
from __future__ import annotations

import os

os.environ.setdefault(
    "RECONOPS_DATABASE_URL",
    "postgresql://reconops:reconops@localhost:5432/reconops_test",
)

import pytest
from sqlalchemy import text

from app.db.base import Base, get_engine
from app.db import models  # noqa: F401 — registers ORM classes on Base.metadata


@pytest.fixture(autouse=True)
def _clean_db():
    engine = get_engine()
    Base.metadata.create_all(engine)
    yield
    table_names = ", ".join(f'"{t}"' for t in Base.metadata.tables.keys())
    if table_names:
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
