"""Run all 85 portable App Record contracts through CoachApplication/PostgreSQL."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import ClassVar

import psycopg

from coach.data import ContextWindow, ReadOnlyRecord
from coach.postgres import DatabaseSettings
from coach.postgres.migrate import migrate
from tests import test_app_sessions as session_contracts
from tests import test_goal_events as goal_contracts
from tests import test_training_plans as plan_contracts
from tests.postgres.app_compat import CoachApplicationCompat
from tests.postgres.support import test_database

_DATABASE = None


def setUpModule() -> None:
    global _DATABASE
    _DATABASE = test_database()
    migrate(DatabaseSettings.from_url(_DATABASE.migration_url))
    CoachApplicationCompat.configure(_DATABASE.application_url)


class _PostgreSQLContractMixin:
    contract_module: ClassVar[ModuleType]

    def setUp(self) -> None:
        if _DATABASE is None:
            raise RuntimeError("PostgreSQL contract module is not initialized")
        with psycopg.connect(_DATABASE.migration_url) as connection:
            connection.execute("TRUNCATE TABLE profiles CASCADE")
        self._original_coach_data = self.contract_module.CoachData
        self.contract_module.CoachData = CoachApplicationCompat

    def tearDown(self) -> None:
        self.contract_module.CoachData = self._original_coach_data


class PostgreSQLCreateSessionContract(
    _PostgreSQLContractMixin, session_contracts.CreateSessionTest
):
    contract_module = session_contracts

    def test_create_date_only_session_projects_app_record_with_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachApplicationCompat(Path(tmp))
            created = coach.create_session(session_contracts._boulder_content())

            self.assertTrue(created["id"].startswith("app:"))
            self.assertEqual(created["origin"], "app_record")
            self.assertEqual(created["provenance"], {"source": "manual"})
            self.assertEqual(created["revision"], 1)
            self.assertEqual(created["sport"], "bouldering")
            self.assertEqual(created["local_date"], "2026-07-18")
            self.assertIsNone(created["local_start"])
            self.assertEqual(created["timing_precision"], "date_only")
            self.assertEqual(created["session_rpe"], 7)
            self.assertEqual(
                created["duration"],
                {"value": 5400, "unit": "seconds", "basis": "active"},
            )
            self.assertIsNone(created["distance"])
            self.assertIsNone(created["loads"])
            self.assertEqual(created["created_at"], created["updated_at"])
            self.assertIsNone(created["source_detail"])
            self.assertEqual(coach.get_session(created["id"]), created)


class PostgreSQLSessionValidationContract(
    _PostgreSQLContractMixin, session_contracts.ValidationTest
):
    contract_module = session_contracts


class PostgreSQLReplaceDeleteSessionContract(
    _PostgreSQLContractMixin, session_contracts.ReplaceDeleteTest
):
    contract_module = session_contracts


class PostgreSQLCollectedSessionContract(
    _PostgreSQLContractMixin, session_contracts.GarminReadOnlyTest
):
    contract_module = session_contracts

    def test_garmin_session_is_readable_but_not_mutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachApplicationCompat(Path(tmp))
            session = coach.ingest_collected_session(
                local_date=date(2026, 7, 18),
                sport="running",
                source_key="portable-read-only",
            )
            self.assertEqual(session["origin"], "collected_record")
            self.assertEqual(session["sport"], "running")
            with self.assertRaises(ReadOnlyRecord):
                coach.replace_session(
                    session["id"],
                    1,
                    session_contracts._boulder_content(sport="running"),
                )
            with self.assertRaises(ReadOnlyRecord):
                coach.delete_session(session["id"], 1)

    def test_list_unifies_garmin_and_app_sessions_and_filters_by_sport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachApplicationCompat(Path(tmp))
            collected = coach.ingest_collected_session(
                local_date=date(2026, 7, 18),
                sport="running",
                source_key="portable-list",
            )
            created = coach.create_session(session_contracts._boulder_content())
            window = ContextWindow(days=7, end_date=date(2026, 7, 18))
            self.assertEqual(
                {item["id"] for item in coach.list_sessions(window)},
                {collected["id"], created["id"]},
            )
            self.assertEqual(
                [item["id"] for item in coach.list_sessions(window, sport="bouldering")],
                [created["id"]],
            )


class PostgreSQLCreateGoalContract(
    _PostgreSQLContractMixin, goal_contracts.CreateGoalEventTest
):
    contract_module = goal_contracts

    def test_create_date_only_event_projects_app_record_with_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachApplicationCompat(Path(tmp))
            created = coach.create_goal_event(goal_contracts._marathon_content())

            self.assertTrue(created["id"].startswith("app:"))
            self.assertEqual(created["origin"], "app_record")
            self.assertEqual(created["provenance"], {"source": "manual"})
            self.assertEqual(created["revision"], 1)
            self.assertEqual(created["name"], "Autumn City Marathon")
            self.assertEqual(created["sport"], "running")
            self.assertEqual(created["local_date"], "2026-10-11")
            self.assertIsNone(created["local_start"])
            self.assertEqual(created["timing_precision"], "date_only")
            self.assertEqual(created["priority"], "primary")
            self.assertEqual(created["status"], "scheduled")
            self.assertIsNone(created["distance"])
            self.assertIsNone(created["goal"])
            self.assertIsNone(created["outcome"])
            self.assertEqual(created["created_at"], created["updated_at"])
            self.assertEqual(coach.get_goal_event(created["id"]), created)


class PostgreSQLGoalValidationContract(
    _PostgreSQLContractMixin, goal_contracts.ValidationTest
):
    contract_module = goal_contracts


class PostgreSQLReplaceDeleteGoalContract(
    _PostgreSQLContractMixin, goal_contracts.ReplaceDeleteTest
):
    contract_module = goal_contracts


class PostgreSQLListGoalContract(
    _PostgreSQLContractMixin, goal_contracts.ListGoalEventsTest
):
    contract_module = goal_contracts


class PostgreSQLReadOnlyGoalContract(
    _PostgreSQLContractMixin, goal_contracts.GarminReadOnlyTest
):
    contract_module = goal_contracts


class PostgreSQLCreatePlanContract(
    _PostgreSQLContractMixin, plan_contracts.CreatePlanTest
):
    contract_module = plan_contracts

    def test_create_projects_draft_head_with_identity(self) -> None:
        coach, _ = plan_contracts._coach()
        created = plan_contracts._base_plan(
            coach,
            constraints=["No sessions on Mondays"],
            planned_sessions=[
                {
                    "scheduled_date": plan_contracts._d(3),
                    "sport": "running",
                    "prescription": "Easy 5k",
                }
            ],
        )

        self.assertTrue(created["id"].startswith("app:"))
        self.assertEqual(created["origin"], "app_record")
        self.assertEqual(created["provenance"], {"source": "manual"})
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["name"], "Base build")
        self.assertEqual(created["status"], "draft")
        self.assertEqual(created["constraints"], ["No sessions on Mondays"])
        self.assertEqual(created["goal_events"], [])
        self.assertFalse(created["requires_review"])
        self.assertEqual(created["created_at"], created["updated_at"])
        self.assertEqual(len(created["planned_sessions"]), 1)
        planned = created["planned_sessions"][0]
        self.assertTrue(planned["id"])
        self.assertEqual(planned["disposition"], "scheduled")
        self.assertEqual(planned["matches"], [])
        self.assertIsNone(planned["fulfilment_note"])
        self.assertEqual(planned["prescription"], "Easy 5k")
        self.assertEqual(coach.get_plan(created["id"]), created)


class PostgreSQLGetListHistoryPlanContract(
    _PostgreSQLContractMixin, plan_contracts.GetListHistoryTest
):
    contract_module = plan_contracts


class PostgreSQLPlanLifecycleContract(
    _PostgreSQLContractMixin, plan_contracts.ActivateArchiveDeleteTest
):
    contract_module = plan_contracts

    def test_delete_only_removes_never_active_draft_without_fulfilment(self) -> None:
        coach, _ = plan_contracts._coach()
        draft = plan_contracts._base_plan(coach)
        result = coach.delete_plan(draft["id"], draft["revision"])
        self.assertTrue(result["deleted"])
        with self.assertRaises(plan_contracts.NotFound):
            coach.get_plan(draft["id"])


class PostgreSQLAdjustPlanContract(
    _PostgreSQLContractMixin, plan_contracts.AdjustPlanTest
):
    contract_module = plan_contracts


class PostgreSQLFulfilmentPlanContract(
    _PostgreSQLContractMixin, plan_contracts.FulfilmentMatchingTest
):
    contract_module = plan_contracts


class PostgreSQLGoalLinkagePlanContract(
    _PostgreSQLContractMixin, plan_contracts.GoalLinkageTest
):
    contract_module = plan_contracts


class PostgreSQLReferentialPlanContract(
    _PostgreSQLContractMixin, plan_contracts.ReferentialIntegrityTest
):
    contract_module = plan_contracts


class PostgreSQLReviewHardeningPlanContract(
    _PostgreSQLContractMixin, plan_contracts.ReviewHardeningTest
):
    contract_module = plan_contracts

    def test_matched_garmin_session_reports_read_only_not_referenced(self) -> None:
        coach, _ = plan_contracts._coach()
        collected = coach.ingest_collected_session(
            local_date=date.fromisoformat(plan_contracts._d(3)),
            sport="running",
            source_key="portable-plan-read-only",
        )
        created = plan_contracts._base_plan(
            coach,
            planned_sessions=[
                {
                    "scheduled_date": plan_contracts._d(3),
                    "sport": "running",
                    "prescription": "run",
                }
            ],
        )
        planned_session_id = created["planned_sessions"][0]["id"]
        coach.set_planned_session_fulfilment(
            created["id"],
            created["revision"],
            planned_session_id,
            "fulfilled",
            "source recorded it",
            [collected["id"]],
        )
        with self.assertRaises(ReadOnlyRecord):
            coach.delete_session(collected["id"], 1)


if __name__ == "__main__":
    unittest.main()
