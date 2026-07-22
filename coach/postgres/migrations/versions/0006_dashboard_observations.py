"""Add the existing dashboard's controlled observation definitions.

Revision ID: 0006_dashboard_observations
Revises: 0005_transactional_app_records
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_dashboard_observations"
down_revision = "0005_transactional_app_records"
branch_labels = None
depends_on = None

_DEFINITIONS = (
    ("daily_steps", "integer", "count", "Source-reported steps for one local date."),
    ("daily_distance", "decimal", "metres", "Source-reported distance for one local date."),
    ("daily_active_energy", "integer", "kilocalories", "Source-reported active energy for one local date."),
    ("daily_minimum_heart_rate", "integer", "beats_per_minute", "Source-reported minimum heart rate for one local date."),
    ("daily_maximum_heart_rate", "integer", "beats_per_minute", "Source-reported maximum heart rate for one local date."),
    ("daily_average_stress", "decimal", "score", "Source-reported average stress for one local date."),
    ("daily_moderate_intensity_minutes", "integer", "minutes", "Source-reported moderate intensity minutes."),
    ("daily_vigorous_intensity_minutes", "integer", "minutes", "Source-reported vigorous intensity minutes."),
    ("daily_floors_ascended", "decimal", "floors", "Source-reported floors ascended for one local date."),
    ("daily_sleep_duration", "integer", "seconds", "Source-reported sleep duration for one local date."),
    ("daily_sleep_score", "decimal", "score", "Source-reported sleep score for one local date."),
    ("nightly_hrv_average", "decimal", "milliseconds", "Source-reported nightly average HRV."),
    ("daily_training_readiness", "decimal", "score", "Source-reported training readiness for one local date."),
    ("daily_training_readiness_level", "text", "status", "Source-reported training readiness level."),
    ("daily_training_status", "boolean", "status", "Whether source training status data was present."),
    ("daily_vo2max_running", "decimal", "millilitres_per_kilogram_minute", "Source-reported running VO2max."),
    ("daily_body_battery_charged", "integer", "points", "Source-reported body battery charged."),
    ("daily_body_battery_drained", "integer", "points", "Source-reported body battery drained."),
)


def upgrade() -> None:
    # Definition rows are immutable at runtime. The migration owner temporarily
    # disables the guard while extending the controlled catalogue.
    op.execute(
        "ALTER TABLE observation_definitions "
        "DISABLE TRIGGER observation_definitions_controlled"
    )
    definitions = sa.table(
        "observation_definitions",
        sa.column("key", sa.Text()),
        sa.column("value_type", sa.Text()),
        sa.column("unit", sa.Text()),
        sa.column("window_kind", sa.Text()),
        sa.column("method", sa.Text()),
        sa.column("missing_allowed", sa.Boolean()),
        sa.column("description", sa.Text()),
    )
    op.bulk_insert(
        definitions,
        [
            {
                "key": key,
                "value_type": value_type,
                "unit": unit,
                "window_kind": "calendar_day",
                "method": "source_reported",
                "missing_allowed": True,
                "description": description,
            }
            for key, value_type, unit, description in _DEFINITIONS
        ],
    )
    op.execute(
        "ALTER TABLE observation_definitions "
        "ENABLE TRIGGER observation_definitions_controlled"
    )


def downgrade() -> None:
    keys = ", ".join(f"'{key}'" for key, *_ in _DEFINITIONS)
    # Exclude concurrent ingest while derived observations are reconciled. The
    # immutable collected_record_captures rows remain the source of authority;
    # replaying the same capture after re-upgrade deterministically rebuilds
    # these disposable projections.
    op.execute("LOCK TABLE profiles IN ACCESS EXCLUSIVE MODE")
    op.execute(
        f"DELETE FROM controlled_observations WHERE definition_key IN ({keys})"
    )
    op.execute(
        "ALTER TABLE observation_definitions "
        "DISABLE TRIGGER observation_definitions_controlled"
    )
    op.execute(f"DELETE FROM observation_definitions WHERE key IN ({keys})")
    op.execute(
        "ALTER TABLE observation_definitions "
        "ENABLE TRIGGER observation_definitions_controlled"
    )
