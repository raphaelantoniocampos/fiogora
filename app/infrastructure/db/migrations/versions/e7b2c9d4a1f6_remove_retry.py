"""Remove the sync retry feature

Revision ID: e7b2c9d4a1f6
Revises: d4e8a1c2b7f3
Create Date: 2026-10-13 11:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e7b2c9d4a1f6"
down_revision: Union[str, Sequence[str], None] = "d4e8a1c2b7f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Syncs waiting for a retry that will never happen ("retrying" is no longer a status)
    op.execute(
        "UPDATE sync_jobs SET status = 'failed', "
        "error_message = coalesce(error_message, 'Falha na sincronização'), "
        "finished_at = coalesce(finished_at, started_at, created_at) "
        "WHERE status = 'retrying'"
    )
    # Each sync kept the (encrypted) passwords only so the scheduler could retry it
    op.execute(
        "UPDATE sync_jobs SET metadata = (metadata::jsonb - '_encrypted_credentials')::json "
        "WHERE metadata IS NOT NULL"
    )
    op.drop_column("sync_jobs", "next_retry_at")
    op.drop_column("sync_jobs", "retry_count")
    op.drop_column("automation_tasks", "retry_count")


def downgrade() -> None:
    """Downgrade schema (the removed data is not restored)."""
    op.add_column(
        "automation_tasks",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "sync_jobs",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("sync_jobs", sa.Column("next_retry_at", sa.DateTime(), nullable=True))
