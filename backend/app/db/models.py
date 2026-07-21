"""SQLAlchemy ORM models mirroring the Pydantic schemas in app/models.py.

Each table carries real columns only for what's queried, filtered, or
foreign-keyed; everything else lives in a `payload` JSONB column holding
`<Model>.model_dump(mode="json")`. This mirrors the Pydantic schema exactly
(the column *is* the schema, serialized) without normalizing structures
nothing ever queries by individual field.
"""
from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB

from .base import Base


class AccountORM(Base):
    __tablename__ = "accounts"

    id = Column(String(36), primary_key=True)
    payload = Column(JSONB, nullable=False, default=dict)


class JobORM(Base):
    __tablename__ = "jobs"

    job_id = Column(String(36), primary_key=True)
    account_id = Column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True, index=True)
    created_at = Column(String, nullable=True, index=True)
    status = Column(String, nullable=False, default="complete")
    payload = Column(JSONB, nullable=False, default=dict)


class RuleORM(Base):
    __tablename__ = "rules"

    id = Column(String(36), primary_key=True)
    account_id = Column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    payload = Column(JSONB, nullable=False, default=dict)
