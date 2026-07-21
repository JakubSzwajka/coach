from __future__ import annotations

import unittest
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import psycopg

from coach.application import (
    ActivateTrainingPlan,
    AdjustTrainingPlan,
    ArchiveTrainingPlan,
    CalendarGoalEventView,
    CalendarPlannedSessionView,
    CalendarTrainingSessionView,
    CapturePointer,
    ClerkActor,
    CoachApplication,
    CollectedBatch,
    CollectedCapture,
    CollectedTrainingSession,
    ControlledObservation,
    CreateGoalEvent,
    CreateSessionAnnotation,
    CreateTrainingPlan,
    CreateTrainingSession,
    EnsureProfile,
    EnsureSourceConnection,
    GetCoachingContext,
    GetCollectionHealth,
    GetDashboard,
    GetTrainingPlanHistory,
    GetTrends,
    GetUnifiedCalendar,
    InvalidRequest,
    ListGoalEvents,
    ListSessionAnnotations,
    ListTrainingPlans,
    ListTrainingSessions,
    ReplaceGoalEvent,
    SetPlannedSessionFulfilment,
)
from coach.postgres import DatabaseSettings
from coach.postgres.migrate import migrate
from tests.postgres.support import test_database


class ReadProjectionsPostgreSQLTest(unittest.TestCase):
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
        self.actor_a = ClerkActor("https://identity.example.test", "projection-a")
        self.actor_b = ClerkActor("https://identity.example.test", "projection-b")
        self.profile_a = self.application.execute(
            self.actor_a, EnsureProfile("Synthetic Projection A")
        ).profile
        self.profile_b = self.application.execute(
            self.actor_b, EnsureProfile("Synthetic Projection B")
        ).profile

    def _manual_session(self, title: str, sport: str = "running"):
        return self.application.execute(
            self.actor_a,
            CreateTrainingSession(
                {
                    "local_date": "2026-07-21",
                    "sport": sport,
                    "title": title,
                    "duration": None,
                    "distance": None,
                    "loads": None,
                }
            ),
        )

    def _collected_graph(self):
        source = self.application.execute(
            self.actor_a,
            EnsureSourceConnection("synthetic-provider", "projection-source"),
        )
        first = CapturePointer("training-session", "overlap-first")
        second = CapturePointer("training-session", "overlap-second")
        self.application.ingest(
            self.profile_a,
            CollectedBatch(
                source=source,
                idempotency_key="projection-overlap",
                captures=(
                    CollectedCapture(first.record_kind, first.source_key, {"private": 1}),
                    CollectedCapture(second.record_kind, second.source_key, {"private": 2}),
                ),
                sessions=(
                    CollectedTrainingSession(
                        capture=first,
                        local_date=date(2026, 7, 21),
                        local_start=datetime(2026, 7, 21, 7),
                        timing_precision="local_datetime",
                        sport="running",
                        title="Synthetic collected run",
                    ),
                    CollectedTrainingSession(
                        capture=second,
                        local_date=date(2026, 7, 21),
                        sport="cycling",
                    ),
                ),
                observations=(
                    ControlledObservation(
                        capture=first,
                        definition="daily_resting_heart_rate",
                        value_type="integer",
                        unit="beats_per_minute",
                        window_kind="calendar_day",
                        method="source_reported",
                        status="observed",
                        value=48,
                        local_date=date(2026, 7, 21),
                        provenance={"private_provider_key": "must-not-project"},
                    ),
                    ControlledObservation(
                        capture=second,
                        definition="daily_hrv_status",
                        value_type="text",
                        unit="status",
                        window_kind="calendar_day",
                        method="source_reported",
                        status="missing",
                        local_date=date(2026, 7, 21),
                    ),
                ),
            ),
        )
        listed = self.application.read(
            self.actor_a,
            ListTrainingSessions(date(2026, 7, 20), date(2026, 7, 25)),
        )
        collected = [item for item in listed.sessions if item.ownership == "collected"]
        self.assertEqual(len(collected), 2)
        return source, collected

    def _successful_collection_health(self, source) -> None:
        profile_id = self.profile_a._id
        source_id = source._id
        job_id = uuid4()
        started = self.now - timedelta(minutes=2)
        finished = self.now - timedelta(minutes=1)
        with psycopg.connect(self.migration_url) as connection:
            connection.execute(
                """
                INSERT INTO collection_jobs (
                    profile_id, id, source_connection_id, request_key, kind,
                    state, revision, diagnostics, created_at, started_at, finished_at
                ) VALUES (%s, %s, %s, 'projection-health', 'incremental',
                          'succeeded', 2, '{}'::jsonb, %s, %s, %s)
                """,
                (profile_id, job_id, source_id, started, started, finished),
            )
            connection.execute(
                """
                INSERT INTO collection_health (
                    profile_id, source_connection_id, domain, collection_job_id,
                    outcome, attempted_at, finished_at, last_success_at, diagnostics
                ) VALUES (%s, %s, 'incremental', %s, 'successful', %s, %s, %s,
                          '{}'::jsonb)
                """,
                (profile_id, source_id, job_id, started, finished, finished),
            )
            connection.execute(
                """
                INSERT INTO collection_checkpoints (
                    profile_id, source_connection_id, domain, cursor,
                    revision, updated_at, last_success_at
                ) VALUES (%s, %s, 'incremental', '{}'::jsonb, 3, %s, %s)
                """,
                (profile_id, source_id, finished, finished),
            )

    def _plans_and_stale_goal(self, matches):
        event = self.application.execute(
            self.actor_a,
            CreateGoalEvent(
                {
                    "name": "Synthetic Goal",
                    "sport": "running",
                    "local_date": "2026-07-24",
                    "priority": "primary",
                }
            ),
        )
        archived = self.application.execute(
            self.actor_a,
            CreateTrainingPlan(
                "Archived synthetic plan",
                "2026-07-20",
                "2026-07-30",
                "Create archived fixture",
                goal_events=(
                    {
                        "goal_event_id": event.id,
                        "goal_event_revision": event.revision,
                    },
                ),
                planned_sessions=(
                    {
                        "scheduled_date": "2026-07-21",
                        "sport": "running",
                        "prescription": "Fulfilled overlap",
                    },
                    {
                        "scheduled_date": "2026-07-22",
                        "sport": "running",
                        "prescription": "Skipped session",
                    },
                    {
                        "scheduled_date": "2026-07-23",
                        "sport": "running",
                        "prescription": "Scheduled session",
                    },
                    {
                        "scheduled_date": "2026-07-24",
                        "sport": "running",
                        "prescription": "Cancelled session",
                    },
                ),
            ),
        )
        planned = archived.content["planned_sessions"]
        archived = self.application.execute(
            self.actor_a,
            SetPlannedSessionFulfilment(
                archived.id,
                archived.revision,
                planned[0]["id"],
                "fulfilled",
                "Explicit one-to-many match",
                tuple(item.id for item in matches),
            ),
        )
        archived = self.application.execute(
            self.actor_a,
            SetPlannedSessionFulfilment(
                archived.id,
                archived.revision,
                planned[1]["id"],
                "skipped",
                "Explicit skip",
            ),
        )
        archived = self.application.execute(
            self.actor_a,
            AdjustTrainingPlan(
                archived.id,
                archived.revision,
                "Explicit cancellation",
                "2026-07-20",
                ({"op": "cancel", "planned_session_id": planned[3]["id"]},),
            ),
        )
        archived = self.application.execute(
            self.actor_a,
            ActivateTrainingPlan(archived.id, archived.revision, "Activate fixture"),
        )
        archived = self.application.execute(
            self.actor_a,
            ArchiveTrainingPlan(archived.id, archived.revision, "Archive fixture"),
        )

        active = self.application.execute(
            self.actor_a,
            CreateTrainingPlan(
                "Active synthetic plan",
                "2026-07-20",
                "2026-07-30",
                "Create active fixture",
                planned_sessions=(),
            ),
        )
        active = self.application.execute(
            self.actor_a, ActivateTrainingPlan(active.id, active.revision, "Activate")
        )
        draft = self.application.execute(
            self.actor_a,
            CreateTrainingPlan(
                "Draft synthetic plan",
                "2026-07-20",
                "2026-07-30",
                "Create draft fixture",
                planned_sessions=(),
            ),
        )
        event = self.application.execute(
            self.actor_a,
            ReplaceGoalEvent(
                event.id,
                event.revision,
                {
                    "name": "Synthetic Goal Advanced",
                    "sport": "running",
                    "local_date": "2026-07-24",
                    "priority": "secondary",
                },
            ),
        )
        return event, archived, active, draft

    def test_consumer_reads_preserve_unknowns_authority_and_safe_stable_dtos(self) -> None:
        source, collected = self._collected_graph()
        manual_a = self._manual_session("Synthetic manual one")
        manual_b = self._manual_session("Synthetic manual two", "strength")
        annotation = self.application.execute(
            self.actor_a,
            CreateSessionAnnotation(
                collected[0].id,
                notes="Synthetic review",
                reliability="unreliable",
                duplicate=False,
            ),
        )
        event, archived, active, draft = self._plans_and_stale_goal(
            (manual_a, manual_b)
        )
        self._successful_collection_health(source)
        window = (date(2026, 7, 20), date(2026, 7, 25))

        dashboard = self.application.read(self.actor_a, GetDashboard(*window))
        trends = self.application.read(self.actor_a, GetTrends(*window))
        context = self.application.read(self.actor_a, GetCoachingContext(*window))
        health = self.application.read(self.actor_a, GetCollectionHealth())
        calendar = self.application.read(self.actor_a, GetUnifiedCalendar(*window))

        self.assertEqual(dashboard.display_name, "Synthetic Projection A")
        self.assertEqual(len(dashboard.recent_sessions), 4)
        self.assertEqual(dashboard.active_plan.id, active.id)
        latest = {item.definition: item for item in dashboard.latest_observations}
        self.assertEqual(latest["daily_resting_heart_rate"].value, 48)
        self.assertEqual(latest["daily_hrv_status"].status, "missing")
        self.assertIsNone(latest["daily_hrv_status"].value)
        self.assertEqual(
            [series.definition for series in trends.series],
            ["daily_hrv_status", "daily_resting_heart_rate"],
        )
        self.assertEqual(len(context.sessions), 4)
        self.assertEqual({plan.content["status"] for plan in context.plans},
                         {"draft", "active", "archived"})
        self.assertEqual(health.records, 2)
        self.assertEqual(health.captures, 2)
        self.assertEqual(health.latest_training_session_date, date(2026, 7, 21))
        self.assertEqual(health.latest_observation_date, date(2026, 7, 21))
        domains = {item.domain: item for item in health.sources[0].domains}
        self.assertEqual(domains["initial_sync"].outcome, "not_attempted")
        self.assertEqual(domains["incremental"].outcome, "successful")
        self.assertEqual(domains["incremental"].checkpoint_revision, 3)

        training = [
            item for item in calendar.items
            if isinstance(item, CalendarTrainingSessionView)
        ]
        planned = [
            item for item in calendar.items
            if isinstance(item, CalendarPlannedSessionView)
        ]
        goals = [
            item for item in calendar.items if isinstance(item, CalendarGoalEventView)
        ]
        self.assertEqual(len(training), 4)
        self.assertEqual(
            {item.disposition for item in planned},
            {"scheduled", "fulfilled", "skipped", "cancelled"},
        )
        fulfilled = next(item for item in planned if item.disposition == "fulfilled")
        self.assertEqual(
            {item.training_session_id for item in fulfilled.matches},
            {manual_a.id, manual_b.id},
        )
        self.assertEqual(fulfilled.plan_status, "archived")
        self.assertEqual(fulfilled.plan_revision, archived.revision)
        self.assertEqual(len(next(item for item in training if item.id == manual_a.id).plan_matches), 1)
        annotated = next(item for item in training if item.id == collected[0].id)
        self.assertEqual(annotated.annotation.id, annotation.id)
        self.assertTrue(goals[0].requires_review)
        self.assertEqual(goals[0].revision, event.revision)
        self.assertEqual(goals[0].plan_references[0].stale_reason, "revision_advanced")
        self.assertTrue(
            any(item.plan_requires_review for item in planned if item.plan_id == archived.id)
        )

        self.assertEqual(
            self.application.read(
                self.actor_a, ListSessionAnnotations()
            ).annotations,
            self.application.read(
                self.actor_a, ListSessionAnnotations(collected[0].id)
            ).annotations,
        )
        self.assertGreater(
            len(self.application.read(
                self.actor_a, GetTrainingPlanHistory(archived.id)
            ).revisions),
            1,
        )
        self.assertEqual(
            {item.content["status"] for item in self.application.read(
                self.actor_a, ListTrainingPlans()
            ).plans},
            {"draft", "active", "archived"},
        )
        self.assertEqual(
            [item.id for item in self.application.read(
                self.actor_a, ListGoalEvents()
            ).events],
            [event.id],
        )

        rendered = repr(asdict(context)) + repr(asdict(calendar))
        self.assertNotIn("private_provider_key", rendered)
        self.assertNotIn("must-not-project", rendered)
        self.assertNotIn("projection-a", rendered)
        self.assertNotIn("overlap-first", rendered)
        self.assertNotIn("/", rendered)

    def test_projection_windows_and_two_profiles_are_isolated(self) -> None:
        self._collected_graph()
        foreign = self.application.execute(
            self.actor_b,
            CreateTrainingSession(
                {
                    "local_date": "2026-07-21",
                    "sport": "rowing",
                    "title": "Foreign synthetic session",
                }
            ),
        )
        window = (date(2026, 7, 20), date(2026, 7, 25))

        calendar_a = self.application.read(self.actor_a, GetUnifiedCalendar(*window))
        calendar_b = self.application.read(self.actor_b, GetUnifiedCalendar(*window))
        ids_a = {item.id for item in calendar_a.items}
        ids_b = {item.id for item in calendar_b.items}
        self.assertNotIn(foreign.id, ids_a)
        self.assertEqual(ids_b, {foreign.id})
        self.assertEqual(
            self.application.read(self.actor_b, GetCollectionHealth()).captures, 0
        )
        self.assertIsNone(
            self.application.read(
                ClerkActor("https://identity.example.test", "unbound"),
                GetDashboard(*window),
            )
        )
        with self.assertRaises(InvalidRequest):
            self.application.read(
                self.actor_a,
                GetUnifiedCalendar(date(2026, 1, 1), date(2026, 4, 1)),
            )


if __name__ == "__main__":
    unittest.main()
