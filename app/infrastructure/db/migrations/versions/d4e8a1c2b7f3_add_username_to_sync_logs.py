"""Add username to sync_logs

Revision ID: d4e8a1c2b7f3
Revises: c11f9d2b3a4
Create Date: 2026-09-30 12:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4e8a1c2b7f3"
down_revision: Union[str, Sequence[str], None] = "c11f9d2b3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("sync_logs", sa.Column("username", sa.String(), nullable=True))
    op.create_index(
        op.f("ix_sync_logs_username"), "sync_logs", ["username"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_sync_logs_username"), table_name="sync_logs")
    op.drop_column("sync_logs", "username")
