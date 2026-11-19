"""job applications: where the distance was measured from (live position, saved GPS location or a typed address)

Revision ID: d4e7b1a9c3f2
Revises: c8a1d4e6f2b9
Create Date: 2026-11-17 23:30:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e7b1a9c3f2"
down_revision: Union[str, None] = "c8a1d4e6f2b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("job_applications", sa.Column("location_source", sa.String(length=8), nullable=True))
    # every earlier application was made with the live position (the only way until now)
    op.execute("UPDATE job_applications SET location_source = 'live' WHERE location_source IS NULL")


def downgrade() -> None:
    op.drop_column("job_applications", "location_source")
