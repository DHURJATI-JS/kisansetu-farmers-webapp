"""outbound messages can carry media (WhatsApp picture of the salary payment screenshot)

Revision ID: 8b2d6c1e4f0a
Revises: 600140e58f6e
Create Date: 2026-11-17 13:10:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8b2d6c1e4f0a"
down_revision: Union[str, None] = "600140e58f6e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("outbound_messages", sa.Column("media_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("outbound_messages", "media_url")
