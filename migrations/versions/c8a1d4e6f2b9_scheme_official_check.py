"""schemes: official-page cross-check (snapshots + current check record)

Revision ID: c8a1d4e6f2b9
Revises: b5f7c3d2e8a1
Create Date: 2026-11-17 20:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8a1d4e6f2b9"
down_revision: Union[str, None] = "b5f7c3d2e8a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("gov_schemes", sa.Column("official_check", postgresql.JSONB(), nullable=True))
    op.create_table(
        "scheme_snapshots",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("scheme_id", sa.UUID(), sa.ForeignKey("gov_schemes.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("final_url", sa.String(length=500), nullable=True),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("chars", sa.Integer(), nullable=False),
        sa.Column("sketch", postgresql.JSONB(), nullable=False),
        sa.Column("key_sentences", postgresql.JSONB(), nullable=False),
        sa.Column("name_found", sa.Boolean(), nullable=False),
        sa.Column("ended_hint", sa.String(length=80), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("scheme_snapshots")
    op.drop_column("gov_schemes", "official_check")
