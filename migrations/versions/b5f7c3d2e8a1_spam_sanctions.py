"""spam sanctions: escalating IP bans with evidence, account posting bans

Revision ID: b5f7c3d2e8a1
Revises: 9c1f2a7d3e5b
Create Date: 2026-11-17 19:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5f7c3d2e8a1"
down_revision: Union[str, None] = "9c1f2a7d3e5b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ip_bans", sa.Column("level", sa.Integer(), server_default="0", nullable=False))
    op.add_column("ip_bans", sa.Column("shared", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("ip_bans", sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("ip_bans", sa.Column("evidence", postgresql.JSONB(), nullable=True))
    op.add_column("ip_bans", sa.Column("lifted_by_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("ip_bans", sa.Column("lift_note", sa.String(length=300), nullable=True))
    op.add_column("users", sa.Column("posting_banned_until", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "user_sanctions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("created_by_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifted_by_id", sa.UUID(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("lift_note", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("user_sanctions")
    op.drop_column("users", "posting_banned_until")
    for c in ("lift_note", "lifted_by_id", "evidence", "user_id", "shared", "level"):
        op.drop_column("ip_bans", c)
