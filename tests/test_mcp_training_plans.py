from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests.legacy_mcp import legacy_mcp_adapter

from coach.data import InvalidRecord, ReferencedRecord, RevisionConflict
from coach.mcp_server import (
    activate_training_plan,
    adjust_training_plan,
    archive_training_plan,
    create_goal_event,
    create_training_plan,
    create_training_session,
    delete_goal_event,
    delete_training_plan,
    get_training_plan,
    get_training_plan_history,
    list_training_plans,
    set_planned_session_fulfilment,
)

TODAY = date.today()


def _d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


class McpTrainingPlanToolsTest(unittest.TestCase):
    def test_full_plan_lifecycle_through_configured_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with legacy_mcp_adapter(tmp):
                created = create_training_plan(
                    name="Autumn build",
                    starts_on=_d(0),
                    ends_on=_d(90),
                    reason="Season kickoff",
                    constraints=["No doubles on weekdays"],
                    planned_sessions=[
                        {
                            "scheduled_date": _d(2),
                            "sport": "running",
                            "prescription": "Easy 5k",
                        },
                        {
                            "scheduled_date": _d(40),
                            "sport": "running",
                            "prescription": "Long run 20k",
                        },
                    ],
                )
                self.assertEqual(created["status"], "draft")
                self.assertEqual(created["revision"], 1)
                plan_id = created["id"]
                late_id = next(
                    p["id"]
                    for p in created["planned_sessions"]
                    if p["scheduled_date"] == _d(40)
                )

                listed = list_training_plans()
                self.assertEqual(
                    {p["id"] for p in listed["plans"]}, {plan_id}
                )

                activated = activate_training_plan(plan_id, 1, "Ready")
                self.assertEqual(activated["status"], "active")
                self.assertEqual(activated["revision"], 2)

                adjusted = adjust_training_plan(
                    plan_id,
                    2,
                    "Push long run out a week",
                    _d(1),
                    [
                        {
                            "op": "update",
                            "planned_session_id": late_id,
                            "scheduled_date": _d(47),
                            "sport": "running",
                            "prescription": "Long run 22k",
                        }
                    ],
                )
                self.assertEqual(adjusted["revision"], 3)
                moved = next(
                    p for p in adjusted["planned_sessions"] if p["id"] == late_id
                )
                self.assertEqual(moved["scheduled_date"], _d(47))

                history = get_training_plan_history(plan_id)
                self.assertEqual(
                    [r["kind"] for r in history["revisions"]],
                    ["create", "lifecycle", "adjustment"],
                )

                fetched = get_training_plan(plan_id)
                self.assertEqual(fetched["revision"], 3)

    def test_matching_and_frozen_history_through_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with legacy_mcp_adapter(tmp):
                plan = create_training_plan(
                    name="Match plan",
                    starts_on=_d(0),
                    ends_on=_d(60),
                    reason="init",
                    planned_sessions=[
                        {
                            "scheduled_date": _d(1),
                            "sport": "running",
                            "prescription": "Interval day",
                        }
                    ],
                )
                plan_id = plan["id"]
                ps_id = plan["planned_sessions"][0]["id"]

                session = create_training_session(
                    sport="running", local_date=_d(1)
                )

                fulfilled = set_planned_session_fulfilment(
                    plan_id,
                    1,
                    ps_id,
                    "fulfilled",
                    "Completed as prescribed",
                    matches=[session["id"]],
                )
                planned = fulfilled["planned_sessions"][0]
                self.assertEqual(planned["disposition"], "fulfilled")
                self.assertEqual(planned["matches"], [session["id"]])

                # A future-effective adjustment cannot touch the past prescription.
                with self.assertRaises(InvalidRecord):
                    adjust_training_plan(
                        plan_id,
                        fulfilled["revision"],
                        "rewrite the past",
                        _d(30),
                        [
                            {
                                "op": "update",
                                "planned_session_id": ps_id,
                                "scheduled_date": _d(1),
                                "sport": "running",
                                "prescription": "changed",
                            }
                        ],
                    )

    def test_second_active_plan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with legacy_mcp_adapter(tmp):
                first = create_training_plan(
                    name="First", starts_on=_d(0), ends_on=_d(30), reason="a"
                )
                second = create_training_plan(
                    name="Second", starts_on=_d(0), ends_on=_d(30), reason="b"
                )
                activate_training_plan(first["id"], 1, "go")
                with self.assertRaises(InvalidRecord):
                    activate_training_plan(second["id"], 1, "go too")

    def test_referenced_goal_event_cannot_be_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with legacy_mcp_adapter(tmp):
                event = create_goal_event(
                    name="Target race",
                    local_date=_d(80),
                    sport="running",
                    priority="primary",
                )
                create_training_plan(
                    name="Goal plan",
                    starts_on=_d(0),
                    ends_on=_d(80),
                    reason="init",
                    goal_events=[
                        {
                            "goal_event_id": event["id"],
                            "goal_event_revision": event["revision"],
                        }
                    ],
                )
                with self.assertRaises(ReferencedRecord):
                    delete_goal_event(event["id"], event["revision"])

    def test_stale_revision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with legacy_mcp_adapter(tmp):
                created = create_training_plan(
                    name="Rev", starts_on=_d(0), ends_on=_d(30), reason="init"
                )
                with self.assertRaises(RevisionConflict):
                    delete_training_plan(created["id"], 99)


if __name__ == "__main__":
    unittest.main()
