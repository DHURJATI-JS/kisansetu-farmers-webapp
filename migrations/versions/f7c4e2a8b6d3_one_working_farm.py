"""one working farm per person (employee, supervisor, manager or accountant); own / co-owned farms don't count

Revision ID: f7c4e2a8b6d3
Revises: e6a2c9f4b8d1
Create Date: 2026-11-18 14:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7c4e2a8b6d3"
down_revision: Union[str, None] = "e6a2c9f4b8d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("uq_one_working_farm", "farm_members", ["user_id"], unique=True,
                    postgresql_where=sa.text("status <> 'ended' AND role IN ('manager', 'accountant', 'supervisor', 'employee')"))


def downgrade() -> None:
    op.drop_index("uq_one_working_farm", table_name="farm_members")
