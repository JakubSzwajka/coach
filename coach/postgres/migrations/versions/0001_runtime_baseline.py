"""Establish the PostgreSQL migration baseline.

Revision ID: 0001_runtime_baseline
Revises: None
"""

from __future__ import annotations

revision = "0001_runtime_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Reserve the first revision; domain tables land in later vertical slices."""


def downgrade() -> None:
    """Return to an unmigrated empty database."""
