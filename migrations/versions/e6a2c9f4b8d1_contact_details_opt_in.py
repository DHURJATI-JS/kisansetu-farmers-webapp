"""contact details in the community directory are opt-in

Phone and email used to be shown to every signed-in account unless the person had switched it off — nobody was asked at
sign-up. From now on they are hidden until the person turns "Let other farmers see my phone and email" on in the account,
and everybody starts hidden (no consent was collected).

Revision ID: e6a2c9f4b8d1
Revises: d4e7b1a9c3f2
Create Date: 2026-11-18 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e6a2c9f4b8d1"
down_revision: Union[str, None] = "d4e7b1a9c3f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("users", "contact_visible", server_default=sa.false())
    op.execute("UPDATE users SET contact_visible = false WHERE contact_visible")


def downgrade() -> None:
    op.alter_column("users", "contact_visible", server_default=None)
