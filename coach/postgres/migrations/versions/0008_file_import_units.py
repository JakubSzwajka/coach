"""Add owner-only legacy reconstruction and immutable import evidence.

Revision ID: 0008_file_import_units
Revises: 0007_collection_job_admission
"""

from __future__ import annotations

from alembic import op

revision = "0008_file_import_units"
down_revision = "0007_collection_job_admission"
branch_labels = None
depends_on = None

_SLEEP_DEFINITIONS = (
    ("daily_deep_sleep_duration", "Deep sleep duration for one local date."),
    ("daily_light_sleep_duration", "Light sleep duration for one local date."),
    ("daily_rem_sleep_duration", "REM sleep duration for one local date."),
    ("daily_awake_sleep_duration", "Awake sleep duration for one local date."),
)


def upgrade() -> None:
    # Canonical-only legacy daily files may be the sole surviving authority for
    # these values. The catalogue remains migration-controlled at runtime.
    op.execute(
        "ALTER TABLE observation_definitions "
        "DISABLE TRIGGER observation_definitions_controlled"
    )
    for key, description in _SLEEP_DEFINITIONS:
        escaped = description.replace("'", "''")
        op.execute(
            "INSERT INTO observation_definitions "
            "(key, value_type, unit, window_kind, method, missing_allowed, description) "
            f"VALUES ('{key}', 'integer', 'seconds', 'calendar_day', "
            f"'source_reported', true, '{escaped}')"
        )
    op.execute(
        "ALTER TABLE observation_definitions "
        "ENABLE TRIGGER observation_definitions_controlled"
    )

    # This mode bypasses only the command-time current-goal policy. It is both
    # transaction-local and table-owner-only; composite FKs and every revision,
    # history, snapshot, fulfilment, and isolation trigger remain enabled.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION garmin_coach_assert_head_goal_reference_current()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE head_revision bigint;
        DECLARE current_goal_revision bigint;
        DECLARE revision_kind text;
        DECLARE revision_status text;
        DECLARE relation_owner text;
        BEGIN
            SELECT pg_get_userbyid(c.relowner) INTO relation_owner
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = 'plan_revision_goal_events';
            IF current_setting(
                   'garmin_coach.maintenance_reconstruction', true
               ) = 'on'
               AND current_user = relation_owner THEN
                RETURN NULL;
            END IF;
            SELECT current_revision INTO head_revision
            FROM public.training_plans
            WHERE profile_id = NEW.profile_id AND id = NEW.plan_id;
            SELECT kind, status INTO revision_kind, revision_status
            FROM public.plan_revisions
            WHERE profile_id = NEW.profile_id AND plan_id = NEW.plan_id
              AND revision = NEW.revision;
            IF head_revision = NEW.revision
               AND (revision_kind IN ('create', 'adjustment')
                    OR (revision_kind = 'lifecycle'
                        AND revision_status = 'active')) THEN
                SELECT revision INTO current_goal_revision
                FROM public.goal_events
                WHERE profile_id = NEW.profile_id AND id = NEW.goal_event_id;
                IF current_goal_revision IS DISTINCT FROM NEW.goal_event_revision THEN
                    RAISE EXCEPTION
                        'plan head must reference the current goal event revision'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )

    # A row commits in the same owner transaction as one complete Profile.
    # It contains only a digest and privacy-safe counts.
    op.execute(
        """
        CREATE TABLE file_import_units (
            profile_id uuid PRIMARY KEY REFERENCES profiles(id),
            importer_version integer NOT NULL,
            source_manifest_hash bytea NOT NULL,
            inventory_counts jsonb NOT NULL,
            imported_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_file_import_version CHECK (importer_version > 0),
            CONSTRAINT ck_file_import_manifest_hash
                CHECK (octet_length(source_manifest_hash) = 32),
            CONSTRAINT ck_file_import_counts
                CHECK (jsonb_typeof(inventory_counts) = 'object')
        );

        CREATE FUNCTION garmin_coach_guard_file_import_unit_write()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE relation_owner text;
        BEGIN
            SELECT pg_get_userbyid(c.relowner) INTO relation_owner
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = TG_TABLE_NAME;
            IF TG_OP = 'INSERT' AND current_user = relation_owner THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'file import evidence is owner-only and immutable'
                USING ERRCODE = '42501';
        END;
        $$;
        CREATE TRIGGER file_import_units_write_guard
            BEFORE INSERT OR UPDATE OR DELETE ON file_import_units
            FOR EACH ROW EXECUTE FUNCTION garmin_coach_guard_file_import_unit_write();

        CREATE FUNCTION garmin_coach_reject_file_import_unit_truncate()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.file_import_units) THEN
                RAISE EXCEPTION 'file import evidence cannot be truncated'
                    USING ERRCODE = '42501';
            END IF;
            RETURN NULL;
        END;
        $$;
        CREATE TRIGGER file_import_units_truncate_guard
            BEFORE TRUNCATE ON file_import_units
            FOR EACH STATEMENT
            EXECUTE FUNCTION garmin_coach_reject_file_import_unit_truncate();

        DO $block$
        DECLARE granted_role text;
        BEGIN
            FOR granted_role IN
                SELECT DISTINCT grantee
                FROM information_schema.role_table_grants
                WHERE table_schema = 'public'
                  AND table_name = 'file_import_units'
                  AND privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
                  AND grantee <> current_user
            LOOP
                EXECUTE format(
                    'REVOKE INSERT, UPDATE, DELETE, TRUNCATE '
                    'ON public.file_import_units FROM %I',
                    granted_role
                );
            END LOOP;
        END;
        $block$;
        """
    )


def downgrade() -> None:
    op.execute("LOCK TABLE profiles IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $block$
        BEGIN
            IF EXISTS (SELECT 1 FROM file_import_units) THEN
                RAISE EXCEPTION '0008 downgrade would discard import evidence'
                    USING ERRCODE = '55000';
            END IF;
        END;
        $block$;
        """
    )
    op.execute("DROP TRIGGER file_import_units_truncate_guard ON file_import_units")
    op.execute("DROP FUNCTION garmin_coach_reject_file_import_unit_truncate()")
    op.execute("DROP TRIGGER file_import_units_write_guard ON file_import_units")
    op.execute("DROP FUNCTION garmin_coach_guard_file_import_unit_write()")
    op.execute("DROP TABLE file_import_units")

    # Restore the ordinary runtime-only policy function from revision 0005.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION garmin_coach_assert_head_goal_reference_current()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE head_revision bigint;
        DECLARE current_goal_revision bigint;
        DECLARE revision_kind text;
        DECLARE revision_status text;
        BEGIN
            SELECT current_revision INTO head_revision
            FROM public.training_plans
            WHERE profile_id = NEW.profile_id AND id = NEW.plan_id;
            SELECT kind, status INTO revision_kind, revision_status
            FROM public.plan_revisions
            WHERE profile_id = NEW.profile_id AND plan_id = NEW.plan_id
              AND revision = NEW.revision;
            IF head_revision = NEW.revision
               AND (revision_kind IN ('create', 'adjustment')
                    OR (revision_kind = 'lifecycle'
                        AND revision_status = 'active')) THEN
                SELECT revision INTO current_goal_revision
                FROM public.goal_events
                WHERE profile_id = NEW.profile_id AND id = NEW.goal_event_id;
                IF current_goal_revision IS DISTINCT FROM NEW.goal_event_revision THEN
                    RAISE EXCEPTION
                        'plan head must reference the current goal event revision'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$
        """
    )

    keys = ", ".join(f"'{key}'" for key, _ in _SLEEP_DEFINITIONS)
    op.execute(f"DELETE FROM controlled_observations WHERE definition_key IN ({keys})")
    op.execute(
        "ALTER TABLE observation_definitions "
        "DISABLE TRIGGER observation_definitions_controlled"
    )
    op.execute(f"DELETE FROM observation_definitions WHERE key IN ({keys})")
    op.execute(
        "ALTER TABLE observation_definitions "
        "ENABLE TRIGGER observation_definitions_controlled"
    )
