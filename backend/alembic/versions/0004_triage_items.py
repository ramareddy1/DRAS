"""create triage_items table

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "triage_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("account_id", sa.String(length=36),
                  sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("signature", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_triage_items_account_id", "triage_items", ["account_id"])
    op.create_index("ix_triage_items_signature", "triage_items", ["signature"])
    op.create_index("ix_triage_items_state", "triage_items", ["state"])


def downgrade() -> None:
    op.drop_table("triage_items")
