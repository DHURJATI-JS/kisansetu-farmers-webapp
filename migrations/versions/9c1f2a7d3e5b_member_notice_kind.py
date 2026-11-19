"""farm members: notice kind (individual notice pauses leadership powers; closure notice does not)

Revision ID: 9c1f2a7d3e5b
Revises: 8b2d6c1e4f0a
Create Date: 2026-11-17 18:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9c1f2a7d3e5b"
down_revision: Union[str, None] = "8b2d6c1e4f0a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("farm_members", sa.Column("notice_kind", sa.String(length=16), nullable=True))
    # existing notices: a closing farm or branch means a closure notice, anything else was given to this person
    op.execute("""
        UPDATE farm_members m SET notice_kind = CASE
            WHEN EXISTS (SELECT 1 FROM farms f WHERE f.id = m.farm_id AND f.status = 'closing')
              OR EXISTS (SELECT 1 FROM branches b WHERE b.id = m.branch_id AND b.status = 'closing') THEN 'closure'
            ELSE 'individual' END
        WHERE m.status = 'notice'
    """)


def downgrade() -> None:
    op.drop_column("farm_members", "notice_kind")
