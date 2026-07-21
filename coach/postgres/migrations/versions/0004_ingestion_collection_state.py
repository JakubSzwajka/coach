"""Add canonical ingestion and durable collection state.

Revision ID: 0004_ingestion_collection_state
Revises: 0003_profile_revisions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_ingestion_collection_state"
down_revision = "0003_profile_revisions"
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)
_JSON = postgresql.JSONB(astext_type=sa.Text())
_TIME = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column(
        "source_connections",
        sa.Column("state_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "source_connections",
        sa.Column("last_authenticated_at", _TIME, nullable=True),
    )
    op.add_column(
        "source_connections",
        sa.Column("reconnect_safe_code", sa.Text(), nullable=True),
    )
    op.add_column(
        "source_connections",
        sa.Column("reconnect_at", _TIME, nullable=True),
    )
    op.execute(
        "UPDATE source_connections "
        "SET reconnect_safe_code = 'authentication_required', "
        "reconnect_at = updated_at "
        "WHERE state = 'needs_reconnect'"
    )
    op.create_check_constraint(
        "ck_source_state_revision_nonnegative",
        "source_connections",
        "state_revision >= 0",
    )
    op.create_check_constraint(
        "ck_source_reconnect_metadata",
        "source_connections",
        "(state = 'needs_reconnect' AND reconnect_safe_code IN "
        "('authentication_required', 'mfa_required', 'credentials_rejected') "
        "AND reconnect_at IS NOT NULL) OR "
        "(state <> 'needs_reconnect' AND reconnect_safe_code IS NULL "
        "AND reconnect_at IS NULL)",
    )
    op.create_unique_constraint(
        "uq_capture_record_identity",
        "collected_record_captures",
        ["profile_id", "collected_record_id", "id"],
    )

    op.create_table(
        "ingest_batches",
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        sa.Column("source_connection_id", _UUID, nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("semantics_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", _TIME, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("btrim(idempotency_key) <> ''", name="ck_ingest_batch_key"),
        sa.CheckConstraint(
            "octet_length(semantics_hash) = 32", name="ck_ingest_batch_hash"
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"]),
        sa.ForeignKeyConstraint(
            ["profile_id", "source_connection_id"],
            ["source_connections.profile_id", "source_connections.id"],
        ),
        sa.PrimaryKeyConstraint("profile_id", "id"),
        sa.UniqueConstraint(
            "profile_id",
            "source_connection_id",
            "idempotency_key",
            name="uq_ingest_batch_idempotency",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION garmin_coach_reject_ingest_batch_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'ingest batch identity is immutable'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER ingest_batches_immutable
        BEFORE UPDATE OR DELETE ON ingest_batches
        FOR EACH ROW EXECUTE FUNCTION garmin_coach_reject_ingest_batch_mutation()
        """
    )
    op.execute(
        """
        DO $block$
        DECLARE granted_role text;
        BEGIN
            FOR granted_role IN
                SELECT DISTINCT grantee
                FROM information_schema.role_table_grants
                WHERE table_schema = 'public' AND table_name = 'ingest_batches'
                  AND privilege_type IN ('UPDATE', 'DELETE')
                  AND grantee <> current_user
            LOOP
                EXECUTE format(
                    'REVOKE UPDATE, DELETE ON public.ingest_batches FROM %I',
                    granted_role
                );
            END LOOP;
        END;
        $block$
        """
    )

    # This is the shared completed-session root. Collected projections and future
    # App Records use one table while retaining explicit, immutable ownership.
    op.create_table(
        "training_sessions",
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        sa.Column("ownership", sa.Text(), nullable=False),
        sa.Column("collected_record_id", _UUID, nullable=True),
        sa.Column("current_capture_id", _UUID, nullable=True),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("local_start", sa.DateTime(timezone=False), nullable=True),
        sa.Column("timing_precision", sa.Text(), nullable=False),
        sa.Column("time_zone", sa.Text(), nullable=True),
        sa.Column("utc_offset", sa.Text(), nullable=True),
        sa.Column("sport", sa.Text(), nullable=False),
        sa.Column("session_type", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("session_rpe", sa.SmallInteger(), nullable=True),
        sa.Column("duration_value", sa.Numeric(), nullable=True),
        sa.Column("duration_unit", sa.Text(), nullable=True),
        sa.Column("duration_basis", sa.Text(), nullable=True),
        sa.Column("distance_value", sa.Numeric(), nullable=True),
        sa.Column("distance_unit", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", _TIME, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TIME, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "ownership IN ('collected', 'app')", name="ck_training_session_ownership"
        ),
        sa.CheckConstraint(
            "(ownership = 'collected' AND collected_record_id IS NOT NULL "
            "AND current_capture_id IS NOT NULL) OR "
            "(ownership = 'app' AND collected_record_id IS NULL "
            "AND current_capture_id IS NULL)",
            name="ck_training_session_owner_reference",
        ),
        sa.CheckConstraint(
            "timing_precision IN ('date_only', 'local_datetime')",
            name="ck_training_session_timing_precision",
        ),
        sa.CheckConstraint("btrim(sport) <> ''", name="ck_training_session_sport"),
        sa.CheckConstraint(
            "session_rpe IS NULL OR session_rpe BETWEEN 1 AND 10",
            name="ck_training_session_rpe",
        ),
        sa.CheckConstraint(
            "(timing_precision = 'date_only' AND local_start IS NULL) OR "
            "(timing_precision = 'local_datetime' AND local_start IS NOT NULL "
            "AND local_start::date = local_date)",
            name="ck_training_session_local_timing",
        ),
        sa.CheckConstraint(
            "num_nonnulls(duration_value, duration_unit, duration_basis) IN (0, 3) "
            "AND (duration_value IS NULL OR (duration_value > 0 "
            "AND btrim(duration_unit) <> '' "
            "AND ((ownership = 'collected' AND duration_basis IN "
            "('elapsed', 'active', 'source_reported')) OR "
            "(ownership = 'app' AND duration_basis IN ('elapsed', 'active')))))",
            name="ck_training_session_duration",
        ),
        sa.CheckConstraint(
            "num_nonnulls(distance_value, distance_unit) IN (0, 2) "
            "AND (distance_value IS NULL OR (distance_value > 0 "
            "AND btrim(distance_unit) <> ''))",
            name="ck_training_session_distance",
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"]),
        sa.ForeignKeyConstraint(
            ["profile_id", "collected_record_id"],
            ["collected_records.profile_id", "collected_records.id"],
        ),
        sa.ForeignKeyConstraint(
            ["profile_id", "collected_record_id", "current_capture_id"],
            [
                "collected_record_captures.profile_id",
                "collected_record_captures.collected_record_id",
                "collected_record_captures.id",
            ],
        ),
        sa.PrimaryKeyConstraint("profile_id", "id"),
        sa.UniqueConstraint(
            "profile_id", "id", "ownership", name="uq_training_session_owner"
        ),
        sa.UniqueConstraint(
            "profile_id", "id", "ownership", "collected_record_id",
            "current_capture_id", name="uq_training_session_load_provenance"
        ),
    )
    op.create_index(
        "uq_collected_training_session_record",
        "training_sessions",
        ["profile_id", "collected_record_id"],
        unique=True,
        postgresql_where=sa.text("collected_record_id IS NOT NULL"),
    )

    op.create_table(
        "session_loads",
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        sa.Column("training_session_id", _UUID, nullable=False),
        sa.Column("ownership", sa.Text(), nullable=False),
        sa.Column("collected_record_id", _UUID, nullable=True),
        sa.Column("source_capture_id", _UUID, nullable=True),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_at", _TIME, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("ownership IN ('collected', 'app')", name="ck_session_load_owner"),
        sa.CheckConstraint("btrim(method) <> ''", name="ck_session_load_method"),
        sa.CheckConstraint("btrim(unit) <> ''", name="ck_session_load_unit"),
        sa.CheckConstraint("value > 0", name="ck_session_load_value"),
        sa.CheckConstraint("btrim(source) <> ''", name="ck_session_load_source"),
        sa.CheckConstraint(
            "(ownership = 'collected' AND collected_record_id IS NOT NULL "
            "AND source_capture_id IS NOT NULL) OR "
            "(ownership = 'app' AND collected_record_id IS NULL "
            "AND source_capture_id IS NULL)",
            name="ck_session_load_owner_reference",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id", "training_session_id"],
            ["training_sessions.profile_id", "training_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["profile_id", "training_session_id", "ownership"],
            [
                "training_sessions.profile_id",
                "training_sessions.id",
                "training_sessions.ownership",
            ],
            name="fk_session_load_session_owner",
        ),
        sa.ForeignKeyConstraint(
            ["profile_id", "collected_record_id", "source_capture_id"],
            [
                "collected_record_captures.profile_id",
                "collected_record_captures.collected_record_id",
                "collected_record_captures.id",
            ],
        ),
        sa.ForeignKeyConstraint(
            [
                "profile_id", "training_session_id", "ownership",
                "collected_record_id", "source_capture_id",
            ],
            [
                "training_sessions.profile_id", "training_sessions.id",
                "training_sessions.ownership",
                "training_sessions.collected_record_id",
                "training_sessions.current_capture_id",
            ],
            name="fk_session_load_current_session_capture",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("profile_id", "id"),
        sa.UniqueConstraint(
            "profile_id",
            "training_session_id",
            "method",
            "unit",
            "source",
            name="uq_session_load_method_unit_source",
        ),
    )

    op.create_table(
        "observation_definitions",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value_type", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("window_kind", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("missing_allowed", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "value_type IN ('decimal', 'integer', 'text', 'boolean')",
            name="ck_observation_definition_type",
        ),
        sa.CheckConstraint(
            "window_kind IN ('instant', 'calendar_day', 'interval')",
            name="ck_observation_definition_window",
        ),
        sa.CheckConstraint("btrim(key) <> ''", name="ck_observation_definition_key"),
        sa.CheckConstraint("btrim(unit) <> ''", name="ck_observation_definition_unit"),
        sa.CheckConstraint("btrim(method) <> ''", name="ck_observation_definition_method"),
        sa.PrimaryKeyConstraint("key"),
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
                "key": "daily_resting_heart_rate",
                "value_type": "integer",
                "unit": "beats_per_minute",
                "window_kind": "calendar_day",
                "method": "source_reported",
                "missing_allowed": True,
                "description": "Source-reported resting heart rate for one local date.",
            },
            {
                "key": "daily_hrv_status",
                "value_type": "text",
                "unit": "status",
                "window_kind": "calendar_day",
                "method": "source_reported",
                "missing_allowed": True,
                "description": "Source-reported HRV status for one local date.",
            },
            {
                "key": "recovery_score",
                "value_type": "decimal",
                "unit": "score",
                "window_kind": "instant",
                "method": "source_reported",
                "missing_allowed": True,
                "description": "A source-reported recovery score at one instant.",
            },
        ],
    )

    op.create_table(
        "observation_projection_heads",
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("collected_record_id", _UUID, nullable=False),
        sa.Column("current_capture_id", _UUID, nullable=False),
        sa.Column("updated_at", _TIME, nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"]),
        sa.ForeignKeyConstraint(
            ["profile_id", "collected_record_id"],
            ["collected_records.profile_id", "collected_records.id"],
        ),
        sa.ForeignKeyConstraint(
            ["profile_id", "collected_record_id", "current_capture_id"],
            [
                "collected_record_captures.profile_id",
                "collected_record_captures.collected_record_id",
                "collected_record_captures.id",
            ],
        ),
        sa.PrimaryKeyConstraint("profile_id", "collected_record_id"),
        sa.UniqueConstraint(
            "profile_id",
            "collected_record_id",
            "current_capture_id",
            name="uq_observation_projection_head_capture",
        ),
    )

    op.create_table(
        "controlled_observations",
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        sa.Column("collected_record_id", _UUID, nullable=False),
        sa.Column("current_capture_id", _UUID, nullable=False),
        sa.Column("definition_key", sa.Text(), nullable=False),
        sa.Column("value_type", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("window_kind", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=True),
        sa.Column("observed_at", _TIME, nullable=True),
        sa.Column("window_start", _TIME, nullable=True),
        sa.Column("window_end", _TIME, nullable=True),
        sa.Column("decimal_value", sa.Numeric(), nullable=True),
        sa.Column("integer_value", sa.BigInteger(), nullable=True),
        sa.Column("text_value", sa.Text(), nullable=True),
        sa.Column("boolean_value", sa.Boolean(), nullable=True),
        sa.Column("provenance", _JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", _TIME, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('observed', 'missing')", name="ck_observation_status"),
        sa.CheckConstraint(
            "(window_kind = 'calendar_day' AND local_date IS NOT NULL "
            "AND observed_at IS NULL AND window_start IS NULL AND window_end IS NULL) OR "
            "(window_kind = 'instant' AND local_date IS NULL "
            "AND observed_at IS NOT NULL AND window_start IS NULL AND window_end IS NULL) OR "
            "(window_kind = 'interval' AND local_date IS NULL AND observed_at IS NULL "
            "AND window_start IS NOT NULL AND window_end IS NOT NULL "
            "AND window_start < window_end)",
            name="ck_observation_window_shape",
        ),
        sa.CheckConstraint(
            "(status = 'missing' AND decimal_value IS NULL AND integer_value IS NULL "
            "AND text_value IS NULL AND boolean_value IS NULL) OR "
            "(status = 'observed' AND num_nonnulls(decimal_value, integer_value, "
            "text_value, boolean_value) = 1)",
            name="ck_observation_value_presence",
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"]),
        sa.ForeignKeyConstraint(["definition_key"], ["observation_definitions.key"]),
        sa.ForeignKeyConstraint(
            ["profile_id", "collected_record_id", "current_capture_id"],
            [
                "collected_record_captures.profile_id",
                "collected_record_captures.collected_record_id",
                "collected_record_captures.id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["profile_id", "collected_record_id", "current_capture_id"],
            [
                "observation_projection_heads.profile_id",
                "observation_projection_heads.collected_record_id",
                "observation_projection_heads.current_capture_id",
            ],
            name="fk_observation_current_projection_head",
        ),
        sa.PrimaryKeyConstraint("profile_id", "id"),
        sa.UniqueConstraint(
            "profile_id", "collected_record_id", "definition_key",
            name="uq_controlled_observation_current",
        ),
    )
    op.create_index(
        "ix_controlled_observation_record",
        "controlled_observations",
        ["profile_id", "collected_record_id"],
    )

    op.execute(
        """
        CREATE FUNCTION garmin_coach_guard_session_identity()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.ownership = 'collected' THEN
                    RAISE EXCEPTION 'collected training sessions are read-only'
                        USING ERRCODE = '55000';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.profile_id IS DISTINCT FROM NEW.profile_id
               OR OLD.id IS DISTINCT FROM NEW.id
               OR OLD.ownership IS DISTINCT FROM NEW.ownership
               OR OLD.collected_record_id IS DISTINCT FROM NEW.collected_record_id THEN
                RAISE EXCEPTION 'training session ownership is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER training_session_identity_immutable
        BEFORE UPDATE OR DELETE ON training_sessions
        FOR EACH ROW EXECUTE FUNCTION garmin_coach_guard_session_identity()
        """
    )
    op.execute(
        """
        CREATE FUNCTION garmin_coach_reject_definition_mutation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'observation definitions are migration-controlled'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER observation_definitions_controlled
        BEFORE INSERT OR UPDATE OR DELETE ON observation_definitions
        FOR EACH ROW EXECUTE FUNCTION garmin_coach_reject_definition_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION garmin_coach_validate_observation()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE definition public.observation_definitions%ROWTYPE;
        BEGIN
            SELECT * INTO definition
            FROM public.observation_definitions
            WHERE key = NEW.definition_key;
            IF NOT FOUND
               OR NEW.value_type <> definition.value_type
               OR NEW.unit <> definition.unit
               OR NEW.window_kind <> definition.window_kind
               OR NEW.method <> definition.method
               OR (NEW.status = 'missing' AND NOT definition.missing_allowed)
               OR (NEW.status = 'observed' AND
                   ((NEW.value_type = 'decimal' AND NEW.decimal_value IS NULL) OR
                    (NEW.value_type = 'integer' AND NEW.integer_value IS NULL) OR
                    (NEW.value_type = 'text' AND NEW.text_value IS NULL) OR
                    (NEW.value_type = 'boolean' AND NEW.boolean_value IS NULL))) THEN
                RAISE EXCEPTION 'controlled observation does not match its definition'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER controlled_observation_valid
        BEFORE INSERT OR UPDATE ON controlled_observations
        FOR EACH ROW EXECUTE FUNCTION garmin_coach_validate_observation()
        """
    )

    op.execute(
        """
        CREATE FUNCTION garmin_coach_safe_diagnostics(value jsonb)
        RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
        DECLARE item text;
        BEGIN
            IF jsonb_typeof(value) <> 'object' THEN RETURN false; END IF;
            FOR item IN SELECT jsonb_object_keys(value) LOOP
                IF item NOT IN ('attempt', 'cached_tokens', 'captures', 'sessions', 'observations')
                THEN RETURN false; END IF;
            END LOOP;
            IF (value ? 'attempt' AND jsonb_typeof(value -> 'attempt') <> 'number')
               OR (value ? 'cached_tokens' AND jsonb_typeof(value -> 'cached_tokens') <> 'boolean')
               OR (value ? 'captures' AND jsonb_typeof(value -> 'captures') <> 'number')
               OR (value ? 'sessions' AND jsonb_typeof(value -> 'sessions') <> 'number')
               OR (value ? 'observations' AND jsonb_typeof(value -> 'observations') <> 'number')
            THEN RETURN false; END IF;
            RETURN true;
        END;
        $$
        """
    )

    op.create_table(
        "collection_jobs",
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        sa.Column("source_connection_id", _UUID, nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="requested"),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("safe_code", sa.Text(), nullable=True),
        sa.Column("diagnostics", _JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", _TIME, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", _TIME, nullable=True),
        sa.Column("finished_at", _TIME, nullable=True),
        sa.CheckConstraint("btrim(request_key) <> ''", name="ck_collection_job_request_key"),
        sa.CheckConstraint("kind IN ('initial_sync', 'incremental')", name="ck_collection_job_kind"),
        sa.CheckConstraint(
            "state IN ('requested', 'running', 'succeeded', 'failed')",
            name="ck_collection_job_state",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_collection_job_revision"),
        sa.CheckConstraint(
            "(state = 'requested' AND revision = 0 AND started_at IS NULL "
            "AND finished_at IS NULL AND safe_code IS NULL) OR "
            "(state = 'running' AND revision = 1 AND started_at IS NOT NULL "
            "AND started_at >= created_at AND finished_at IS NULL "
            "AND safe_code IS NULL) OR "
            "(state = 'succeeded' AND revision = 2 AND started_at IS NOT NULL "
            "AND started_at >= created_at AND finished_at IS NOT NULL "
            "AND finished_at >= started_at AND safe_code IS NULL) OR "
            "(state = 'failed' AND revision = 2 AND started_at IS NOT NULL "
            "AND started_at >= created_at AND finished_at IS NOT NULL "
            "AND finished_at >= started_at "
            "AND safe_code IS NOT NULL)",
            name="ck_collection_job_lifecycle_shape",
        ),
        sa.CheckConstraint(
            "safe_code IS NULL OR safe_code IN ('authentication_required', 'mfa_required', "
            "'credentials_rejected', 'rate_limited', 'provider_unavailable', "
            "'external_failure', 'invalid_response', 'worker_lost')",
            name="ck_collection_job_safe_code",
        ),
        sa.CheckConstraint(
            "garmin_coach_safe_diagnostics(diagnostics)",
            name="ck_collection_job_safe_diagnostics",
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"]),
        sa.ForeignKeyConstraint(
            ["profile_id", "source_connection_id"],
            ["source_connections.profile_id", "source_connections.id"],
        ),
        sa.PrimaryKeyConstraint("profile_id", "id"),
        sa.UniqueConstraint(
            "profile_id", "source_connection_id", "request_key",
            name="uq_collection_job_request",
        ),
    )
    op.create_table(
        "collection_runs",
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        sa.Column("job_id", _UUID, nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("safe_code", sa.Text(), nullable=True),
        sa.Column("diagnostics", _JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("started_at", _TIME, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", _TIME, nullable=True),
        sa.CheckConstraint("run_number > 0", name="ck_collection_run_number"),
        sa.CheckConstraint("state IN ('running', 'succeeded', 'failed')", name="ck_collection_run_state"),
        sa.CheckConstraint(
            "(state = 'running' AND finished_at IS NULL AND safe_code IS NULL) OR "
            "(state = 'succeeded' AND finished_at IS NOT NULL "
            "AND finished_at >= started_at AND safe_code IS NULL) OR "
            "(state = 'failed' AND finished_at IS NOT NULL "
            "AND finished_at >= started_at AND safe_code IS NOT NULL)",
            name="ck_collection_run_lifecycle_shape",
        ),
        sa.CheckConstraint(
            "safe_code IS NULL OR safe_code IN ('authentication_required', 'mfa_required', "
            "'credentials_rejected', 'rate_limited', 'provider_unavailable', "
            "'external_failure', 'invalid_response', 'worker_lost')",
            name="ck_collection_run_safe_code",
        ),
        sa.CheckConstraint("garmin_coach_safe_diagnostics(diagnostics)", name="ck_collection_run_safe_diagnostics"),
        sa.ForeignKeyConstraint(
            ["profile_id", "job_id"], ["collection_jobs.profile_id", "collection_jobs.id"]
        ),
        sa.PrimaryKeyConstraint("profile_id", "id"),
        sa.UniqueConstraint("profile_id", "job_id", "run_number", name="uq_collection_run_number"),
    )
    op.create_table(
        "collection_attempts",
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("id", _UUID, nullable=False),
        sa.Column("run_id", _UUID, nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("safe_code", sa.Text(), nullable=True),
        sa.Column("diagnostics", _JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("started_at", _TIME, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", _TIME, nullable=True),
        sa.CheckConstraint("attempt_number > 0", name="ck_collection_attempt_number"),
        sa.CheckConstraint("state IN ('running', 'succeeded', 'failed')", name="ck_collection_attempt_state"),
        sa.CheckConstraint(
            "(state = 'running' AND finished_at IS NULL AND safe_code IS NULL) OR "
            "(state = 'succeeded' AND finished_at IS NOT NULL "
            "AND finished_at >= started_at AND safe_code IS NULL) OR "
            "(state = 'failed' AND finished_at IS NOT NULL "
            "AND finished_at >= started_at AND safe_code IS NOT NULL)",
            name="ck_collection_attempt_lifecycle_shape",
        ),
        sa.CheckConstraint(
            "safe_code IS NULL OR safe_code IN ('authentication_required', 'mfa_required', "
            "'credentials_rejected', 'rate_limited', 'provider_unavailable', "
            "'external_failure', 'invalid_response', 'worker_lost')",
            name="ck_collection_attempt_safe_code",
        ),
        sa.CheckConstraint("garmin_coach_safe_diagnostics(diagnostics)", name="ck_collection_attempt_safe_diagnostics"),
        sa.ForeignKeyConstraint(
            ["profile_id", "run_id"], ["collection_runs.profile_id", "collection_runs.id"]
        ),
        sa.PrimaryKeyConstraint("profile_id", "id"),
        sa.UniqueConstraint("profile_id", "run_id", "attempt_number", name="uq_collection_attempt_number"),
    )
    op.create_table(
        "collection_checkpoints",
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("source_connection_id", _UUID, nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("cursor", _JSON, nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", _TIME, nullable=False, server_default=sa.func.now()),
        sa.Column("last_success_at", _TIME, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("domain IN ('initial_sync', 'incremental')", name="ck_collection_checkpoint_domain"),
        sa.CheckConstraint("revision >= 0", name="ck_collection_checkpoint_revision"),
        sa.CheckConstraint("jsonb_typeof(cursor) = 'object'", name="ck_collection_checkpoint_cursor"),
        sa.ForeignKeyConstraint(
            ["profile_id", "source_connection_id"],
            ["source_connections.profile_id", "source_connections.id"],
        ),
        sa.PrimaryKeyConstraint("profile_id", "source_connection_id", "domain"),
    )
    op.create_table(
        "collection_health",
        sa.Column("profile_id", _UUID, nullable=False),
        sa.Column("source_connection_id", _UUID, nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("collection_job_id", _UUID, nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("attempted_at", _TIME, nullable=True),
        sa.Column("finished_at", _TIME, nullable=True),
        sa.Column("last_success_at", _TIME, nullable=True),
        sa.Column("safe_code", sa.Text(), nullable=True),
        sa.Column("diagnostics", _JSON, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("domain IN ('initial_sync', 'incremental')", name="ck_collection_health_domain"),
        sa.CheckConstraint(
            "outcome IN ('not_attempted', 'running', 'successful', 'degraded', 'failed')",
            name="ck_collection_health_outcome",
        ),
        sa.CheckConstraint(
            "safe_code IS NULL OR safe_code IN ('authentication_required', 'mfa_required', "
            "'credentials_rejected', 'rate_limited', 'provider_unavailable', "
            "'external_failure', 'invalid_response', 'worker_lost')",
            name="ck_collection_health_safe_code",
        ),
        sa.CheckConstraint("garmin_coach_safe_diagnostics(diagnostics)", name="ck_collection_health_safe_diagnostics"),
        sa.ForeignKeyConstraint(
            ["profile_id", "source_connection_id"],
            ["source_connections.profile_id", "source_connections.id"],
        ),
        sa.ForeignKeyConstraint(
            ["profile_id", "collection_job_id"],
            ["collection_jobs.profile_id", "collection_jobs.id"],
        ),
        sa.PrimaryKeyConstraint("profile_id", "source_connection_id", "domain"),
    )

    op.execute(
        """
        CREATE FUNCTION garmin_coach_guard_job_transition()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'collection job evidence is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.profile_id IS DISTINCT FROM NEW.profile_id OR OLD.id IS DISTINCT FROM NEW.id
               OR OLD.source_connection_id IS DISTINCT FROM NEW.source_connection_id
               OR OLD.request_key IS DISTINCT FROM NEW.request_key OR OLD.kind IS DISTINCT FROM NEW.kind
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'collection job identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF OLD.state = NEW.state THEN
                RAISE EXCEPTION 'collection job same-state rewrites are forbidden'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.state = 'requested' AND NEW.state = 'running' THEN
                IF NEW.revision <> OLD.revision + 1 OR NEW.started_at IS NULL
                   OR NEW.finished_at IS NOT NULL OR NEW.safe_code IS NOT NULL THEN
                    RAISE EXCEPTION 'invalid requested to running evidence'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF OLD.state = 'running' AND NEW.state IN ('succeeded', 'failed') THEN
                IF NEW.revision <> OLD.revision + 1
                   OR NEW.started_at IS DISTINCT FROM OLD.started_at
                   OR NEW.finished_at IS NULL
                   OR (NEW.state = 'succeeded' AND NEW.safe_code IS NOT NULL)
                   OR (NEW.state = 'failed' AND NEW.safe_code IS NULL) THEN
                    RAISE EXCEPTION 'invalid terminal collection job evidence'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                RAISE EXCEPTION 'illegal collection job transition' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER collection_job_transition
        BEFORE UPDATE OR DELETE ON collection_jobs
        FOR EACH ROW EXECUTE FUNCTION garmin_coach_guard_job_transition()
        """
    )
    op.execute(
        """
        CREATE FUNCTION garmin_coach_guard_run_transition()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'collection run evidence is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.profile_id IS DISTINCT FROM NEW.profile_id OR OLD.id IS DISTINCT FROM NEW.id
               OR OLD.job_id IS DISTINCT FROM NEW.job_id OR OLD.run_number IS DISTINCT FROM NEW.run_number
               OR OLD.started_at IS DISTINCT FROM NEW.started_at THEN
                RAISE EXCEPTION 'collection run identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF OLD.state = NEW.state THEN
                RAISE EXCEPTION 'collection run same-state rewrites are forbidden'
                    USING ERRCODE = '55000';
            END IF;
            IF NOT (OLD.state = 'running' AND NEW.state IN ('succeeded', 'failed'))
               OR NEW.finished_at IS NULL
               OR (NEW.state = 'succeeded' AND NEW.safe_code IS NOT NULL)
               OR (NEW.state = 'failed' AND NEW.safe_code IS NULL) THEN
                RAISE EXCEPTION 'illegal collection run transition' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER collection_run_transition BEFORE UPDATE OR DELETE ON collection_runs "
        "FOR EACH ROW EXECUTE FUNCTION garmin_coach_guard_run_transition()"
    )
    op.execute(
        """
        CREATE FUNCTION garmin_coach_guard_attempt_transition()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'collection attempt evidence is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.profile_id IS DISTINCT FROM NEW.profile_id OR OLD.id IS DISTINCT FROM NEW.id
               OR OLD.run_id IS DISTINCT FROM NEW.run_id
               OR OLD.attempt_number IS DISTINCT FROM NEW.attempt_number
               OR OLD.started_at IS DISTINCT FROM NEW.started_at THEN
                RAISE EXCEPTION 'collection attempt identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF OLD.state = NEW.state THEN
                RAISE EXCEPTION 'collection attempt same-state rewrites are forbidden'
                    USING ERRCODE = '55000';
            END IF;
            IF NOT (OLD.state = 'running' AND NEW.state IN ('succeeded', 'failed'))
               OR NEW.finished_at IS NULL
               OR (NEW.state = 'succeeded' AND NEW.safe_code IS NOT NULL)
               OR (NEW.state = 'failed' AND NEW.safe_code IS NULL) THEN
                RAISE EXCEPTION 'illegal collection attempt transition' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER collection_attempt_transition BEFORE UPDATE OR DELETE ON collection_attempts "
        "FOR EACH ROW EXECUTE FUNCTION garmin_coach_guard_attempt_transition()"
    )
    op.execute(
        """
        DO $block$
        DECLARE granted_role text;
        DECLARE lifecycle_table text;
        BEGIN
            FOREACH lifecycle_table IN ARRAY ARRAY[
                'collection_jobs', 'collection_runs', 'collection_attempts'
            ]
            LOOP
                FOR granted_role IN
                    SELECT DISTINCT grantee
                    FROM information_schema.role_table_grants
                    WHERE table_schema = 'public'
                      AND table_name = lifecycle_table
                      AND privilege_type = 'DELETE'
                      AND grantee <> current_user
                LOOP
                    EXECUTE format(
                        'REVOKE DELETE ON public.%I FROM %I',
                        lifecycle_table, granted_role
                    );
                END LOOP;
            END LOOP;
        END;
        $block$
        """
    )

    op.execute(
        """
        CREATE VIEW collection_status_safe AS
        SELECT sc.profile_id,
               sc.id AS source_connection_id,
               sc.provider,
               sc.state AS source_state,
               h.domain,
               h.outcome,
               h.attempted_at,
               h.finished_at,
               h.last_success_at,
               h.safe_code,
               h.diagnostics,
               cp.revision AS checkpoint_revision
        FROM source_connections AS sc
        LEFT JOIN collection_health AS h
          ON h.profile_id = sc.profile_id AND h.source_connection_id = sc.id
        LEFT JOIN collection_checkpoints AS cp
          ON cp.profile_id = h.profile_id
         AND cp.source_connection_id = h.source_connection_id
         AND cp.domain = h.domain
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW collection_status_safe")
    op.execute("DROP TRIGGER collection_attempt_transition ON collection_attempts")
    op.execute("DROP FUNCTION garmin_coach_guard_attempt_transition()")
    op.execute("DROP TRIGGER collection_run_transition ON collection_runs")
    op.execute("DROP FUNCTION garmin_coach_guard_run_transition()")
    op.execute("DROP TRIGGER collection_job_transition ON collection_jobs")
    op.execute("DROP FUNCTION garmin_coach_guard_job_transition()")
    op.drop_table("collection_health")
    op.drop_table("collection_checkpoints")
    op.drop_table("collection_attempts")
    op.drop_table("collection_runs")
    op.drop_table("collection_jobs")
    op.execute("DROP FUNCTION garmin_coach_safe_diagnostics(jsonb)")
    op.execute("DROP TRIGGER controlled_observation_valid ON controlled_observations")
    op.execute("DROP FUNCTION garmin_coach_validate_observation()")
    op.execute("DROP TRIGGER observation_definitions_controlled ON observation_definitions")
    op.execute("DROP FUNCTION garmin_coach_reject_definition_mutation()")
    op.execute("DROP TRIGGER training_session_identity_immutable ON training_sessions")
    op.execute("DROP FUNCTION garmin_coach_guard_session_identity()")
    op.drop_table("controlled_observations")
    op.drop_table("observation_projection_heads")
    op.drop_table("observation_definitions")
    op.drop_table("session_loads")
    op.drop_index("uq_collected_training_session_record", table_name="training_sessions")
    op.drop_table("training_sessions")
    op.execute("DROP TRIGGER ingest_batches_immutable ON ingest_batches")
    op.execute("DROP FUNCTION garmin_coach_reject_ingest_batch_mutation()")
    op.drop_table("ingest_batches")
    op.drop_constraint(
        "uq_capture_record_identity", "collected_record_captures", type_="unique"
    )
    op.drop_constraint(
        "ck_source_reconnect_metadata", "source_connections", type_="check"
    )
    op.drop_constraint(
        "ck_source_state_revision_nonnegative", "source_connections", type_="check"
    )
    op.drop_column("source_connections", "reconnect_at")
    op.drop_column("source_connections", "reconnect_safe_code")
    op.drop_column("source_connections", "last_authenticated_at")
    op.drop_column("source_connections", "state_revision")
