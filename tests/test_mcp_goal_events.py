from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.legacy_mcp import legacy_mcp_adapter

from coach.data import InvalidRecord, ReadOnlyRecord, RevisionConflict
from coach.mcp_server import (
    create_goal_event,
    delete_goal_event,
    get_goal_event,
    list_goal_events,
    replace_goal_event,
)


class McpGoalEventToolsTest(unittest.TestCase):
    def test_full_lifecycle_through_configured_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with legacy_mcp_adapter(tmp):
                created = create_goal_event(
                    name="Autumn City Marathon",
                    local_date="2026-10-11",
                    sport="running",
                    priority="primary",
                    distance={"value": 42195, "unit": "meters"},
                    goal={
                        "target_duration": {"value": 10800, "unit": "seconds"},
                        "statement": "Sub-3 hours",
                    },
                )
                self.assertEqual(created["origin"], "app_record")
                self.assertEqual(created["revision"], 1)
                self.assertEqual(created["status"], "scheduled")
                self.assertEqual(created["priority"], "primary")
                event_id = created["id"]

                listed = list_goal_events()
                self.assertIn(event_id, {e["id"] for e in listed["goal_events"]})

                fetched = get_goal_event(event_id)
                self.assertEqual(fetched["id"], event_id)
                self.assertEqual(fetched["name"], "Autumn City Marathon")

                replaced = replace_goal_event(
                    goal_event_id=event_id,
                    expected_revision=1,
                    name="Autumn City Marathon",
                    local_date="2026-10-11",
                    sport="running",
                    priority="secondary",
                    status="completed",
                    outcome={
                        "actual_duration": {"value": 10950, "unit": "seconds"}
                    },
                )
                self.assertEqual(replaced["revision"], 2)
                self.assertEqual(replaced["priority"], "secondary")
                self.assertEqual(replaced["status"], "completed")
                self.assertEqual(
                    replaced["outcome"],
                    {
                        "actual_duration": {"value": 10950, "unit": "seconds"},
                        "statement": None,
                    },
                )

                with self.assertRaises(RevisionConflict):
                    replace_goal_event(
                        goal_event_id=event_id,
                        expected_revision=1,
                        name="Autumn City Marathon",
                        local_date="2026-10-11",
                        sport="running",
                        priority="primary",
                    )

                deleted = delete_goal_event(event_id, expected_revision=2)
                self.assertTrue(deleted["deleted"])
                stem = event_id.split(":", 1)[1]
                self.assertFalse(
                    (Path(tmp) / "app" / "goal_events" / f"{stem}.json").exists()
                )

    def test_invalid_priority_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with legacy_mcp_adapter(tmp):
                with self.assertRaises(InvalidRecord):
                    create_goal_event(
                        name="Club B race",
                        local_date="2026-05-01",
                        sport="running",
                        priority="B",
                    )

    def test_garmin_ids_cannot_be_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with legacy_mcp_adapter(tmp):
                with self.assertRaises(ReadOnlyRecord):
                    replace_goal_event(
                        goal_event_id="garmin:123",
                        expected_revision=1,
                        name="x",
                        local_date="2026-10-11",
                        sport="running",
                        priority="primary",
                    )
                with self.assertRaises(ReadOnlyRecord):
                    delete_goal_event("garmin:123", expected_revision=1)


if __name__ == "__main__":
    unittest.main()
