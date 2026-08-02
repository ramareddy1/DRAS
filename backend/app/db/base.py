"""SQLAlchemy engine/session plumbing for Postgres-backed stores.

Reads `RECONOPS_DATABASE_URL` lazily (first call), mirroring
`config.data_dir()`'s env-read-at-call-time pattern so tests can set the env
var before first use without import-order issues. The engine/session
factory is cached at module scope and reused for the process's lifetime —
unlike a per-account file lock, a connection pool is expensive to recreate.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal = None


def _database_url() -> str:
    url = os.environ.get("RECONOPS_DATABASE_URL")
    if not url:
        raise RuntimeError("RECONOPS_DATABASE_URL is not set")
    return url


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(_database_url(), pool_pre_ping=True, future=True)
        _SessionLocal = sessionmaker(bind=_engine, future=True)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
