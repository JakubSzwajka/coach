"""Bound active collection admission to one job per source.

Revision ID: 0007_collection_job_admission
Revises: 0006_dashboard_observations
"""

from __future__ import annotations

from alembic import op

revision = "0007_collection_job_admission"
down_revision = "0006_dashboard_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Admission is authoritative in PostgreSQL: concurrent adapters and HTTP
    # requests cannot create an unbounded queue behind the Profile lock.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_collection_job_active_source
        ON collection_jobs (profile_id, source_connection_id)
        WHERE state IN ('requested', 'running')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_collection_job_active_source")
