"""remove global user fields from global_settings

Revision ID: c11f9d2b3a4
Revises: b85f41ed1fb8
Create Date: 2026-07-06 09:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c11f9d2b3a4"
down_revision: Union[str, Sequence[str], None] = "b85f41ed1fb8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop username columns from global_settings; keep urls and company
    with op.batch_alter_table("global_settings") as batch_op:
        # guard: only drop if column exists will be handled by DB admin if needed
        batch_op.drop_column("fiorilli_user")
        batch_op.drop_column("ahgora_user")


def downgrade() -> None:
    # Recreate the username columns (non-nullable with empty default to be safe)
    with op.batch_alter_table("global_settings") as batch_op:
        batch_op.add_column(
            sa.Column("fiorilli_user", sa.String(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("ahgora_user", sa.String(), nullable=False, server_default="")
        )
