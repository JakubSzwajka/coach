from __future__ import annotations

import threading
import time
import unittest
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

import psycopg

from coach.application import (
    ActivateTrainingPlan,
    AdjustTrainingPlan,
    ArchiveTrainingPlan,
    ClerkActor,
    CoachApplication,
    CollectedBatch,
    CollectedCapture,
    CollectedTrainingSession,
    Conflict,
    CreateGoalEvent,
    CreateSessionAnnotation,
    CreateTrainingPlan,
    CreateTrainingSession,
    DeleteGoalEvent,
    DeleteSessionAnnotation,
    DeleteTrainingPlan,
    DeleteTrainingSession,
    EnsureProfile,
    EnsureSourceConnection,
    GetCollectedRecord,
    GetGoalEvent,
    GetProfile,
    GetSessionAnnotation,
    GetTrainingPlan,
    GetTrainingPlanHistory,
    GetTrainingSession,
    InvalidRequest,
    ListGoalEvents,
    ListTrainingPlans,
    NotFound,
    CapturePointer,
    ReplaceGoalEvent,
    ReplaceSessionAnnotation,
    ReplaceTrainingSession,
    SetPlannedSessionFulfilment,
    StaleRevision,
)
from coach.postgres import DatabaseSettings
from coach.postgres.migrate import migrate
from tests.postgres.support import test_database


class TransactionalAppRecordsPostgreSQLTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database = test_database()
        cls.application_url = database.application_url
        cls.migration_url = database.migration_url
        cls.settings = DatabaseSettings.from_url(cls.application_url)
        migrate(DatabaseSettings.from_url(cls.migration_url))

    def setUp(self) -> None:
        with psycopg.connect(self.migration_url) as connection:
            connection.execute("TRUNCATE TABLE profiles CASCADE")
        self.now = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        self.application = CoachApplication(self.settings, clock=lambda: self.now)
        self.actor_a = ClerkActor("https://identity.example.test", "app-record-a")
        self.actor_b = ClerkActor("https://identity.example.test", "app-record-b")
        self.application.execute(self.actor_a, EnsureProfile("Synthetic A"))
        self.application.execute(self.actor_b, EnsureProfile("Synthetic B"))

    def _session_content(self, **overrides: object) -> dict[str, object]:
        content: dict[str, object] = {
            "sport": "running",
            "local_date": "2026-07-20",
            "duration": {"value": 1800, "unit": "seconds", "basis": "active"},
            "session_rpe": 5,
            "loads": [
                {
                    "value": 90,
                    "unit": "rpe_minutes",
                    "method": "session_rpe",
                    "source": "athlete",
                }
            ],
        }
        content.update(overrides)
        return content

    def _goal_content(self, **overrides: object) -> dict[str, object]:
        content: dict[str, object] = {
            "name": "Synthetic 10k",
            "sport": "running",
            "local_date": "2026-10-11",
            "priority": "primary",
        }
        content.update(overrides)
        return content

    def _plan(self, actor: ClerkActor | None = None, *, goal_events=None, count: int = 2):
        actor = actor or self.actor_a
        planned = [
            {
                "scheduled_date": f"2026-07-{21 + index:02d}",
                "sport": "running",
                "prescription": f"Synthetic session {index + 1}",
            }
            for index in range(count)
        ]
        return self.application.execute(
            actor,
            CreateTrainingPlan(
                name="Synthetic plan",
                starts_on="2026-07-20",
                ends_on="2026-08-20",
                reason="Initial synthetic plan",
                goal_events=goal_events,
                constraints=("Synthetic constraint",),
                planned_sessions=planned,
            ),
        )

    def _collected_session(self, actor: ClerkActor, suffix: str):
        source = self.application.execute(
            actor,
            EnsureSourceConnection("synthetic-provider", f"source-{suffix}"),
        )
        profile = self.application.read(actor, GetProfile()).profile
        self.application.ingest(
            profile,
            CollectedBatch(
                source=source,
                captures=(CollectedCapture("activity", f"activity-{suffix}", {"v": 1}),),
                idempotency_key=f"batch-{suffix}",
                sessions=(
                    CollectedTrainingSession(
                        capture=CapturePointer("activity", f"activity-{suffix}"),
                        local_date=date(2026, 7, 20),
                        sport="running",
                        duration_value=1800,
                        duration_unit="seconds",
                        duration_basis="source_reported",
                    ),
                ),
            ),
        )
        projection = self.application.read(
            actor, GetCollectedRecord(source, "activity", f"activity-{suffix}")
        )
        self.assertIsNotNone(projection)
        self.assertIsNotNone(projection.session)
        return source, projection.session

    def _wait_for_database_lock(self, query_fragment: str, count: int = 1) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with psycopg.connect(self.migration_url) as connection:
                waiting = connection.execute(
                    """
                    SELECT count(*) FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND usename = 'coach_test'
                      AND wait_event_type = 'Lock'
                      AND position(%s in query) > 0
                    """,
                    (query_fragment,),
                ).fetchone()[0]
            if waiting >= count:
                return
            time.sleep(0.02)
        self.fail(
            f"expected {count} database lock waiter(s) for {query_fragment!r}"
        )

    def _join_threads(self, threads: list[threading.Thread]) -> None:
        for thread in threads:
            thread.join(10)
        self.assertTrue(
            all(not thread.is_alive() for thread in threads),
            "database contention threads did not terminate",
        )

    def _stage_direct_adjustment(
        self,
        connection: psycopg.Connection,
        plan_id: UUID,
        previous_revision: int,
        *,
        kind: str = "adjustment",
        status: str | None = None,
        disposition: str | None = None,
        mutate_note: bool = False,
        fulfilment_note: str | None = None,
        replacement_match: UUID | None = None,
    ) -> None:
        revision = previous_revision + 1
        connection.execute(
            """
            INSERT INTO plan_revisions (
                profile_id, plan_id, revision, kind, recorded_at,
                recorded_date, reason, effective_from, name, starts_on,
                ends_on, status
            )
            SELECT profile_id, plan_id, %s, %s, %s, %s,
                   'Direct invariant probe',
                   CASE WHEN %s = 'adjustment' THEN starts_on ELSE NULL END,
                   name, starts_on, ends_on, COALESCE(%s, status)
            FROM plan_revisions
            WHERE plan_id = %s AND revision = %s
            """,
            (
                revision,
                kind,
                self.now,
                self.now.date(),
                kind,
                status,
                plan_id,
                previous_revision,
            ),
        )
        connection.execute(
            """
            INSERT INTO plan_revision_goal_events (
                profile_id, plan_id, revision, position,
                goal_event_id, goal_event_revision
            )
            SELECT profile_id, plan_id, %s, position,
                   goal_event_id, goal_event_revision
            FROM plan_revision_goal_events
            WHERE plan_id = %s AND revision = %s
            """,
            (revision, plan_id, previous_revision),
        )
        connection.execute(
            """
            INSERT INTO plan_revision_constraints (
                profile_id, plan_id, revision, position, statement
            )
            SELECT profile_id, plan_id, %s, position, statement
            FROM plan_revision_constraints
            WHERE plan_id = %s AND revision = %s
            """,
            (revision, plan_id, previous_revision),
        )
        connection.execute(
            """
            INSERT INTO plan_revision_planned_sessions (
                profile_id, plan_id, revision, planned_session_id,
                position, scheduled_date, sport, session_type, prescription,
                target_duration_seconds, target_distance_meters,
                effort_guidance, disposition, fulfilment_note
            )
            SELECT profile_id, plan_id, %s, planned_session_id,
                   position, scheduled_date, sport, session_type, prescription,
                   target_duration_seconds, target_distance_meters,
                   effort_guidance, COALESCE(%s, disposition),
                   CASE WHEN %s THEN %s ELSE fulfilment_note END
            FROM plan_revision_planned_sessions
            WHERE plan_id = %s AND revision = %s
            """,
            (
                revision,
                disposition,
                mutate_note,
                fulfilment_note,
                plan_id,
                previous_revision,
            ),
        )
        connection.execute(
            """
            INSERT INTO plan_revision_matches (
                profile_id, plan_id, revision, planned_session_id,
                position, training_session_id
            )
            SELECT profile_id, plan_id, %s, planned_session_id, position,
                   COALESCE(%s, training_session_id)
            FROM plan_revision_matches
            WHERE plan_id = %s AND revision = %s
            """,
            (revision, replacement_match, plan_id, previous_revision),
        )
        if replacement_match is not None:
            connection.execute(
                "DELETE FROM planned_session_current_matches WHERE plan_id = %s",
                (plan_id,),
            )
            connection.execute(
                """
                INSERT INTO planned_session_current_matches (
                    profile_id, plan_id, planned_session_id,
                    training_session_id, position
                )
                SELECT profile_id, plan_id, planned_session_id,
                       training_session_id, position
                FROM plan_revision_matches
                WHERE plan_id = %s AND revision = %s
                """,
                (plan_id, revision),
            )
        connection.execute(
            """
            UPDATE training_plans
            SET current_revision = %s, status = COALESCE(%s, status)
            WHERE id = %s AND current_revision = %s
            """,
            (revision, status, plan_id, previous_revision),
        )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def _stage_direct_create(
        self,
        connection: psycopg.Connection,
        *,
        status: str = "draft",
        scheduled_date: str = "2026-07-21",
        disposition: str = "scheduled",
        fulfilment_note: str | None = None,
        match_id: UUID | None = None,
    ) -> UUID:
        profile_id = connection.execute(
            """
            SELECT id FROM profiles
            WHERE clerk_issuer = %s AND clerk_subject = %s
            """,
            (self.actor_a.issuer, self.actor_a.subject),
        ).fetchone()[0]
        plan_id = uuid4()
        planned_session_id = uuid4()
        connection.execute(
            """
            INSERT INTO training_plans (
                profile_id, id, current_revision, status, created_at, updated_at
            ) VALUES (%s, %s, 1, %s, %s, %s)
            """,
            (profile_id, plan_id, status, self.now, self.now),
        )
        connection.execute(
            """
            INSERT INTO plan_revisions (
                profile_id, plan_id, revision, kind, recorded_at,
                recorded_date, reason, effective_from, name,
                starts_on, ends_on, status
            ) VALUES (
                %s, %s, 1, 'create', %s, %s, 'Direct create probe',
                NULL, 'Direct plan', '2026-07-20', '2026-08-20', %s
            )
            """,
            (profile_id, plan_id, self.now, self.now.date(), status),
        )
        connection.execute(
            """
            INSERT INTO planned_sessions (
                profile_id, plan_id, id, created_revision
            ) VALUES (%s, %s, %s, 1)
            """,
            (profile_id, plan_id, planned_session_id),
        )
        connection.execute(
            """
            INSERT INTO plan_revision_planned_sessions (
                profile_id, plan_id, revision, planned_session_id,
                position, scheduled_date, sport, prescription,
                disposition, fulfilment_note
            ) VALUES (%s, %s, 1, %s, 0, %s, 'running',
                      'Direct prescription', %s, %s)
            """,
            (
                profile_id,
                plan_id,
                planned_session_id,
                scheduled_date,
                disposition,
                fulfilment_note,
            ),
        )
        if match_id is not None:
            connection.execute(
                """
                INSERT INTO plan_revision_matches (
                    profile_id, plan_id, revision, planned_session_id,
                    position, training_session_id
                ) VALUES (%s, %s, 1, %s, 0, %s)
                """,
                (profile_id, plan_id, planned_session_id, match_id),
            )
            connection.execute(
                """
                INSERT INTO planned_session_current_matches (
                    profile_id, plan_id, planned_session_id,
                    training_session_id, position
                ) VALUES (%s, %s, %s, %s, 0)
                """,
                (profile_id, plan_id, planned_session_id, match_id),
            )
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
        return plan_id

    def test_manual_training_session_lifecycle_is_durable_and_revision_checked(self) -> None:
        created = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        self.assertRegex(created.id, r"^app:[0-9a-f]{32}$")
        self.assertEqual(created.revision, 1)
        self.assertEqual(created.content["loads"][0]["value"], 90)

        replaced = self.application.execute(
            self.actor_a,
            ReplaceTrainingSession(
                created.id, 1, self._session_content(sport="strength", loads=None)
            ),
        )
        self.assertEqual(replaced.revision, 2)
        self.assertEqual(replaced.content["sport"], "strength")
        with self.assertRaises(StaleRevision):
            self.application.execute(
                self.actor_a,
                ReplaceTrainingSession(created.id, 1, self._session_content()),
            )
        self.assertEqual(
            CoachApplication(self.settings).read(
                self.actor_a, GetTrainingSession(created.id)
            ),
            replaced,
        )
        deleted = self.application.execute(
            self.actor_a, DeleteTrainingSession(created.id, 2)
        )
        self.assertTrue(deleted.deleted)
        self.assertIsNone(self.application.read(self.actor_a, GetTrainingSession(created.id)))

    def test_create_validation_distinct_identity_and_goal_ordering_match_contract(self) -> None:
        first = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        second = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        self.assertNotEqual(first.id, second.id)
        with self.assertRaises(InvalidRequest):
            self.application.execute(
                self.actor_a,
                CreateTrainingSession(self._session_content(session_rpe=True)),
            )

        nine = self.application.execute(
            self.actor_a,
            CreateGoalEvent(
                self._goal_content(local_start="2026-10-11 09:00:00")
            ),
        )
        eight = self.application.execute(
            self.actor_a,
            CreateGoalEvent(
                self._goal_content(local_start="2026-10-11T08:00:00")
            ),
        )
        ordered = self.application.read(self.actor_a, ListGoalEvents())
        self.assertEqual([item.id for item in ordered.events], [eight.id, nine.id])
        advanced = self.application.execute(
            self.actor_a,
            ReplaceGoalEvent(eight.id, 1, self._goal_content(priority="practice")),
        )
        with self.assertRaises(StaleRevision):
            self.application.execute(
                self.actor_a, DeleteGoalEvent(eight.id, expected_revision=1)
            )
        self.application.execute(
            self.actor_a, DeleteGoalEvent(eight.id, advanced.revision)
        )
        self.assertIsNone(self.application.read(self.actor_a, GetGoalEvent(eight.id)))

    def test_session_annotation_targets_only_same_profile_collected_session(self) -> None:
        _, collected = self._collected_session(self.actor_a, "a")
        before = self.application.read(
            self.actor_a, GetTrainingSession(collected.id)
        )
        annotation = self.application.execute(
            self.actor_a,
            CreateSessionAnnotation(
                collected.id,
                notes="Synthetic source concern",
                reliability="unreliable",
                duplicate=False,
            ),
        )
        self.assertRegex(collected.id, r"^session:[0-9a-f]{32}$")
        self.assertFalse(annotation.duplicate)
        self.assertEqual(annotation.training_session_id, collected.id)
        replaced = self.application.execute(
            self.actor_a,
            ReplaceSessionAnnotation(annotation.id, 1, duplicate=True),
        )
        self.assertTrue(replaced.duplicate)
        with self.assertRaises(StaleRevision):
            self.application.execute(
                self.actor_a,
                ReplaceSessionAnnotation(annotation.id, 1, duplicate=False),
            )
        manual = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        with self.assertRaises(Conflict):
            self.application.execute(
                self.actor_a, CreateSessionAnnotation(manual.id, duplicate=True)
            )
        with self.assertRaises(NotFound):
            self.application.execute(
                self.actor_b, CreateSessionAnnotation(collected.id, duplicate=True)
            )
        self.assertEqual(
            self.application.read(self.actor_a, GetTrainingSession(collected.id)),
            before,
        )
        self.assertEqual(
            self.application.read(self.actor_a, GetSessionAnnotation(annotation.id)),
            replaced,
        )
        self.assertIsNone(
            self.application.read(self.actor_b, GetSessionAnnotation(annotation.id))
        )

    def test_goal_reference_staleness_and_historical_delete_guard(self) -> None:
        event = self.application.execute(
            self.actor_a, CreateGoalEvent(self._goal_content())
        )
        plan = self._plan(
            goal_events=(
                {"goal_event_id": event.id, "goal_event_revision": event.revision},
            )
        )
        advanced = self.application.execute(
            self.actor_a,
            ReplaceGoalEvent(event.id, 1, self._goal_content(priority="secondary")),
        )
        stale = self.application.read(self.actor_a, GetTrainingPlan(plan.id))
        self.assertTrue(stale.content["requires_review"])
        with self.assertRaises(Conflict):
            self.application.execute(
                self.actor_a, ActivateTrainingPlan(plan.id, 1, "Cannot use stale goal")
            )
        refreshed = self.application.execute(
            self.actor_a,
            AdjustTrainingPlan(
                plan.id,
                1,
                "Reviewed advanced synthetic goal",
                "2026-07-20",
                (),
                goal_events=(
                    {
                        "goal_event_id": advanced.id,
                        "goal_event_revision": advanced.revision,
                    },
                ),
            ),
        )
        self.assertFalse(refreshed.content["requires_review"])
        with self.assertRaises(Conflict):
            self.application.execute(
                self.actor_a, DeleteGoalEvent(event.id, advanced.revision)
            )

    def test_plan_revisions_freeze_prescriptions_and_history(self) -> None:
        plan = self._plan(count=1)
        planned = plan.content["planned_sessions"][0]
        adjusted = self.application.execute(
            self.actor_a,
            AdjustTrainingPlan(
                plan.id,
                1,
                "Move future prescription",
                "2026-07-21",
                (
                    {
                        "op": "update",
                        "planned_session_id": planned["id"],
                        "scheduled_date": "2026-07-22",
                        "sport": "running",
                        "prescription": "Adjusted synthetic session",
                    },
                ),
            ),
        )
        history = self.application.read(
            self.actor_a, GetTrainingPlanHistory(plan.id)
        )
        self.assertEqual([item.revision for item in history.revisions], [1, 2])
        self.assertEqual(
            history.revisions[0].plan["planned_sessions"][0]["scheduled_date"],
            "2026-07-21",
        )
        self.assertEqual(adjusted.content["planned_sessions"][0]["id"], planned["id"])
        with self.assertRaises(InvalidRequest):
            self.application.execute(
                self.actor_a,
                AdjustTrainingPlan(
                    plan.id,
                    2,
                    "Attempt past rewrite",
                    "2026-07-23",
                    (
                        {
                            "op": "update",
                            "planned_session_id": planned["id"],
                            "scheduled_date": "2026-07-22",
                            "sport": "running",
                            "prescription": "Illegal frozen rewrite",
                        },
                    ),
                ),
            )

    def test_current_match_can_be_reassigned_but_history_guards_manual_delete(self) -> None:
        session = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        plan = self._plan()
        first, second = plan.content["planned_sessions"]
        fulfilled = self.application.execute(
            self.actor_a,
            SetPlannedSessionFulfilment(
                plan.id, 1, first["id"], "fulfilled", "Synthetic completion", (session.id,)
            ),
        )
        unlinked = self.application.execute(
            self.actor_a,
            SetPlannedSessionFulfilment(
                plan.id, fulfilled.revision, first["id"], "skipped", "Synthetic mismatch"
            ),
        )
        reassigned = self.application.execute(
            self.actor_a,
            SetPlannedSessionFulfilment(
                plan.id,
                unlinked.revision,
                second["id"],
                "fulfilled",
                "Synthetic reassignment",
                (session.id,),
            ),
        )
        self.assertEqual(reassigned.content["planned_sessions"][1]["matches"], [session.id])
        with self.assertRaises(Conflict):
            self.application.execute(
                self.actor_a, DeleteTrainingSession(session.id, session.revision)
            )

    def test_draft_delete_and_archived_fulfilment_follow_lifecycle_rules(self) -> None:
        disposable = self._plan(count=1)
        deleted = self.application.execute(
            self.actor_a, DeleteTrainingPlan(disposable.id, disposable.revision)
        )
        self.assertTrue(deleted.deleted)
        self.assertIsNone(
            self.application.read(self.actor_a, GetTrainingPlan(disposable.id))
        )

        session = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        plan = self._plan(count=1)
        active = self.application.execute(
            self.actor_a, ActivateTrainingPlan(plan.id, 1, "Start synthetic plan")
        )
        archived = self.application.execute(
            self.actor_a,
            ArchiveTrainingPlan(active.id, active.revision, "End synthetic plan"),
        )
        corrected = self.application.execute(
            self.actor_a,
            SetPlannedSessionFulfilment(
                archived.id,
                archived.revision,
                archived.content["planned_sessions"][0]["id"],
                "fulfilled",
                "Late audited completion",
                (session.id,),
            ),
        )
        self.assertEqual(corrected.content["status"], "archived")
        with self.assertRaises(Conflict):
            self.application.execute(
                self.actor_a, DeleteTrainingPlan(corrected.id, corrected.revision)
            )

    def test_annotation_delete_and_collected_session_match_leave_source_read_only(self) -> None:
        _, collected = self._collected_session(self.actor_a, "match-collected")
        annotation = self.application.execute(
            self.actor_a, CreateSessionAnnotation(collected.id, duplicate=True)
        )
        deleted = self.application.execute(
            self.actor_a, DeleteSessionAnnotation(annotation.id, annotation.revision)
        )
        self.assertTrue(deleted.deleted)
        plan = self._plan(count=1)
        fulfilled = self.application.execute(
            self.actor_a,
            SetPlannedSessionFulfilment(
                plan.id,
                plan.revision,
                plan.content["planned_sessions"][0]["id"],
                "fulfilled",
                "Explicit collected-session match",
                (collected.id,),
            ),
        )
        self.assertEqual(
            fulfilled.content["planned_sessions"][0]["matches"], [collected.id]
        )
        with self.assertRaises(Conflict):
            self.application.execute(
                self.actor_a, DeleteTrainingSession(collected.id, 1)
            )

    def test_failed_multi_match_correction_leaves_plan_head_unchanged(self) -> None:
        session = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        foreign = self.application.execute(
            self.actor_b, CreateTrainingSession(self._session_content())
        )
        plan = self._plan(count=1)
        with self.assertRaises(Conflict):
            self.application.execute(
                self.actor_a,
                SetPlannedSessionFulfilment(
                    plan.id,
                    plan.revision,
                    plan.content["planned_sessions"][0]["id"],
                    "fulfilled",
                    "Mixed-profile rollback",
                    (session.id, foreign.id),
                ),
            )
        reread = self.application.read(self.actor_a, GetTrainingPlan(plan.id))
        history = self.application.read(
            self.actor_a, GetTrainingPlanHistory(plan.id)
        )
        self.assertEqual(reread.revision, 1)
        self.assertEqual(len(history.revisions), 1)
        self.assertEqual(reread.content["planned_sessions"][0]["matches"], [])

    def test_forged_training_session_aliases_never_resolve_or_persist(self) -> None:
        manual = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        _, collected = self._collected_session(self.actor_a, "forged-alias")
        forged_collected = "session:" + manual.id.split(":", 1)[1]
        forged_app = "app:" + collected.id.split(":", 1)[1]

        self.assertIsNone(
            self.application.read(self.actor_a, GetTrainingSession(forged_collected))
        )
        self.assertIsNone(
            self.application.read(self.actor_a, GetTrainingSession(forged_app))
        )
        with self.assertRaisesRegex(Conflict, "read-only"):
            self.application.execute(
                self.actor_a, DeleteTrainingSession(forged_collected, 1)
            )
        with self.assertRaises(NotFound):
            self.application.execute(
                self.actor_a, DeleteTrainingSession(forged_app, 1)
            )

        plan = self._plan(count=1)
        planned_session_id = plan.content["planned_sessions"][0]["id"]
        for expected_revision, forged in ((plan.revision, forged_app), (99, forged_collected)):
            with self.subTest(forged=forged):
                with self.assertRaises(Conflict):
                    self.application.execute(
                        self.actor_a,
                        SetPlannedSessionFulfilment(
                            plan.id,
                            expected_revision,
                            planned_session_id,
                            "fulfilled",
                            "Reject forged ownership alias",
                            (forged,),
                        ),
                    )
        reread = self.application.read(self.actor_a, GetTrainingPlan(plan.id))
        history = self.application.read(self.actor_a, GetTrainingPlanHistory(plan.id))
        self.assertEqual(reread.revision, 1)
        self.assertEqual(len(history.revisions), 1)
        self.assertEqual(reread.content["planned_sessions"][0]["matches"], [])

    def test_unhashable_enum_values_are_stable_invalid_requests(self) -> None:
        _, collected = self._collected_session(self.actor_a, "unhashable-enums")
        plan = self._plan(count=1)
        planned_session_id = plan.content["planned_sessions"][0]["id"]
        for invalid in ([], {}):
            with self.subTest(value=invalid, field="goal status"):
                with self.assertRaises(InvalidRequest):
                    self.application.read(
                        self.actor_a, ListGoalEvents(status=invalid)
                    )
            with self.subTest(value=invalid, field="plan status"):
                with self.assertRaises(InvalidRequest):
                    self.application.read(
                        self.actor_a, ListTrainingPlans(status=invalid)
                    )
            with self.subTest(value=invalid, field="disposition"):
                with self.assertRaises(InvalidRequest):
                    self.application.execute(
                        self.actor_a,
                        SetPlannedSessionFulfilment(
                            plan.id,
                            plan.revision,
                            planned_session_id,
                            invalid,
                            "Invalid enum probe",
                        ),
                    )
            with self.subTest(value=invalid, field="reliability"):
                with self.assertRaises(InvalidRequest):
                    self.application.execute(
                        self.actor_a,
                        CreateSessionAnnotation(
                            collected.id, reliability=invalid
                        ),
                    )

    def test_concurrent_manual_replacements_have_one_winner(self) -> None:
        created = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        session_id = UUID(hex=created.id.split(":", 1)[1])
        outcomes: list[object] = []
        lock = threading.Lock()

        def replace(sport: str) -> None:
            app = CoachApplication(self.settings, clock=lambda: self.now)
            try:
                result: object = app.execute(
                    self.actor_a,
                    ReplaceTrainingSession(
                        created.id, 1, self._session_content(sport=sport)
                    ),
                )
            except Exception as exc:  # recorded for the deterministic multiset
                result = exc
            with lock:
                outcomes.append(result)

        blocker = psycopg.connect(self.application_url)
        threads = [
            threading.Thread(target=replace, args=(sport,))
            for sport in ("cycling", "strength")
        ]
        try:
            blocker.execute(
                "SELECT 1 FROM training_sessions WHERE id = %s FOR UPDATE",
                (session_id,),
            )
            for thread in threads:
                thread.start()
            self._wait_for_database_lock("UPDATE training_sessions SET", count=2)
            blocker.commit()
            self._join_threads(threads)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(sum(isinstance(item, StaleRevision) for item in outcomes), 1)
        self.assertEqual(sum(getattr(item, "revision", None) == 2 for item in outcomes), 1)
        reread = self.application.read(self.actor_a, GetTrainingSession(created.id))
        self.assertEqual(reread.revision, 2)
        self.assertIn(reread.content["sport"], {"cycling", "strength"})
        self.assertEqual(len(reread.content["loads"]), 1)

    def test_concurrent_activation_is_profile_scoped_and_rolls_back_loser(self) -> None:
        first = self._plan()
        second = self._plan()
        plan_ids = sorted(
            [UUID(hex=item.id.split(":", 1)[1]) for item in (first, second)],
            key=lambda item: item.int,
        )
        outcomes: list[object] = []
        lock = threading.Lock()

        def activate(plan_id: str) -> None:
            app = CoachApplication(self.settings, clock=lambda: self.now)
            try:
                result: object = app.execute(
                    self.actor_a, ActivateTrainingPlan(plan_id, 1, "Concurrent activation")
                )
            except Exception as exc:
                result = exc
            with lock:
                outcomes.append(result)

        blocker = psycopg.connect(self.application_url)
        threads = [
            threading.Thread(target=activate, args=(item.id,))
            for item in (first, second)
        ]
        try:
            blocker.execute(
                """
                SELECT 1 FROM training_plans
                WHERE id = ANY(%s) ORDER BY id FOR UPDATE
                """,
                (plan_ids,),
            ).fetchall()
            for thread in threads:
                thread.start()
            self._wait_for_database_lock(
                "SELECT current_revision FROM training_plans", count=2
            )
            blocker.commit()
            self._join_threads(threads)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(sum(isinstance(item, Conflict) for item in outcomes), 1)
        listed = self.application.read(self.actor_a, ListTrainingPlans())
        self.assertEqual(sum(item.content["status"] == "active" for item in listed.plans), 1)
        self.assertEqual(sorted(item.revision for item in listed.plans), [1, 2])
        self.assertEqual(
            sorted(
                len(self.application.read(self.actor_a, GetTrainingPlanHistory(item.id)).revisions)
                for item in (first, second)
            ),
            [1, 2],
        )

        other = self._plan(self.actor_b)
        activated_other = self.application.execute(
            self.actor_b, ActivateTrainingPlan(other.id, 1, "Independent profile")
        )
        self.assertEqual(activated_other.content["status"], "active")

    def test_goal_reference_writer_contention_rolls_back_stale_plan_create(self) -> None:
        event = self.application.execute(
            self.actor_a, CreateGoalEvent(self._goal_content())
        )
        event_id = UUID(hex=event.id.split(":", 1)[1])
        outcomes: list[object] = []

        def create_plan() -> None:
            app = CoachApplication(self.settings, clock=lambda: self.now)
            try:
                result: object = app.execute(
                    self.actor_a,
                    CreateTrainingPlan(
                        "Contended goal plan",
                        "2026-07-20",
                        "2026-08-20",
                        "Must observe committed goal head",
                        goal_events=(
                            {
                                "goal_event_id": event.id,
                                "goal_event_revision": event.revision,
                            },
                        ),
                    ),
                )
            except Exception as exc:
                result = exc
            outcomes.append(result)

        blocker = psycopg.connect(self.application_url)
        thread = threading.Thread(target=create_plan)
        try:
            blocker.execute(
                """
                UPDATE goal_events
                SET priority = 'secondary', revision = revision + 1
                WHERE id = %s
                """,
                (event_id,),
            )
            thread.start()
            self._wait_for_database_lock("SELECT revision FROM goal_events")
            blocker.commit()
            self._join_threads([thread])
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], Conflict)
        self.assertEqual(
            self.application.read(self.actor_a, ListTrainingPlans()).plans, ()
        )
        advanced = self.application.read(self.actor_a, GetGoalEvent(event.id))
        self.assertEqual(advanced.revision, 2)
        self.assertEqual(advanced.content["priority"], "secondary")

    def test_current_match_swap_contention_terminates_without_partial_writes(self) -> None:
        first_session = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        second_session = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content(sport="cycling"))
        )
        first_plan = self._plan(count=1)
        second_plan = self._plan(count=1)
        first_planned = first_plan.content["planned_sessions"][0]
        second_planned = second_plan.content["planned_sessions"][0]
        first_head = self.application.execute(
            self.actor_a,
            SetPlannedSessionFulfilment(
                first_plan.id,
                first_plan.revision,
                first_planned["id"],
                "fulfilled",
                "Initial first match",
                (first_session.id,),
            ),
        )
        second_head = self.application.execute(
            self.actor_a,
            SetPlannedSessionFulfilment(
                second_plan.id,
                second_plan.revision,
                second_planned["id"],
                "fulfilled",
                "Initial second match",
                (second_session.id,),
            ),
        )
        session_ids = sorted(
            [
                UUID(hex=first_session.id.split(":", 1)[1]),
                UUID(hex=second_session.id.split(":", 1)[1]),
            ],
            key=lambda item: item.int,
        )
        outcomes: list[object] = []
        outcome_lock = threading.Lock()

        def swap(plan, planned_session_id: str, incoming_session_id: str) -> None:
            app = CoachApplication(self.settings, clock=lambda: self.now)
            try:
                result: object = app.execute(
                    self.actor_a,
                    SetPlannedSessionFulfilment(
                        plan.id,
                        plan.revision,
                        planned_session_id,
                        "fulfilled",
                        "Concurrent synthetic swap",
                        (incoming_session_id,),
                    ),
                )
            except Exception as exc:
                result = exc
            with outcome_lock:
                outcomes.append(result)

        threads = [
            threading.Thread(
                target=swap,
                args=(first_head, first_planned["id"], second_session.id),
            ),
            threading.Thread(
                target=swap,
                args=(second_head, second_planned["id"], first_session.id),
            ),
        ]
        blocker = psycopg.connect(self.application_url)
        try:
            blocker.execute(
                """
                SELECT 1 FROM training_sessions
                WHERE id = ANY(%s) ORDER BY id FOR NO KEY UPDATE
                """,
                (session_ids,),
            ).fetchall()
            for thread in threads:
                thread.start()
            self._wait_for_database_lock(
                "SELECT 1 FROM training_sessions", count=2
            )
            blocker.commit()
            self._join_threads(threads)
        finally:
            blocker.rollback()
            blocker.close()
        self.assertEqual(sum(isinstance(item, Conflict) for item in outcomes), 2)
        first_after = self.application.read(
            self.actor_a, GetTrainingPlan(first_plan.id)
        )
        second_after = self.application.read(
            self.actor_a, GetTrainingPlan(second_plan.id)
        )
        self.assertEqual(first_after.revision, 2)
        self.assertEqual(second_after.revision, 2)
        self.assertEqual(
            first_after.content["planned_sessions"][0]["matches"],
            [first_session.id],
        )
        self.assertEqual(
            second_after.content["planned_sessions"][0]["matches"],
            [second_session.id],
        )
        self.assertEqual(
            len(
                self.application.read(
                    self.actor_a, GetTrainingPlanHistory(first_plan.id)
                ).revisions
            ),
            2,
        )
        self.assertEqual(
            len(
                self.application.read(
                    self.actor_a, GetTrainingPlanHistory(second_plan.id)
                ).revisions
            ),
            2,
        )

    def test_active_plan_fulfilment_allows_a_stale_goal_reference(self) -> None:
        event = self.application.execute(
            self.actor_a, CreateGoalEvent(self._goal_content())
        )
        plan = self._plan(
            goal_events=(
                {
                    "goal_event_id": event.id,
                    "goal_event_revision": event.revision,
                },
            ),
            count=1,
        )
        active = self.application.execute(
            self.actor_a, ActivateTrainingPlan(plan.id, 1, "Activate current goal")
        )
        self.application.execute(
            self.actor_a,
            ReplaceGoalEvent(
                event.id,
                event.revision,
                self._goal_content(priority="secondary"),
            ),
        )
        corrected = self.application.execute(
            self.actor_a,
            SetPlannedSessionFulfilment(
                active.id,
                active.revision,
                active.content["planned_sessions"][0]["id"],
                "skipped",
                "Goal review is independent from audited fulfilment",
            ),
        )
        self.assertEqual(corrected.revision, 3)
        self.assertEqual(corrected.content["status"], "active")
        self.assertTrue(corrected.content["requires_review"])
        self.assertEqual(
            corrected.content["planned_sessions"][0]["disposition"], "skipped"
        )

    def test_app_point_reads_hide_malformed_unavailable_and_cross_profile_ids(self) -> None:
        session = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        _, collected = self._collected_session(self.actor_a, "point-read")
        annotation = self.application.execute(
            self.actor_a, CreateSessionAnnotation(collected.id, duplicate=True)
        )
        event = self.application.execute(
            self.actor_a, CreateGoalEvent(self._goal_content())
        )
        plan = self._plan(count=1)
        queries = (
            (GetTrainingSession, session.id),
            (GetSessionAnnotation, annotation.id),
            (GetGoalEvent, event.id),
            (GetTrainingPlan, plan.id),
            (GetTrainingPlanHistory, plan.id),
        )
        malformed_ids: tuple[object, ...] = (
            None,
            [],
            "",
            "garmin:unavailable",
            "app:not-a-uuid",
        )
        for query_type, identifier in queries:
            with self.subTest(query=query_type.__name__, policy="cross-profile"):
                self.assertIsNone(
                    self.application.read(self.actor_b, query_type(identifier))
                )
            with self.subTest(query=query_type.__name__, policy="unavailable"):
                self.assertIsNone(
                    self.application.read(
                        self.actor_a, query_type("app:" + "0" * 32)
                    )
                )
            for malformed in malformed_ids:
                with self.subTest(query=query_type.__name__, malformed=malformed):
                    self.assertIsNone(
                        self.application.read(self.actor_a, query_type(malformed))
                    )

    def test_app_mutations_keep_stable_validation_not_found_and_read_only_errors(self) -> None:
        session = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        _, collected = self._collected_session(self.actor_a, "mutation-policy")
        annotation = self.application.execute(
            self.actor_a, CreateSessionAnnotation(collected.id, duplicate=True)
        )
        event = self.application.execute(
            self.actor_a, CreateGoalEvent(self._goal_content())
        )
        plan = self._plan(count=1)
        malformed_commands = (
            DeleteTrainingSession([], 1),
            DeleteSessionAnnotation([], 1),
            DeleteGoalEvent([], 1),
            DeleteTrainingPlan([], 1),
        )
        for command in malformed_commands:
            with self.subTest(command=type(command).__name__, policy="malformed"):
                with self.assertRaises(NotFound):
                    self.application.execute(self.actor_a, command)
        readonly_commands = (
            DeleteTrainingSession(collected.id, 1),
            DeleteSessionAnnotation(collected.id, 1),
            DeleteGoalEvent("garmin:unavailable", 1),
            DeleteTrainingPlan("garmin:unavailable", 1),
        )
        for command in readonly_commands:
            with self.subTest(command=type(command).__name__, policy="read-only"):
                with self.assertRaisesRegex(Conflict, "read-only"):
                    self.application.execute(self.actor_a, command)
        invalid_revision_commands = (
            DeleteTrainingSession(session.id, 0),
            DeleteSessionAnnotation(annotation.id, 0),
            DeleteGoalEvent(event.id, 0),
            DeleteTrainingPlan(plan.id, 0),
        )
        for command in invalid_revision_commands:
            with self.subTest(command=type(command).__name__, policy="invalid"):
                with self.assertRaises(InvalidRequest):
                    self.application.execute(self.actor_a, command)
        cross_profile_commands = (
            DeleteTrainingSession(session.id, 1),
            DeleteSessionAnnotation(annotation.id, 1),
            DeleteGoalEvent(event.id, 1),
            DeleteTrainingPlan(plan.id, 1),
        )
        for command in cross_profile_commands:
            with self.subTest(command=type(command).__name__, policy="cross-profile"):
                with self.assertRaises(NotFound):
                    self.application.execute(self.actor_b, command)
        self.assertIsNotNone(
            self.application.read(self.actor_a, GetTrainingSession(session.id))
        )
        self.assertIsNotNone(
            self.application.read(self.actor_a, GetSessionAnnotation(annotation.id))
        )
        self.assertIsNotNone(
            self.application.read(self.actor_a, GetGoalEvent(event.id))
        )
        self.assertIsNotNone(
            self.application.read(self.actor_a, GetTrainingPlan(plan.id))
        )

    def test_application_role_cannot_update_current_match_projection(self) -> None:
        session = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        plan = self._plan(count=1)
        planned_session_id = plan.content["planned_sessions"][0]["id"]
        fulfilled = self.application.execute(
            self.actor_a,
            SetPlannedSessionFulfilment(
                plan.id,
                plan.revision,
                planned_session_id,
                "fulfilled",
                "Projection update defence",
                (session.id,),
            ),
        )
        plan_id = UUID(hex=plan.id.split(":", 1)[1])
        with psycopg.connect(self.application_url) as connection:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    """
                    UPDATE planned_session_current_matches
                    SET position = position + 1 WHERE plan_id = %s
                    """,
                    (plan_id,),
                )
        reread = self.application.read(self.actor_a, GetTrainingPlan(plan.id))
        self.assertEqual(reread, fulfilled)

    def test_direct_app_role_rejects_invalid_create_revision_authority(self) -> None:
        session = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        session_id = UUID(hex=session.id.split(":", 1)[1])
        invalid_snapshots = (
            {"status": "active"},
            {"disposition": "skipped"},
            {"fulfilment_note": "Premature fulfilment note"},
            {"disposition": "fulfilled", "match_id": session_id},
        )
        with psycopg.connect(self.application_url) as connection:
            for invalid in invalid_snapshots:
                with self.subTest(invalid=invalid):
                    with self.assertRaises(psycopg.errors.CheckViolation):
                        with connection.transaction():
                            self._stage_direct_create(connection, **invalid)
        self.assertEqual(
            self.application.read(self.actor_a, ListTrainingPlans()).plans, ()
        )

    def test_direct_app_role_enforces_revision_date_range_inclusively(self) -> None:
        with psycopg.connect(self.application_url) as connection:
            with self.assertRaises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    self._stage_direct_create(
                        connection, scheduled_date="2026-08-21"
                    )
            for boundary in ("2026-07-20", "2026-08-20"):
                with self.subTest(boundary=boundary):
                    with connection.transaction():
                        self._stage_direct_create(
                            connection, scheduled_date=boundary
                        )
        plans = self.application.read(self.actor_a, ListTrainingPlans()).plans
        self.assertEqual(len(plans), 2)
        self.assertEqual(
            {plan.content["planned_sessions"][0]["scheduled_date"] for plan in plans},
            {"2026-07-20", "2026-08-20"},
        )

    def test_direct_app_role_fulfilment_correction_cannot_change_cancellation(self) -> None:
        scheduled = self._plan(count=1)
        scheduled_id = UUID(hex=scheduled.id.split(":", 1)[1])
        with psycopg.connect(self.application_url) as connection:
            with self.assertRaises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    self._stage_direct_adjustment(
                        connection,
                        scheduled_id,
                        1,
                        kind="fulfilment_correction",
                        disposition="cancelled",
                    )

        planned_session_id = scheduled.content["planned_sessions"][0]["id"]
        cancelled = self.application.execute(
            self.actor_a,
            AdjustTrainingPlan(
                scheduled.id,
                scheduled.revision,
                "Legitimate cancellation",
                "2026-07-20",
                ({"op": "cancel", "planned_session_id": planned_session_id},),
            ),
        )
        for change in (
            {"disposition": "scheduled"},
            {
                "mutate_note": True,
                "fulfilment_note": "Cancelled session tamper",
            },
        ):
            with self.subTest(change=change):
                with psycopg.connect(self.application_url) as connection:
                    with self.assertRaises(psycopg.errors.CheckViolation):
                        with connection.transaction():
                            self._stage_direct_adjustment(
                                connection,
                                scheduled_id,
                                cancelled.revision,
                                kind="fulfilment_correction",
                                **change,
                            )
        reread = self.application.read(
            self.actor_a, GetTrainingPlan(scheduled.id)
        )
        self.assertEqual(reread, cancelled)

    def test_direct_app_role_rejects_revision_gaps_and_adjustment_authority_changes(self) -> None:
        plan = self._plan(count=1)
        plan_id = UUID(hex=plan.id.split(":", 1)[1])
        with psycopg.connect(self.application_url) as connection:
            with self.assertRaises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO plan_revisions (
                            profile_id, plan_id, revision, kind, recorded_at,
                            recorded_date, reason, effective_from, name,
                            starts_on, ends_on, status
                        )
                        SELECT profile_id, plan_id, 0, 'create', recorded_at,
                               recorded_date, reason, NULL, name,
                               starts_on, ends_on, status
                        FROM plan_revisions
                        WHERE plan_id = %s AND revision = 1
                        """,
                        (plan_id,),
                    )
            with self.assertRaises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO plan_revisions (
                            profile_id, plan_id, revision, kind, recorded_at,
                            recorded_date, reason, effective_from, name,
                            starts_on, ends_on, status
                        )
                        SELECT profile_id, plan_id, 2, 'adjustment', %s,
                               %s, 'Gap probe', starts_on, name,
                               starts_on, ends_on, status
                        FROM plan_revisions
                        WHERE plan_id = %s AND revision = 1
                        """,
                        (self.now, self.now.date(), plan_id),
                    )
                    connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for change in (
                {"status": "active"},
                {
                    "disposition": "skipped",
                    "mutate_note": True,
                    "fulfilment_note": "Direct fulfilment mutation",
                },
            ):
                with self.subTest(change=change):
                    with self.assertRaises(psycopg.errors.CheckViolation):
                        with connection.transaction():
                            self._stage_direct_adjustment(
                                connection, plan_id, 1, **change
                            )
        reread = self.application.read(self.actor_a, GetTrainingPlan(plan.id))
        history = self.application.read(
            self.actor_a, GetTrainingPlanHistory(plan.id)
        )
        self.assertEqual(reread.revision, 1)
        self.assertEqual(len(history.revisions), 1)
        self.assertEqual(
            reread.content["planned_sessions"][0]["disposition"], "scheduled"
        )

    def test_direct_app_role_adjustment_cannot_reassign_fulfilment_match(self) -> None:
        first_session = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        second_session = self.application.execute(
            self.actor_a, CreateTrainingSession(self._session_content())
        )
        plan = self._plan(count=1)
        planned_session_id = plan.content["planned_sessions"][0]["id"]
        fulfilled = self.application.execute(
            self.actor_a,
            SetPlannedSessionFulfilment(
                plan.id,
                1,
                planned_session_id,
                "fulfilled",
                "Legitimate fulfilment",
                (first_session.id,),
            ),
        )
        plan_id = UUID(hex=plan.id.split(":", 1)[1])
        second_session_id = UUID(hex=second_session.id.split(":", 1)[1])
        with psycopg.connect(self.application_url) as connection:
            with self.assertRaises(psycopg.errors.CheckViolation):
                with connection.transaction():
                    self._stage_direct_adjustment(
                        connection,
                        plan_id,
                        2,
                        replacement_match=second_session_id,
                    )
        reread = self.application.read(self.actor_a, GetTrainingPlan(plan.id))
        history = self.application.read(
            self.actor_a, GetTrainingPlanHistory(plan.id)
        )
        self.assertEqual(reread.revision, 2)
        self.assertEqual(len(history.revisions), 2)
        self.assertEqual(
            reread.content["planned_sessions"][0]["matches"],
            [first_session.id],
        )
        self.assertEqual(fulfilled, reread)

    def test_invalid_multi_child_reference_rolls_back_entire_plan(self) -> None:
        event = self.application.execute(
            self.actor_a, CreateGoalEvent(self._goal_content())
        )
        with self.assertRaises(Conflict):
            self.application.execute(
                self.actor_b,
                CreateTrainingPlan(
                    "Cross-profile plan",
                    "2026-07-20",
                    "2026-08-20",
                    "Must roll back",
                    goal_events=(
                        {"goal_event_id": event.id, "goal_event_revision": 1},
                    ),
                    planned_sessions=(
                        {
                            "scheduled_date": "2026-07-21",
                            "sport": "running",
                            "prescription": "Would otherwise be valid",
                        },
                    ),
                ),
            )
        self.assertEqual(
            self.application.read(self.actor_b, ListTrainingPlans()).plans, ()
        )

    def test_database_denies_cross_profile_annotation_reference(self) -> None:
        _, collected = self._collected_session(self.actor_a, "raw-cross-profile")
        session_id = UUID(hex=collected.id.split(":", 1)[1])
        with psycopg.connect(self.migration_url) as connection:
            profile_b = connection.execute(
                """
                SELECT id FROM profiles
                WHERE clerk_issuer = %s AND clerk_subject = %s
                """,
                ("https://identity.example.test", "app-record-b"),
            ).fetchone()[0]
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO session_annotations (
                            profile_id, id, training_session_id,
                            notes, reliability, duplicate_flag
                        ) VALUES (%s, %s, %s, 'cross profile', NULL, NULL)
                        """,
                        (profile_b, UUID(int=0), session_id),
                    )

    def test_application_role_cannot_mutate_immutable_plan_history(self) -> None:
        plan = self._plan()
        with psycopg.connect(self.application_url) as connection:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute(
                    """
                    UPDATE plan_revisions SET reason = 'tampered'
                    WHERE plan_id = %s
                    """,
                    (UUID(hex=plan.id.split(":", 1)[1]),),
                )


if __name__ == "__main__":
    unittest.main()
