"""empty message

Revision ID: 1ffbaa1b1145
Revises: f10380cf5957
Create Date: 2026-05-22 12:15:55.610777

"""

from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = "1ffbaa1b1145"
down_revision: Union[str, Sequence[str], None] = "f10380cf5957"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
