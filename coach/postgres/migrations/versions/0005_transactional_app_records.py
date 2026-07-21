"""Add typed transactional App Records.

Revision ID: 0005_transactional_app_records
Revises: 0004_ingestion_collection_state
"""

from __future__ import annotations

from alembic import op

revision = "0005_transactional_app_records"
down_revision = "0004_ingestion_collection_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The shared Training Session root is also the public identity authority.
    # App ids encode this UUID as app:<uuidhex>; collected ids encode it as
    # session:<uuidhex>. Neither form contains a Profile or provider identity.
    op.execute(
        """
        ALTER TABLE training_sessions ADD COLUMN app_revision bigint;
        UPDATE training_sessions SET app_revision = 1 WHERE ownership = 'app';
        ALTER TABLE training_sessions ADD CONSTRAINT ck_training_session_app_revision
            CHECK ((ownership = 'app' AND app_revision >= 1) OR
                   (ownership = 'collected' AND app_revision IS NULL));

        DROP TRIGGER training_session_identity_immutable ON training_sessions;
        DROP FUNCTION garmin_coach_guard_session_identity();
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
               OR OLD.collected_record_id IS DISTINCT FROM NEW.collected_record_id
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'training session ownership is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.ownership = 'app' AND NEW.app_revision <> OLD.app_revision + 1 THEN
                RAISE EXCEPTION 'app training session revision must advance once'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER training_session_identity_immutable
        BEFORE UPDATE OR DELETE ON training_sessions
        FOR EACH ROW EXECUTE FUNCTION garmin_coach_guard_session_identity();
        """
    )

    op.execute(
        """
        CREATE TABLE session_annotations (
            profile_id uuid NOT NULL,
            id uuid NOT NULL,
            training_session_id uuid NOT NULL,
            target_ownership text NOT NULL DEFAULT 'collected',
            revision bigint NOT NULL DEFAULT 1,
            notes text,
            reliability text,
            duplicate_flag boolean,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (profile_id, id),
            UNIQUE (profile_id, training_session_id),
            CONSTRAINT ck_session_annotation_target
                CHECK (target_ownership = 'collected'),
            CONSTRAINT ck_session_annotation_revision CHECK (revision >= 1),
            CONSTRAINT ck_session_annotation_reliability
                CHECK (reliability IS NULL OR reliability IN ('reliable', 'unreliable')),
            CONSTRAINT ck_session_annotation_value
                CHECK (notes IS NOT NULL OR reliability IS NOT NULL OR duplicate_flag IS NOT NULL),
            CONSTRAINT ck_session_annotation_notes
                CHECK (notes IS NULL OR btrim(notes) <> ''),
            FOREIGN KEY (profile_id) REFERENCES profiles(id),
            FOREIGN KEY (profile_id, training_session_id, target_ownership)
                REFERENCES training_sessions(profile_id, id, ownership)
        );

        CREATE TABLE goal_events (
            profile_id uuid NOT NULL,
            id uuid NOT NULL,
            revision bigint NOT NULL DEFAULT 1,
            local_date date NOT NULL,
            local_start timestamp,
            timing_precision text NOT NULL,
            time_zone text,
            utc_offset text,
            sport text NOT NULL,
            name text NOT NULL,
            priority text NOT NULL,
            status text NOT NULL DEFAULT 'scheduled',
            distance_value numeric,
            distance_unit text,
            goal_target_value numeric,
            goal_target_unit text,
            goal_statement text,
            outcome_actual_value numeric,
            outcome_actual_unit text,
            outcome_statement text,
            notes text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (profile_id, id),
            UNIQUE (profile_id, id, revision),
            CONSTRAINT ck_goal_event_revision CHECK (revision >= 1),
            CONSTRAINT ck_goal_event_name CHECK (btrim(name) <> ''),
            CONSTRAINT ck_goal_event_sport CHECK (btrim(sport) <> ''),
            CONSTRAINT ck_goal_event_priority
                CHECK (priority IN ('primary', 'secondary', 'practice')),
            CONSTRAINT ck_goal_event_status
                CHECK (status IN ('scheduled', 'completed', 'cancelled')),
            CONSTRAINT ck_goal_event_timing_precision
                CHECK (timing_precision IN ('date_only', 'local_datetime')),
            CONSTRAINT ck_goal_event_local_timing CHECK (
                (timing_precision = 'date_only' AND local_start IS NULL) OR
                (timing_precision = 'local_datetime' AND local_start IS NOT NULL
                 AND local_start::date = local_date)
            ),
            CONSTRAINT ck_goal_event_distance CHECK (
                num_nonnulls(distance_value, distance_unit) IN (0, 2) AND
                (distance_value IS NULL OR (distance_value > 0 AND btrim(distance_unit) <> ''))
            ),
            CONSTRAINT ck_goal_event_goal CHECK (
                num_nonnulls(goal_target_value, goal_target_unit) IN (0, 2) AND
                (goal_target_value IS NULL OR
                 (goal_target_value > 0 AND goal_target_unit = 'seconds')) AND
                (goal_target_value IS NOT NULL OR goal_statement IS NOT NULL OR
                 (goal_target_unit IS NULL AND goal_statement IS NULL)) AND
                (goal_statement IS NULL OR btrim(goal_statement) <> '')
            ),
            CONSTRAINT ck_goal_event_outcome CHECK (
                num_nonnulls(outcome_actual_value, outcome_actual_unit) IN (0, 2) AND
                (outcome_actual_value IS NULL OR
                 (outcome_actual_value > 0 AND outcome_actual_unit = 'seconds')) AND
                (outcome_statement IS NULL OR btrim(outcome_statement) <> '')
            ),
            CONSTRAINT ck_goal_event_notes CHECK (notes IS NULL OR btrim(notes) <> ''),
            FOREIGN KEY (profile_id) REFERENCES profiles(id)
        );

        CREATE TABLE training_plans (
            profile_id uuid NOT NULL,
            id uuid NOT NULL,
            current_revision bigint NOT NULL DEFAULT 1,
            status text NOT NULL DEFAULT 'draft',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (profile_id, id),
            CONSTRAINT ck_training_plan_revision CHECK (current_revision >= 1),
            CONSTRAINT ck_training_plan_status CHECK (status IN ('draft', 'active', 'archived')),
            FOREIGN KEY (profile_id) REFERENCES profiles(id)
        );
        CREATE UNIQUE INDEX uq_training_plan_active_profile
            ON training_plans(profile_id) WHERE status = 'active';

        CREATE TABLE plan_revisions (
            profile_id uuid NOT NULL,
            plan_id uuid NOT NULL,
            revision bigint NOT NULL,
            kind text NOT NULL,
            recorded_at timestamptz NOT NULL,
            recorded_date date NOT NULL,
            reason text NOT NULL,
            effective_from date,
            name text NOT NULL,
            starts_on date NOT NULL,
            ends_on date NOT NULL,
            status text NOT NULL,
            PRIMARY KEY (profile_id, plan_id, revision),
            UNIQUE (profile_id, plan_id, revision, status),
            CONSTRAINT ck_plan_revision_number CHECK (revision >= 1),
            CONSTRAINT ck_plan_revision_kind CHECK (
                kind IN ('create', 'adjustment', 'lifecycle', 'fulfilment_correction')
            ),
            CONSTRAINT ck_plan_revision_reason CHECK (btrim(reason) <> ''),
            CONSTRAINT ck_plan_revision_name CHECK (btrim(name) <> ''),
            CONSTRAINT ck_plan_revision_range CHECK (starts_on <= ends_on),
            CONSTRAINT ck_plan_revision_status CHECK (status IN ('draft', 'active', 'archived')),
            CONSTRAINT ck_plan_revision_effective CHECK (
                (kind = 'adjustment' AND effective_from IS NOT NULL
                 AND effective_from >= recorded_date
                 AND effective_from BETWEEN starts_on AND ends_on) OR
                (kind <> 'adjustment' AND effective_from IS NULL)
            ),
            FOREIGN KEY (profile_id, plan_id)
                REFERENCES training_plans(profile_id, id) ON DELETE CASCADE
        );
        ALTER TABLE training_plans ADD CONSTRAINT fk_training_plan_current_revision
            FOREIGN KEY (profile_id, id, current_revision, status)
            REFERENCES plan_revisions(profile_id, plan_id, revision, status)
            DEFERRABLE INITIALLY DEFERRED;

        CREATE TABLE planned_sessions (
            profile_id uuid NOT NULL,
            plan_id uuid NOT NULL,
            id uuid NOT NULL,
            created_revision bigint NOT NULL,
            PRIMARY KEY (profile_id, plan_id, id),
            FOREIGN KEY (profile_id, plan_id)
                REFERENCES training_plans(profile_id, id) ON DELETE CASCADE,
            FOREIGN KEY (profile_id, plan_id, created_revision)
                REFERENCES plan_revisions(profile_id, plan_id, revision)
                DEFERRABLE INITIALLY DEFERRED
        );

        CREATE TABLE plan_revision_goal_events (
            profile_id uuid NOT NULL,
            plan_id uuid NOT NULL,
            revision bigint NOT NULL,
            position integer NOT NULL,
            goal_event_id uuid NOT NULL,
            goal_event_revision bigint NOT NULL,
            PRIMARY KEY (profile_id, plan_id, revision, position),
            UNIQUE (profile_id, plan_id, revision, goal_event_id),
            CONSTRAINT ck_plan_goal_position CHECK (position >= 0),
            CONSTRAINT ck_plan_goal_revision CHECK (goal_event_revision >= 1),
            FOREIGN KEY (profile_id, plan_id, revision)
                REFERENCES plan_revisions(profile_id, plan_id, revision) ON DELETE CASCADE,
            FOREIGN KEY (profile_id, goal_event_id)
                REFERENCES goal_events(profile_id, id)
        );

        CREATE TABLE plan_revision_constraints (
            profile_id uuid NOT NULL,
            plan_id uuid NOT NULL,
            revision bigint NOT NULL,
            position integer NOT NULL,
            statement text NOT NULL,
            PRIMARY KEY (profile_id, plan_id, revision, position),
            CONSTRAINT ck_plan_constraint_position CHECK (position >= 0),
            CONSTRAINT ck_plan_constraint_statement CHECK (btrim(statement) <> ''),
            FOREIGN KEY (profile_id, plan_id, revision)
                REFERENCES plan_revisions(profile_id, plan_id, revision) ON DELETE CASCADE
        );

        CREATE TABLE plan_revision_planned_sessions (
            profile_id uuid NOT NULL,
            plan_id uuid NOT NULL,
            revision bigint NOT NULL,
            planned_session_id uuid NOT NULL,
            position integer NOT NULL,
            scheduled_date date NOT NULL,
            sport text NOT NULL,
            session_type text,
            prescription text NOT NULL,
            target_duration_seconds numeric,
            target_distance_meters numeric,
            effort_guidance text,
            disposition text NOT NULL,
            fulfilment_note text,
            PRIMARY KEY (profile_id, plan_id, revision, planned_session_id),
            UNIQUE (profile_id, plan_id, revision, position),
            CONSTRAINT ck_plan_session_position CHECK (position >= 0),
            CONSTRAINT ck_plan_session_sport CHECK (btrim(sport) <> ''),
            CONSTRAINT ck_plan_session_prescription CHECK (btrim(prescription) <> ''),
            CONSTRAINT ck_plan_session_targets CHECK (
                (target_duration_seconds IS NULL OR target_duration_seconds > 0) AND
                (target_distance_meters IS NULL OR target_distance_meters > 0)
            ),
            CONSTRAINT ck_plan_session_disposition CHECK (
                disposition IN ('scheduled', 'fulfilled', 'skipped', 'cancelled')
            ),
            CONSTRAINT ck_plan_session_note CHECK (
                fulfilment_note IS NULL OR btrim(fulfilment_note) <> ''
            ),
            FOREIGN KEY (profile_id, plan_id, revision)
                REFERENCES plan_revisions(profile_id, plan_id, revision) ON DELETE CASCADE,
            FOREIGN KEY (profile_id, plan_id, planned_session_id)
                REFERENCES planned_sessions(profile_id, plan_id, id)
        );

        CREATE TABLE plan_revision_matches (
            profile_id uuid NOT NULL,
            plan_id uuid NOT NULL,
            revision bigint NOT NULL,
            planned_session_id uuid NOT NULL,
            position integer NOT NULL,
            training_session_id uuid NOT NULL,
            PRIMARY KEY (profile_id, plan_id, revision, planned_session_id, position),
            UNIQUE (profile_id, plan_id, revision, planned_session_id, training_session_id),
            CONSTRAINT ck_plan_match_position CHECK (position >= 0),
            FOREIGN KEY (profile_id, plan_id, revision, planned_session_id)
                REFERENCES plan_revision_planned_sessions(
                    profile_id, plan_id, revision, planned_session_id
                ) ON DELETE CASCADE,
            FOREIGN KEY (profile_id, training_session_id)
                REFERENCES training_sessions(profile_id, id)
        );

        CREATE TABLE planned_session_current_matches (
            profile_id uuid NOT NULL,
            plan_id uuid NOT NULL,
            planned_session_id uuid NOT NULL,
            training_session_id uuid NOT NULL,
            position integer NOT NULL,
            PRIMARY KEY (profile_id, plan_id, planned_session_id, position),
            UNIQUE (profile_id, training_session_id),
            UNIQUE (profile_id, plan_id, planned_session_id, training_session_id),
            CONSTRAINT ck_current_match_position CHECK (position >= 0),
            FOREIGN KEY (profile_id, plan_id, planned_session_id)
                REFERENCES planned_sessions(profile_id, plan_id, id) ON DELETE CASCADE,
            FOREIGN KEY (profile_id, training_session_id)
                REFERENCES training_sessions(profile_id, id)
        );
        """
    )

    op.execute(
        """
        CREATE FUNCTION garmin_coach_guard_app_head()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF OLD.profile_id IS DISTINCT FROM NEW.profile_id
               OR OLD.id IS DISTINCT FROM NEW.id
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'app record identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'app record revision must advance once' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER session_annotation_revision
            BEFORE UPDATE ON session_annotations FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_guard_app_head();
        CREATE TRIGGER goal_event_revision
            BEFORE UPDATE ON goal_events FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_guard_app_head();

        CREATE FUNCTION garmin_coach_guard_annotation_target()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF OLD.training_session_id IS DISTINCT FROM NEW.training_session_id
               OR OLD.target_ownership IS DISTINCT FROM NEW.target_ownership THEN
                RAISE EXCEPTION 'session annotation target is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER session_annotation_target_immutable
            BEFORE UPDATE ON session_annotations FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_guard_annotation_target();

        CREATE FUNCTION garmin_coach_guard_plan_head()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status <> 'draft'
                   OR EXISTS (
                       SELECT 1 FROM public.plan_revisions r
                       WHERE r.profile_id = OLD.profile_id AND r.plan_id = OLD.id
                         AND (r.status = 'active' OR r.kind = 'fulfilment_correction')
                   )
                   OR EXISTS (
                       SELECT 1 FROM public.plan_revision_planned_sessions s
                       WHERE s.profile_id = OLD.profile_id AND s.plan_id = OLD.id
                         AND (s.disposition IN ('fulfilled', 'skipped')
                              OR s.fulfilment_note IS NOT NULL)
                   )
                   OR EXISTS (
                       SELECT 1 FROM public.plan_revision_matches m
                       WHERE m.profile_id = OLD.profile_id AND m.plan_id = OLD.id
                   ) THEN
                    RAISE EXCEPTION 'training plan must be archived instead of deleted'
                        USING ERRCODE = '23514';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.profile_id IS DISTINCT FROM NEW.profile_id
               OR OLD.id IS DISTINCT FROM NEW.id
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'training plan identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF NEW.current_revision <> OLD.current_revision + 1 THEN
                RAISE EXCEPTION 'training plan revision must advance once'
                    USING ERRCODE = '23514';
            END IF;
            IF NOT (
                (OLD.status = 'draft' AND NEW.status IN ('draft', 'active', 'archived')) OR
                (OLD.status = 'active' AND NEW.status IN ('active', 'archived')) OR
                (OLD.status = 'archived' AND NEW.status = 'archived')
            ) THEN
                RAISE EXCEPTION 'illegal training plan status transition'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER training_plan_head_guard
            BEFORE UPDATE OR DELETE ON training_plans FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_guard_plan_head();

        CREATE FUNCTION garmin_coach_history_immutable()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND NOT EXISTS (
                SELECT 1 FROM public.training_plans p
                WHERE p.profile_id = OLD.profile_id AND p.id = OLD.plan_id
            ) THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION 'plan revision history is immutable' USING ERRCODE = '55000';
        END;
        $$;
        CREATE TRIGGER plan_revisions_immutable
            BEFORE UPDATE OR DELETE ON plan_revisions FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_history_immutable();
        CREATE TRIGGER planned_sessions_immutable
            BEFORE UPDATE OR DELETE ON planned_sessions FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_history_immutable();
        CREATE TRIGGER plan_revision_goals_immutable
            BEFORE UPDATE OR DELETE ON plan_revision_goal_events FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_history_immutable();
        CREATE TRIGGER plan_revision_constraints_immutable
            BEFORE UPDATE OR DELETE ON plan_revision_constraints FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_history_immutable();
        CREATE TRIGGER plan_revision_sessions_immutable
            BEFORE UPDATE OR DELETE ON plan_revision_planned_sessions FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_history_immutable();
        CREATE TRIGGER plan_revision_matches_immutable
            BEFORE UPDATE OR DELETE ON plan_revision_matches FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_history_immutable();

        CREATE FUNCTION garmin_coach_goal_reference_not_future()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE current_revision bigint;
        BEGIN
            SELECT revision INTO current_revision FROM public.goal_events
            WHERE profile_id = NEW.profile_id AND id = NEW.goal_event_id
            FOR KEY SHARE;
            IF current_revision IS NULL OR NEW.goal_event_revision > current_revision THEN
                RAISE EXCEPTION 'goal event revision is unavailable' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER plan_goal_revision_valid
            BEFORE INSERT ON plan_revision_goal_events FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_goal_reference_not_future();

        CREATE FUNCTION garmin_coach_assert_head_goal_reference_current()
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
                    RAISE EXCEPTION 'plan head must reference the current goal event revision'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER plan_head_goal_revision_current
            AFTER INSERT ON plan_revision_goal_events
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_assert_head_goal_reference_current();
        """
    )

    # Deferred relational assertions make a full snapshot and its replaceable
    # current-match projection one atomic authority.
    op.execute(
        """
        CREATE FUNCTION garmin_coach_assert_plan_revision()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE p_profile uuid := COALESCE(
            (to_jsonb(NEW) ->> 'profile_id')::uuid,
            (to_jsonb(OLD) ->> 'profile_id')::uuid
        );
        DECLARE p_plan uuid := COALESCE(
            (to_jsonb(NEW) ->> 'plan_id')::uuid,
            (to_jsonb(NEW) ->> 'id')::uuid,
            (to_jsonb(OLD) ->> 'plan_id')::uuid,
            (to_jsonb(OLD) ->> 'id')::uuid
        );
        DECLARE head bigint;
        DECLARE revision_count bigint;
        DECLARE minimum_revision bigint;
        DECLARE maximum_revision bigint;
        BEGIN
            SELECT current_revision INTO head FROM public.training_plans
            WHERE profile_id = p_profile AND id = p_plan;
            IF head IS NULL THEN RETURN NULL; END IF;
            SELECT count(*), min(revision), max(revision)
            INTO revision_count, minimum_revision, maximum_revision
            FROM public.plan_revisions
            WHERE profile_id = p_profile AND plan_id = p_plan;
            IF revision_count <> head
               OR minimum_revision IS DISTINCT FROM 1
               OR maximum_revision IS DISTINCT FROM head THEN
                RAISE EXCEPTION 'plan revisions must be contiguous through the head'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.plan_revisions r
                WHERE r.profile_id = p_profile AND r.plan_id = p_plan
                  AND r.kind = 'adjustment'
                  AND EXISTS (
                      SELECT 1 FROM public.plan_revisions earlier
                      WHERE earlier.profile_id = r.profile_id
                        AND earlier.plan_id = r.plan_id
                        AND earlier.revision < r.revision
                        AND earlier.kind = 'adjustment'
                        AND earlier.effective_from > r.effective_from
                  )
            ) THEN
                RAISE EXCEPTION 'plan effective boundary cannot move backwards'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER plan_revision_complete_from_root
            AFTER INSERT OR UPDATE ON training_plans DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION garmin_coach_assert_plan_revision();
        CREATE CONSTRAINT TRIGGER plan_revision_complete_from_history
            AFTER INSERT ON plan_revisions DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION garmin_coach_assert_plan_revision();

        CREATE FUNCTION garmin_coach_assert_snapshot_matches()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE p_profile uuid := COALESCE(NEW.profile_id, OLD.profile_id);
        DECLARE p_plan uuid := COALESCE(NEW.plan_id, OLD.plan_id);
        DECLARE p_revision bigint := COALESCE(NEW.revision, OLD.revision);
        DECLARE p_session uuid := COALESCE(NEW.planned_session_id, OLD.planned_session_id);
        DECLARE disposition_value text;
        DECLARE match_count bigint;
        BEGIN
            SELECT disposition INTO disposition_value
            FROM public.plan_revision_planned_sessions
            WHERE profile_id = p_profile AND plan_id = p_plan
              AND revision = p_revision AND planned_session_id = p_session;
            IF disposition_value IS NULL THEN RETURN NULL; END IF;
            SELECT count(*) INTO match_count FROM public.plan_revision_matches
            WHERE profile_id = p_profile AND plan_id = p_plan
              AND revision = p_revision AND planned_session_id = p_session;
            IF (disposition_value = 'fulfilled' AND match_count = 0)
               OR (disposition_value <> 'fulfilled' AND match_count <> 0) THEN
                RAISE EXCEPTION 'planned-session disposition and matches disagree'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER snapshot_disposition_from_session
            AFTER INSERT ON plan_revision_planned_sessions DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION garmin_coach_assert_snapshot_matches();
        CREATE CONSTRAINT TRIGGER snapshot_disposition_from_match
            AFTER INSERT ON plan_revision_matches DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION garmin_coach_assert_snapshot_matches();

        CREATE FUNCTION garmin_coach_assert_current_matches()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE p_profile uuid := COALESCE(
            (to_jsonb(NEW) ->> 'profile_id')::uuid,
            (to_jsonb(OLD) ->> 'profile_id')::uuid
        );
        DECLARE p_plan uuid := COALESCE(
            (to_jsonb(NEW) ->> 'plan_id')::uuid,
            (to_jsonb(NEW) ->> 'id')::uuid,
            (to_jsonb(OLD) ->> 'plan_id')::uuid,
            (to_jsonb(OLD) ->> 'id')::uuid
        );
        DECLARE head bigint;
        BEGIN
            SELECT current_revision INTO head FROM public.training_plans
            WHERE profile_id = p_profile AND id = p_plan;
            IF head IS NULL THEN RETURN NULL; END IF;
            IF EXISTS (
                (SELECT planned_session_id, position, training_session_id
                 FROM public.plan_revision_matches
                 WHERE profile_id = p_profile AND plan_id = p_plan AND revision = head
                 EXCEPT
                 SELECT planned_session_id, position, training_session_id
                 FROM public.planned_session_current_matches
                 WHERE profile_id = p_profile AND plan_id = p_plan)
                UNION ALL
                (SELECT planned_session_id, position, training_session_id
                 FROM public.planned_session_current_matches
                 WHERE profile_id = p_profile AND plan_id = p_plan
                 EXCEPT
                 SELECT planned_session_id, position, training_session_id
                 FROM public.plan_revision_matches
                 WHERE profile_id = p_profile AND plan_id = p_plan AND revision = head)
            ) THEN
                RAISE EXCEPTION 'current matches must equal the plan head snapshot'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER current_matches_from_root
            AFTER INSERT OR UPDATE ON training_plans DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION garmin_coach_assert_current_matches();
        CREATE CONSTRAINT TRIGGER current_matches_from_projection
            AFTER INSERT OR UPDATE OR DELETE ON planned_session_current_matches
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_assert_current_matches();
        CREATE CONSTRAINT TRIGGER current_matches_from_history
            AFTER INSERT ON plan_revision_matches DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION garmin_coach_assert_current_matches();
        """
    )

    # Snapshot authority is checked from both the revision root and affected
    # children. The constraint triggers stay deferred so writers can insert a
    # complete immutable snapshot in natural parent-before-child order.
    op.execute(
        """
        CREATE FUNCTION garmin_coach_assert_revision_session_authority()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE p_profile uuid := (to_jsonb(NEW) ->> 'profile_id')::uuid;
        DECLARE p_plan uuid := (to_jsonb(NEW) ->> 'plan_id')::uuid;
        DECLARE p_revision bigint := (to_jsonb(NEW) ->> 'revision')::bigint;
        DECLARE revision_kind text;
        DECLARE revision_status text;
        BEGIN
            SELECT kind, status INTO revision_kind, revision_status
            FROM public.plan_revisions
            WHERE profile_id = p_profile AND plan_id = p_plan
              AND revision = p_revision;
            IF revision_kind IS NULL THEN RETURN NULL; END IF;

            IF p_revision = 1
               AND (revision_kind <> 'create' OR revision_status <> 'draft') THEN
                RAISE EXCEPTION 'first plan revision must be a draft create'
                    USING ERRCODE = '23514';
            END IF;
            IF revision_kind = 'create' AND EXISTS (
                SELECT 1
                FROM public.plan_revision_planned_sessions fresh
                WHERE fresh.profile_id = p_profile
                  AND fresh.plan_id = p_plan
                  AND fresh.revision = p_revision
                  AND (
                      fresh.disposition <> 'scheduled'
                      OR fresh.fulfilment_note IS NOT NULL
                      OR EXISTS (
                          SELECT 1 FROM public.plan_revision_matches matched
                          WHERE matched.profile_id = fresh.profile_id
                            AND matched.plan_id = fresh.plan_id
                            AND matched.revision = fresh.revision
                            AND matched.planned_session_id = fresh.planned_session_id
                      )
                  )
            ) THEN
                RAISE EXCEPTION 'create snapshot sessions must be unfulfilled and scheduled'
                    USING ERRCODE = '23514';
            END IF;

            IF revision_kind = 'fulfilment_correction' AND EXISTS (
                SELECT 1
                FROM public.plan_revision_planned_sessions fresh
                LEFT JOIN public.plan_revision_planned_sessions previous
                  ON previous.profile_id = fresh.profile_id
                 AND previous.plan_id = fresh.plan_id
                 AND previous.revision = fresh.revision - 1
                 AND previous.planned_session_id = fresh.planned_session_id
                WHERE fresh.profile_id = p_profile
                  AND fresh.plan_id = p_plan
                  AND fresh.revision = p_revision
                  AND (
                      (fresh.disposition = 'cancelled'
                       AND previous.disposition IS DISTINCT FROM 'cancelled')
                      OR
                      (previous.disposition = 'cancelled' AND (
                          ROW(
                              fresh.position, fresh.scheduled_date, fresh.sport,
                              fresh.session_type, fresh.prescription,
                              fresh.target_duration_seconds,
                              fresh.target_distance_meters,
                              fresh.effort_guidance, fresh.disposition,
                              fresh.fulfilment_note
                          ) IS DISTINCT FROM ROW(
                              previous.position, previous.scheduled_date,
                              previous.sport, previous.session_type,
                              previous.prescription,
                              previous.target_duration_seconds,
                              previous.target_distance_meters,
                              previous.effort_guidance, previous.disposition,
                              previous.fulfilment_note
                          )
                          OR EXISTS (
                              (SELECT position, training_session_id
                               FROM public.plan_revision_matches
                               WHERE profile_id = fresh.profile_id
                                 AND plan_id = fresh.plan_id
                                 AND revision = fresh.revision
                                 AND planned_session_id = fresh.planned_session_id
                               EXCEPT
                               SELECT position, training_session_id
                               FROM public.plan_revision_matches
                               WHERE profile_id = previous.profile_id
                                 AND plan_id = previous.plan_id
                                 AND revision = previous.revision
                                 AND planned_session_id = previous.planned_session_id)
                              UNION ALL
                              (SELECT position, training_session_id
                               FROM public.plan_revision_matches
                               WHERE profile_id = previous.profile_id
                                 AND plan_id = previous.plan_id
                                 AND revision = previous.revision
                                 AND planned_session_id = previous.planned_session_id
                               EXCEPT
                               SELECT position, training_session_id
                               FROM public.plan_revision_matches
                               WHERE profile_id = fresh.profile_id
                                 AND plan_id = fresh.plan_id
                                 AND revision = fresh.revision
                                 AND planned_session_id = fresh.planned_session_id)
                          )
                      ))
                  )
            ) THEN
                RAISE EXCEPTION 'fulfilment correction changed cancellation authority'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER plan_session_authority_from_revision
            AFTER INSERT ON plan_revisions DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION garmin_coach_assert_revision_session_authority();
        CREATE CONSTRAINT TRIGGER plan_session_authority_from_session
            AFTER INSERT ON plan_revision_planned_sessions
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_assert_revision_session_authority();
        CREATE CONSTRAINT TRIGGER plan_session_authority_from_match
            AFTER INSERT ON plan_revision_matches DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION garmin_coach_assert_revision_session_authority();

        CREATE FUNCTION garmin_coach_assert_planned_session_in_revision_range()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE plan_starts date;
        DECLARE plan_ends date;
        BEGIN
            SELECT starts_on, ends_on INTO plan_starts, plan_ends
            FROM public.plan_revisions
            WHERE profile_id = NEW.profile_id AND plan_id = NEW.plan_id
              AND revision = NEW.revision;
            IF plan_starts IS NULL
               OR NEW.scheduled_date NOT BETWEEN plan_starts AND plan_ends THEN
                RAISE EXCEPTION 'planned session date must fall within its revision range'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER planned_session_in_revision_range
            AFTER INSERT ON plan_revision_planned_sessions
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION garmin_coach_assert_planned_session_in_revision_range();
        """
    )

    # Earlier prescriptions are frozen in PostgreSQL, independent of adapter
    # behavior. Other revision kinds may only make their documented class of
    # change; every snapshot remains full and immutable.
    op.execute(
        """
        CREATE FUNCTION garmin_coach_assert_frozen_prescriptions()
        RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE previous_revision bigint := NEW.revision - 1;
        BEGIN
            IF NEW.revision = 1 THEN
                IF NEW.kind <> 'create' THEN
                    RAISE EXCEPTION 'first plan revision must be create' USING ERRCODE = '23514';
                END IF;
                RETURN NULL;
            END IF;
            IF NEW.kind = 'create' THEN
                RAISE EXCEPTION 'create is only valid for revision one' USING ERRCODE = '23514';
            END IF;
            IF NEW.kind <> 'fulfilment_correction' AND EXISTS (
                SELECT 1 FROM public.plan_revisions previous_head
                WHERE previous_head.profile_id = NEW.profile_id
                  AND previous_head.plan_id = NEW.plan_id
                  AND previous_head.revision = previous_revision
                  AND previous_head.status = 'archived'
            ) THEN
                RAISE EXCEPTION 'archived plans accept only fulfilment corrections'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.kind = 'adjustment' AND EXISTS (
                SELECT 1 FROM public.plan_revisions previous_head
                WHERE previous_head.profile_id = NEW.profile_id
                  AND previous_head.plan_id = NEW.plan_id
                  AND previous_head.revision = previous_revision
                  AND previous_head.status IS DISTINCT FROM NEW.status
            ) THEN
                RAISE EXCEPTION 'adjustment revision changed plan status'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.plan_revision_planned_sessions previous
                WHERE previous.profile_id = NEW.profile_id
                  AND previous.plan_id = NEW.plan_id
                  AND previous.revision = previous_revision
                  AND NOT EXISTS (
                      SELECT 1 FROM public.plan_revision_planned_sessions fresh
                      WHERE fresh.profile_id = previous.profile_id
                        AND fresh.plan_id = previous.plan_id
                        AND fresh.revision = NEW.revision
                        AND fresh.planned_session_id = previous.planned_session_id
                  )
            ) THEN
                RAISE EXCEPTION 'planned sessions cannot disappear from history'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.kind IN ('lifecycle', 'fulfilment_correction') AND (
                EXISTS (
                    SELECT 1 FROM public.plan_revision_planned_sessions fresh
                    WHERE fresh.profile_id = NEW.profile_id
                      AND fresh.plan_id = NEW.plan_id
                      AND fresh.revision = NEW.revision
                      AND NOT EXISTS (
                          SELECT 1 FROM public.plan_revision_planned_sessions previous
                          WHERE previous.profile_id = fresh.profile_id
                            AND previous.plan_id = fresh.plan_id
                            AND previous.revision = previous_revision
                            AND previous.planned_session_id = fresh.planned_session_id
                      )
                )
                OR EXISTS (
                    SELECT 1 FROM public.plan_revisions previous_head
                    WHERE previous_head.profile_id = NEW.profile_id
                      AND previous_head.plan_id = NEW.plan_id
                      AND previous_head.revision = previous_revision
                      AND (
                          previous_head.name IS DISTINCT FROM NEW.name OR
                          previous_head.starts_on IS DISTINCT FROM NEW.starts_on OR
                          previous_head.ends_on IS DISTINCT FROM NEW.ends_on OR
                          (NEW.kind = 'fulfilment_correction' AND
                           previous_head.status IS DISTINCT FROM NEW.status)
                      )
                )
                OR EXISTS (
                    (SELECT position, goal_event_id, goal_event_revision
                     FROM public.plan_revision_goal_events
                     WHERE profile_id = NEW.profile_id AND plan_id = NEW.plan_id
                       AND revision = NEW.revision
                     EXCEPT
                     SELECT position, goal_event_id, goal_event_revision
                     FROM public.plan_revision_goal_events
                     WHERE profile_id = NEW.profile_id AND plan_id = NEW.plan_id
                       AND revision = previous_revision)
                    UNION ALL
                    (SELECT position, goal_event_id, goal_event_revision
                     FROM public.plan_revision_goal_events
                     WHERE profile_id = NEW.profile_id AND plan_id = NEW.plan_id
                       AND revision = previous_revision
                     EXCEPT
                     SELECT position, goal_event_id, goal_event_revision
                     FROM public.plan_revision_goal_events
                     WHERE profile_id = NEW.profile_id AND plan_id = NEW.plan_id
                       AND revision = NEW.revision)
                )
                OR EXISTS (
                    (SELECT position, statement FROM public.plan_revision_constraints
                     WHERE profile_id = NEW.profile_id AND plan_id = NEW.plan_id
                       AND revision = NEW.revision
                     EXCEPT
                     SELECT position, statement FROM public.plan_revision_constraints
                     WHERE profile_id = NEW.profile_id AND plan_id = NEW.plan_id
                       AND revision = previous_revision)
                    UNION ALL
                    (SELECT position, statement FROM public.plan_revision_constraints
                     WHERE profile_id = NEW.profile_id AND plan_id = NEW.plan_id
                       AND revision = previous_revision
                     EXCEPT
                     SELECT position, statement FROM public.plan_revision_constraints
                     WHERE profile_id = NEW.profile_id AND plan_id = NEW.plan_id
                       AND revision = NEW.revision)
                )
            ) THEN
                RAISE EXCEPTION 'non-adjustment revision changed plan authority'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.kind = 'adjustment' AND EXISTS (
                SELECT 1
                FROM public.plan_revision_planned_sessions fresh
                LEFT JOIN public.plan_revision_planned_sessions previous
                  ON previous.profile_id = fresh.profile_id
                 AND previous.plan_id = fresh.plan_id
                 AND previous.revision = previous_revision
                 AND previous.planned_session_id = fresh.planned_session_id
                WHERE fresh.profile_id = NEW.profile_id AND fresh.plan_id = NEW.plan_id
                  AND fresh.revision = NEW.revision
                  AND (
                    (previous.planned_session_id IS NULL
                     AND fresh.scheduled_date < NEW.effective_from)
                    OR
                    (previous.planned_session_id IS NOT NULL
                     AND (previous.scheduled_date < NEW.effective_from
                          OR fresh.scheduled_date < NEW.effective_from)
                     AND (ROW(
                        fresh.position, fresh.scheduled_date, fresh.sport,
                        fresh.session_type, fresh.prescription,
                        fresh.target_duration_seconds,
                        fresh.target_distance_meters, fresh.effort_guidance,
                        fresh.disposition, fresh.fulfilment_note
                     ) IS DISTINCT FROM ROW(
                        previous.position, previous.scheduled_date, previous.sport,
                        previous.session_type, previous.prescription,
                        previous.target_duration_seconds,
                        previous.target_distance_meters, previous.effort_guidance,
                        previous.disposition, previous.fulfilment_note
                     ) OR EXISTS (
                        (SELECT position, training_session_id
                         FROM public.plan_revision_matches
                         WHERE profile_id = fresh.profile_id
                           AND plan_id = fresh.plan_id
                           AND revision = fresh.revision
                           AND planned_session_id = fresh.planned_session_id
                         EXCEPT
                         SELECT position, training_session_id
                         FROM public.plan_revision_matches
                         WHERE profile_id = previous.profile_id
                           AND plan_id = previous.plan_id
                           AND revision = previous.revision
                           AND planned_session_id = previous.planned_session_id)
                        UNION ALL
                        (SELECT position, training_session_id
                         FROM public.plan_revision_matches
                         WHERE profile_id = previous.profile_id
                           AND plan_id = previous.plan_id
                           AND revision = previous.revision
                           AND planned_session_id = previous.planned_session_id
                         EXCEPT
                         SELECT position, training_session_id
                         FROM public.plan_revision_matches
                         WHERE profile_id = fresh.profile_id
                           AND plan_id = fresh.plan_id
                           AND revision = fresh.revision
                           AND planned_session_id = fresh.planned_session_id)
                     )))
                  )
            ) THEN
                RAISE EXCEPTION 'prescriptions before effective_from are frozen'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.kind = 'adjustment' AND EXISTS (
                SELECT 1
                FROM public.plan_revision_planned_sessions fresh
                LEFT JOIN public.plan_revision_planned_sessions previous
                  ON previous.profile_id = fresh.profile_id
                 AND previous.plan_id = fresh.plan_id
                 AND previous.revision = previous_revision
                 AND previous.planned_session_id = fresh.planned_session_id
                WHERE fresh.profile_id = NEW.profile_id AND fresh.plan_id = NEW.plan_id
                  AND fresh.revision = NEW.revision
                  AND (
                    (previous.planned_session_id IS NULL AND (
                        fresh.disposition <> 'scheduled'
                        OR fresh.fulfilment_note IS NOT NULL
                        OR EXISTS (
                            SELECT 1 FROM public.plan_revision_matches m
                            WHERE m.profile_id = fresh.profile_id
                              AND m.plan_id = fresh.plan_id
                              AND m.revision = fresh.revision
                              AND m.planned_session_id = fresh.planned_session_id
                        )
                    ))
                    OR
                    (previous.planned_session_id IS NOT NULL AND (
                        fresh.fulfilment_note IS DISTINCT FROM previous.fulfilment_note
                        OR (
                            fresh.disposition IS DISTINCT FROM previous.disposition
                            AND NOT (
                                previous.disposition = 'scheduled'
                                AND fresh.disposition = 'cancelled'
                            )
                        )
                        OR EXISTS (
                            (SELECT position, training_session_id
                             FROM public.plan_revision_matches
                             WHERE profile_id = fresh.profile_id
                               AND plan_id = fresh.plan_id
                               AND revision = fresh.revision
                               AND planned_session_id = fresh.planned_session_id
                             EXCEPT
                             SELECT position, training_session_id
                             FROM public.plan_revision_matches
                             WHERE profile_id = previous.profile_id
                               AND plan_id = previous.plan_id
                               AND revision = previous.revision
                               AND planned_session_id = previous.planned_session_id)
                            UNION ALL
                            (SELECT position, training_session_id
                             FROM public.plan_revision_matches
                             WHERE profile_id = previous.profile_id
                               AND plan_id = previous.plan_id
                               AND revision = previous.revision
                               AND planned_session_id = previous.planned_session_id
                             EXCEPT
                             SELECT position, training_session_id
                             FROM public.plan_revision_matches
                             WHERE profile_id = fresh.profile_id
                               AND plan_id = fresh.plan_id
                               AND revision = fresh.revision
                               AND planned_session_id = fresh.planned_session_id)
                        )
                    ))
                  )
            ) THEN
                RAISE EXCEPTION 'adjustment revision changed fulfilment authority'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.kind IN ('lifecycle', 'fulfilment_correction') AND EXISTS (
                SELECT 1 FROM public.plan_revision_planned_sessions fresh
                JOIN public.plan_revision_planned_sessions previous
                  ON previous.profile_id = fresh.profile_id
                 AND previous.plan_id = fresh.plan_id
                 AND previous.revision = previous_revision
                 AND previous.planned_session_id = fresh.planned_session_id
                WHERE fresh.profile_id = NEW.profile_id AND fresh.plan_id = NEW.plan_id
                  AND fresh.revision = NEW.revision
                  AND ROW(fresh.position, fresh.scheduled_date, fresh.sport,
                          fresh.session_type, fresh.prescription,
                          fresh.target_duration_seconds, fresh.target_distance_meters,
                          fresh.effort_guidance)
                      IS DISTINCT FROM
                      ROW(previous.position, previous.scheduled_date, previous.sport,
                          previous.session_type, previous.prescription,
                          previous.target_duration_seconds,
                          previous.target_distance_meters,
                          previous.effort_guidance)
            ) THEN
                RAISE EXCEPTION 'non-adjustment revision changed a prescription'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.kind = 'lifecycle' AND EXISTS (
                SELECT 1 FROM public.plan_revision_planned_sessions fresh
                JOIN public.plan_revision_planned_sessions previous
                  ON previous.profile_id = fresh.profile_id
                 AND previous.plan_id = fresh.plan_id
                 AND previous.revision = previous_revision
                 AND previous.planned_session_id = fresh.planned_session_id
                WHERE fresh.profile_id = NEW.profile_id AND fresh.plan_id = NEW.plan_id
                  AND fresh.revision = NEW.revision
                  AND (
                    fresh.disposition IS DISTINCT FROM previous.disposition OR
                    fresh.fulfilment_note IS DISTINCT FROM previous.fulfilment_note OR
                    EXISTS (
                        (SELECT position, training_session_id
                         FROM public.plan_revision_matches
                         WHERE profile_id = fresh.profile_id
                           AND plan_id = fresh.plan_id
                           AND revision = fresh.revision
                           AND planned_session_id = fresh.planned_session_id
                         EXCEPT
                         SELECT position, training_session_id
                         FROM public.plan_revision_matches
                         WHERE profile_id = previous.profile_id
                           AND plan_id = previous.plan_id
                           AND revision = previous.revision
                           AND planned_session_id = previous.planned_session_id)
                        UNION ALL
                        (SELECT position, training_session_id
                         FROM public.plan_revision_matches
                         WHERE profile_id = previous.profile_id
                           AND plan_id = previous.plan_id
                           AND revision = previous.revision
                           AND planned_session_id = previous.planned_session_id
                         EXCEPT
                         SELECT position, training_session_id
                         FROM public.plan_revision_matches
                         WHERE profile_id = fresh.profile_id
                           AND plan_id = fresh.plan_id
                           AND revision = fresh.revision
                           AND planned_session_id = fresh.planned_session_id)
                    )
                  )
            ) THEN
                RAISE EXCEPTION 'lifecycle revision changed fulfilment authority'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END;
        $$;
        CREATE CONSTRAINT TRIGGER plan_prescriptions_frozen
            AFTER INSERT ON plan_revisions DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION garmin_coach_assert_frozen_prescriptions();
        """
    )

    op.execute(
        """
        DO $block$
        DECLARE granted_role text;
        DECLARE history_table text;
        BEGIN
            FOREACH history_table IN ARRAY ARRAY[
                'plan_revisions', 'planned_sessions',
                'plan_revision_goal_events', 'plan_revision_constraints',
                'plan_revision_planned_sessions', 'plan_revision_matches'
            ] LOOP
                FOR granted_role IN
                    SELECT DISTINCT grantee FROM information_schema.role_table_grants
                    WHERE table_schema = 'public' AND table_name = history_table
                      AND privilege_type IN ('UPDATE', 'DELETE', 'TRUNCATE')
                      AND grantee <> current_user
                LOOP
                    EXECUTE format(
                        'REVOKE UPDATE, DELETE, TRUNCATE ON public.%I FROM %I',
                        history_table, granted_role
                    );
                END LOOP;
            END LOOP;
            FOR granted_role IN
                SELECT DISTINCT grantee FROM information_schema.role_table_grants
                WHERE table_schema = 'public'
                  AND table_name = 'planned_session_current_matches'
                  AND privilege_type = 'UPDATE'
                  AND grantee <> current_user
            LOOP
                EXECUTE format(
                    'REVOKE UPDATE ON public.planned_session_current_matches FROM %I',
                    granted_role
                );
            END LOOP;
        END;
        $block$;
        """
    )


def downgrade() -> None:
    # A downgrade must never silently discard App authority. Operators can
    # downgrade a clean schema after exporting/deleting App Records explicitly.
    op.execute(
        """
        DO $block$
        BEGIN
            -- profiles is the transaction gate every application writer or FK
            -- checker touches. Taking it first drains existing writers and
            -- prevents a new writer from slipping between the checks and DDL;
            -- the App authority roots then follow in one documented order.
            LOCK TABLE profiles, goal_events, session_annotations,
                       training_plans, training_sessions
                IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM training_sessions WHERE ownership = 'app')
               OR EXISTS (SELECT 1 FROM session_annotations)
               OR EXISTS (SELECT 1 FROM goal_events)
               OR EXISTS (SELECT 1 FROM training_plans) THEN
                RAISE EXCEPTION '0005 downgrade would discard App Records'
                    USING ERRCODE = '55000';
            END IF;
        END;
        $block$;
        """
    )
    op.execute("DROP TRIGGER plan_prescriptions_frozen ON plan_revisions")
    op.execute("DROP FUNCTION garmin_coach_assert_frozen_prescriptions()")
    op.execute(
        "DROP TRIGGER planned_session_in_revision_range "
        "ON plan_revision_planned_sessions"
    )
    op.execute("DROP FUNCTION garmin_coach_assert_planned_session_in_revision_range()")
    op.execute("DROP TRIGGER plan_session_authority_from_match ON plan_revision_matches")
    op.execute(
        "DROP TRIGGER plan_session_authority_from_session "
        "ON plan_revision_planned_sessions"
    )
    op.execute("DROP TRIGGER plan_session_authority_from_revision ON plan_revisions")
    op.execute("DROP FUNCTION garmin_coach_assert_revision_session_authority()")
    op.execute("DROP TRIGGER current_matches_from_history ON plan_revision_matches")
    op.execute("DROP TRIGGER current_matches_from_projection ON planned_session_current_matches")
    op.execute("DROP TRIGGER current_matches_from_root ON training_plans")
    op.execute("DROP FUNCTION garmin_coach_assert_current_matches()")
    op.execute("DROP TRIGGER snapshot_disposition_from_match ON plan_revision_matches")
    op.execute("DROP TRIGGER snapshot_disposition_from_session ON plan_revision_planned_sessions")
    op.execute("DROP FUNCTION garmin_coach_assert_snapshot_matches()")
    op.execute("DROP TRIGGER plan_revision_complete_from_history ON plan_revisions")
    op.execute("DROP TRIGGER plan_revision_complete_from_root ON training_plans")
    op.execute("DROP FUNCTION garmin_coach_assert_plan_revision()")
    op.execute("DROP TRIGGER plan_head_goal_revision_current ON plan_revision_goal_events")
    op.execute("DROP FUNCTION garmin_coach_assert_head_goal_reference_current()")
    op.execute("DROP TRIGGER plan_goal_revision_valid ON plan_revision_goal_events")
    op.execute("DROP FUNCTION garmin_coach_goal_reference_not_future()")
    op.execute("DROP TRIGGER plan_revision_matches_immutable ON plan_revision_matches")
    op.execute("DROP TRIGGER plan_revision_sessions_immutable ON plan_revision_planned_sessions")
    op.execute("DROP TRIGGER plan_revision_constraints_immutable ON plan_revision_constraints")
    op.execute("DROP TRIGGER plan_revision_goals_immutable ON plan_revision_goal_events")
    op.execute("DROP TRIGGER planned_sessions_immutable ON planned_sessions")
    op.execute("DROP TRIGGER plan_revisions_immutable ON plan_revisions")
    op.execute("DROP FUNCTION garmin_coach_history_immutable()")
    op.execute("DROP TRIGGER training_plan_head_guard ON training_plans")
    op.execute("DROP FUNCTION garmin_coach_guard_plan_head()")
    op.execute("DROP TRIGGER session_annotation_target_immutable ON session_annotations")
    op.execute("DROP FUNCTION garmin_coach_guard_annotation_target()")
    op.execute("DROP TRIGGER goal_event_revision ON goal_events")
    op.execute("DROP TRIGGER session_annotation_revision ON session_annotations")
    op.execute("DROP FUNCTION garmin_coach_guard_app_head()")

    op.execute("ALTER TABLE training_plans DROP CONSTRAINT fk_training_plan_current_revision")
    op.execute("DROP TABLE planned_session_current_matches")
    op.execute("DROP TABLE plan_revision_matches")
    op.execute("DROP TABLE plan_revision_planned_sessions")
    op.execute("DROP TABLE plan_revision_constraints")
    op.execute("DROP TABLE plan_revision_goal_events")
    op.execute("DROP TABLE planned_sessions")
    op.execute("DROP TABLE plan_revisions")
    op.execute("DROP INDEX uq_training_plan_active_profile")
    op.execute("DROP TABLE training_plans")
    op.execute("DROP TABLE goal_events")
    op.execute("DROP TABLE session_annotations")

    op.execute("DROP TRIGGER training_session_identity_immutable ON training_sessions")
    op.execute("DROP FUNCTION garmin_coach_guard_session_identity()")
    op.execute("ALTER TABLE training_sessions DROP CONSTRAINT ck_training_session_app_revision")
    op.execute("ALTER TABLE training_sessions DROP COLUMN app_revision")
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
        $$;
        CREATE TRIGGER training_session_identity_immutable
        BEFORE UPDATE OR DELETE ON training_sessions
        FOR EACH ROW EXECUTE FUNCTION garmin_coach_guard_session_identity();
        """
    )
