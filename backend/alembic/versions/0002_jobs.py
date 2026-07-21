"""create jobs table

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(length=36), primary_key=True),
        sa.Column("account_id", sa.String(length=36),
                  sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="complete"),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_jobs_account_id", "jobs", ["account_id"])
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])
    op.create_index("ix_jobs_status", "jobs", ["status"])


def downgrade() -> None:
    op.drop_table("jobs")
