"""create metrics table

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metrics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.String(length=36),
                  sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_metrics_account_id", "metrics", ["account_id"])


def downgrade() -> None:
    op.drop_table("metrics")
