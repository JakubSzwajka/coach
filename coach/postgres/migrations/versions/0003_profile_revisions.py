"""Add optimistic revisions for Profile commands.

Revision ID: 0003_profile_revisions
Revises: 0002_profile_source_captures
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_profile_revisions"
down_revision = "0002_profile_source_captures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "profiles",
        sa.Column(
            "revision",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_profile_revision_nonnegative",
        "profiles",
        "revision >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_profile_revision_nonnegative",
        "profiles",
        type_="check",
    )
    op.drop_column("profiles", "revision")
