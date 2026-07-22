from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@unittest.skip("superseded by disposable-PostgreSQL MCP process integration")
class McpStdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_client_discovers_and_invokes_read_only_context_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "derived" / "athlete.json",
                {"snapshot_date": "2026-07-18", "full_name": "Synthetic Athlete"},
            )
            self._write_json(
                root / "derived" / "daily" / "2026-07-18.json",
                {"date": "2026-07-18", "training_readiness": None},
            )
            self._write_json(
                root / "derived" / "activities.json",
                [
                    {
                        "activity_id": "synthetic-session",
                        "name": "Synthetic Run",
                        "type": "running",
                        "start_local": "2026-07-18 07:30:00",
                        "distance_m": 5000,
                        "duration_s": 1800,
                        "avg_hr": None,
                        "max_hr": 165,
                    }
                ],
            )
            self._write_json(
                root / "index" / "collector-health.json",
                {
                    "schema_version": 1,
                    "run": {
                        "outcome": "successful",
                        "attempted_at": "2026-07-18T08:00:00Z",
                        "finished_at": "2026-07-18T08:01:00Z",
                        "last_success_at": "2026-07-18T08:01:00Z",
                    },
                    "domains": {
                        "daily": {
                            "outcome": "successful",
                            "last_success_at": "2026-07-18T08:01:00Z",
                        },
                        "activities": {
                            "outcome": "successful",
                            "last_success_at": "2026-07-18T08:01:00Z",
                        },
                        "plans": {"outcome": "not_attempted"},
                        "profile": {"outcome": "not_attempted"},
                    },
                },
            )

            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "coach.mcp_server"],
                cwd=Path(__file__).resolve().parents[1],
                env={"GARMIN_COACH_DATA_DIR": str(root)},
            )
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool(
                        "read_coaching_context",
                        {"days": 1, "end_date": "2026-07-18"},
                    )

            self.assertEqual(
                {tool.name for tool in tools.tools},
                {
                    "read_coaching_context",
                    "create_training_session",
                    "list_training_sessions",
                    "get_training_session",
                    "replace_training_session",
                    "delete_training_session",
                    "create_goal_event",
                    "list_goal_events",
                    "get_goal_event",
                    "replace_goal_event",
                    "delete_goal_event",
                    "create_training_plan",
                    "get_training_plan",
                    "list_training_plans",
                    "get_training_plan_history",
                    "activate_training_plan",
                    "archive_training_plan",
                    "delete_training_plan",
                    "adjust_training_plan",
                    "set_planned_session_fulfilment",
                },
            )
            self.assertFalse(result.isError)
            self.assertEqual(
                result.structuredContent["window"],
                {
                    "start_date": "2026-07-18",
                    "end_date": "2026-07-18",
                    "days": 1,
                },
            )
            self.assertEqual(
                result.structuredContent["athlete"]["full_name"],
                "Synthetic Athlete",
            )
            self.assertIsNone(
                result.structuredContent["wellness"]["records"][0][
                    "training_readiness"
                ]
            )
            self.assertEqual(
                result.structuredContent["collection_health"]["run"]["outcome"],
                "successful",
            )
            self.assertEqual(
                result.structuredContent["freshness"],
                {
                    "athlete_snapshot_date": "2026-07-18",
                    "wellness_latest_date": "2026-07-18",
                    "training_history_latest_date": "2026-07-18",
                },
            )
            session = result.structuredContent["training_history"][0]
            self.assertEqual(session["origin"], "garmin_derived")
            self.assertEqual(session["sport"], "running")
            self.assertIsNone(session["session_type"])
            self.assertIsNone(session["session_rpe"])
            self.assertIsNone(session["loads"])
            self.assertEqual(
                session["duration"],
                {"value": 1800, "unit": "seconds", "basis": "source_reported"},
            )

    async def test_client_creates_reads_replaces_and_deletes_app_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "derived" / "activities.json",
                [
                    {
                        "activity_id": "fixture-run",
                        "start_local": "2026-07-18 07:30:00",
                        "type": "running",
                        "duration_s": 1800,
                    }
                ],
            )
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "coach.mcp_server"],
                cwd=Path(__file__).resolve().parents[1],
                env={"GARMIN_COACH_DATA_DIR": str(root)},
            )
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()

                    created = await session.call_tool(
                        "create_training_session",
                        {
                            "sport": "bouldering",
                            "local_date": "2026-07-18",
                            "session_rpe": 7,
                            "duration": {
                                "value": 5400,
                                "unit": "seconds",
                                "basis": "active",
                            },
                        },
                    )
                    self.assertFalse(created.isError)
                    session_id = created.structuredContent["id"]
                    self.assertTrue(session_id.startswith("app:"))
                    self.assertEqual(created.structuredContent["revision"], 1)

                    listed = await session.call_tool(
                        "list_training_sessions",
                        {"days": 7, "end_date": "2026-07-18"},
                    )
                    ids = {s["id"] for s in listed.structuredContent["sessions"]}
                    self.assertEqual(ids, {"garmin:fixture-run", session_id})

                    fetched = await session.call_tool(
                        "get_training_session", {"session_id": session_id}
                    )
                    self.assertEqual(
                        fetched.structuredContent["sport"], "bouldering"
                    )

                    replaced = await session.call_tool(
                        "replace_training_session",
                        {
                            "session_id": session_id,
                            "expected_revision": 1,
                            "sport": "bouldering",
                            "local_date": "2026-07-18",
                            "session_rpe": 9,
                        },
                    )
                    self.assertFalse(replaced.isError)
                    self.assertEqual(replaced.structuredContent["revision"], 2)

                    stale = await session.call_tool(
                        "replace_training_session",
                        {
                            "session_id": session_id,
                            "expected_revision": 1,
                            "sport": "bouldering",
                            "local_date": "2026-07-18",
                        },
                    )
                    self.assertTrue(stale.isError)

                    garmin = await session.call_tool(
                        "replace_training_session",
                        {
                            "session_id": "garmin:fixture-run",
                            "expected_revision": 1,
                            "sport": "running",
                            "local_date": "2026-07-18",
                        },
                    )
                    self.assertTrue(garmin.isError)

                    deleted = await session.call_tool(
                        "delete_training_session",
                        {"session_id": session_id, "expected_revision": 2},
                    )
                    self.assertFalse(deleted.isError)
                    self.assertTrue(deleted.structuredContent["deleted"])

                    gone = await session.call_tool(
                        "get_training_session", {"session_id": session_id}
                    )
                    self.assertTrue(gone.isError)

    async def test_integer_tool_arguments_reject_float_and_bool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "coach.mcp_server"],
                cwd=Path(__file__).resolve().parents[1],
                env={"GARMIN_COACH_DATA_DIR": tmp},
            )
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()

                    good = await session.call_tool(
                        "create_training_session",
                        {
                            "sport": "bouldering",
                            "local_date": "2026-07-18",
                            "session_rpe": 7,
                        },
                    )
                    self.assertFalse(good.isError)

                    as_float = await session.call_tool(
                        "create_training_session",
                        {
                            "sport": "bouldering",
                            "local_date": "2026-07-18",
                            "session_rpe": 7.0,
                        },
                    )
                    self.assertTrue(as_float.isError)

                    as_bool = await session.call_tool(
                        "create_training_session",
                        {
                            "sport": "bouldering",
                            "local_date": "2026-07-18",
                            "session_rpe": True,
                        },
                    )
                    self.assertTrue(as_bool.isError)

                    rev_float = await session.call_tool(
                        "delete_training_session",
                        {"session_id": good.structuredContent["id"],
                         "expected_revision": 1.0},
                    )
                    self.assertTrue(rev_float.isError)

    async def test_client_creates_lists_replaces_and_deletes_goal_event(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "coach.mcp_server"],
                cwd=Path(__file__).resolve().parents[1],
                env={"GARMIN_COACH_DATA_DIR": tmp},
            )
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()

                    created = await session.call_tool(
                        "create_goal_event",
                        {
                            "name": "Autumn City Marathon",
                            "local_date": "2026-10-11",
                            "sport": "running",
                            "priority": "primary",
                            "distance": {"value": 42195, "unit": "meters"},
                            "goal": {
                                "target_duration": {
                                    "value": 10800,
                                    "unit": "seconds",
                                },
                                "statement": "Sub-3 hours",
                            },
                        },
                    )
                    self.assertFalse(created.isError)
                    event_id = created.structuredContent["id"]
                    self.assertTrue(event_id.startswith("app:"))
                    self.assertEqual(created.structuredContent["status"], "scheduled")

                    # A goal event never appears as a Training Session.
                    sessions = await session.call_tool(
                        "list_training_sessions",
                        {"days": 90, "end_date": "2026-10-11"},
                    )
                    self.assertEqual(sessions.structuredContent["sessions"], [])

                    listed = await session.call_tool("list_goal_events", {})
                    ids = {e["id"] for e in listed.structuredContent["goal_events"]}
                    self.assertEqual(ids, {event_id})

                    fetched = await session.call_tool(
                        "get_goal_event", {"goal_event_id": event_id}
                    )
                    self.assertEqual(
                        fetched.structuredContent["name"], "Autumn City Marathon"
                    )

                    replaced = await session.call_tool(
                        "replace_goal_event",
                        {
                            "goal_event_id": event_id,
                            "expected_revision": 1,
                            "name": "Autumn City Marathon",
                            "local_date": "2026-10-18",
                            "sport": "running",
                            "priority": "secondary",
                            "status": "cancelled",
                        },
                    )
                    self.assertFalse(replaced.isError)
                    self.assertEqual(replaced.structuredContent["revision"], 2)
                    self.assertEqual(
                        replaced.structuredContent["status"], "cancelled"
                    )

                    stale = await session.call_tool(
                        "replace_goal_event",
                        {
                            "goal_event_id": event_id,
                            "expected_revision": 1,
                            "name": "Autumn City Marathon",
                            "local_date": "2026-10-18",
                            "sport": "running",
                            "priority": "secondary",
                        },
                    )
                    self.assertTrue(stale.isError)

                    bad_priority = await session.call_tool(
                        "create_goal_event",
                        {
                            "name": "Club B race",
                            "local_date": "2026-05-01",
                            "sport": "running",
                            "priority": "B",
                        },
                    )
                    self.assertTrue(bad_priority.isError)

                    rev_float = await session.call_tool(
                        "delete_goal_event",
                        {"goal_event_id": event_id, "expected_revision": 2.0},
                    )
                    self.assertTrue(rev_float.isError)

                    deleted = await session.call_tool(
                        "delete_goal_event",
                        {"goal_event_id": event_id, "expected_revision": 2},
                    )
                    self.assertFalse(deleted.isError)
                    self.assertTrue(deleted.structuredContent["deleted"])

                    gone = await session.call_tool(
                        "get_goal_event", {"goal_event_id": event_id}
                    )
                    self.assertTrue(gone.isError)

    async def test_client_manages_a_training_plan_end_to_end(self) -> None:
        today = date.today()

        def day(offset: int) -> str:
            return (today + timedelta(days=offset)).isoformat()

        with tempfile.TemporaryDirectory() as tmp:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "coach.mcp_server"],
                cwd=Path(__file__).resolve().parents[1],
                env={"GARMIN_COACH_DATA_DIR": tmp},
            )
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()

                    # A real client links a Goal Event revision into a plan.
                    event = await session.call_tool(
                        "create_goal_event",
                        {
                            "name": "Autumn City Marathon",
                            "local_date": day(80),
                            "sport": "running",
                            "priority": "primary",
                        },
                    )
                    self.assertFalse(event.isError)
                    event_id = event.structuredContent["id"]

                    created = await session.call_tool(
                        "create_training_plan",
                        {
                            "name": "Marathon build",
                            "starts_on": day(0),
                            "ends_on": day(90),
                            "reason": "Base into build",
                            "constraints": ["No hard sessions on Mondays"],
                            "goal_events": [
                                {
                                    "goal_event_id": event_id,
                                    "goal_event_revision": 1,
                                }
                            ],
                            "planned_sessions": [
                                {
                                    "scheduled_date": day(1),
                                    "sport": "running",
                                    "prescription": "Easy 5k",
                                },
                                {
                                    "scheduled_date": day(40),
                                    "sport": "running",
                                    "prescription": "Long run 20k",
                                    "target_distance_meters": 20000,
                                },
                            ],
                        },
                    )
                    self.assertFalse(created.isError)
                    plan_id = created.structuredContent["id"]
                    self.assertEqual(created.structuredContent["status"], "draft")
                    self.assertFalse(created.structuredContent["requires_review"])
                    early_id = next(
                        p["id"]
                        for p in created.structuredContent["planned_sessions"]
                        if p["scheduled_date"] == day(1)
                    )
                    late_id = next(
                        p["id"]
                        for p in created.structuredContent["planned_sessions"]
                        if p["scheduled_date"] == day(40)
                    )

                    # Inspect the scheduled sessions before activating.
                    fetched = await session.call_tool(
                        "get_training_plan", {"plan_id": plan_id}
                    )
                    self.assertEqual(
                        len(fetched.structuredContent["planned_sessions"]), 2
                    )

                    activated = await session.call_tool(
                        "activate_training_plan",
                        {
                            "plan_id": plan_id,
                            "expected_revision": 1,
                            "reason": "Athlete is ready",
                        },
                    )
                    self.assertFalse(activated.isError)
                    self.assertEqual(activated.structuredContent["status"], "active")
                    self.assertEqual(activated.structuredContent["revision"], 2)

                    # A reasoned adjustment changes only future prescriptions.
                    adjusted = await session.call_tool(
                        "adjust_training_plan",
                        {
                            "plan_id": plan_id,
                            "expected_revision": 2,
                            "reason": "Extend the long run",
                            "effective_from": day(20),
                            "operations": [
                                {
                                    "op": "update",
                                    "planned_session_id": late_id,
                                    "scheduled_date": day(45),
                                    "sport": "running",
                                    "prescription": "Long run 24k",
                                    "target_distance_meters": 24000,
                                }
                            ],
                        },
                    )
                    self.assertFalse(adjusted.isError)
                    self.assertEqual(adjusted.structuredContent["revision"], 3)

                    # The frozen past prescription cannot be rewritten.
                    frozen = await session.call_tool(
                        "adjust_training_plan",
                        {
                            "plan_id": plan_id,
                            "expected_revision": 3,
                            "reason": "Try to rewrite the past",
                            "effective_from": day(20),
                            "operations": [
                                {
                                    "op": "update",
                                    "planned_session_id": early_id,
                                    "scheduled_date": day(1),
                                    "sport": "running",
                                    "prescription": "Rewritten easy run",
                                }
                            ],
                        },
                    )
                    self.assertTrue(frozen.isError)

                    # Match completed work to the frozen prescription (audited).
                    completed = await session.call_tool(
                        "create_training_session",
                        {"sport": "running", "local_date": day(1)},
                    )
                    self.assertFalse(completed.isError)
                    session_id = completed.structuredContent["id"]

                    matched = await session.call_tool(
                        "set_planned_session_fulfilment",
                        {
                            "plan_id": plan_id,
                            "expected_revision": 3,
                            "planned_session_id": early_id,
                            "disposition": "fulfilled",
                            "reason": "Ran it as prescribed",
                            "matches": [session_id],
                        },
                    )
                    self.assertFalse(matched.isError)
                    self.assertEqual(matched.structuredContent["revision"], 4)
                    fulfilled = next(
                        p
                        for p in matched.structuredContent["planned_sessions"]
                        if p["id"] == early_id
                    )
                    self.assertEqual(fulfilled["disposition"], "fulfilled")
                    self.assertEqual(fulfilled["matches"], [session_id])

                    # A matched, completed session stays deletion-protected.
                    protected = await session.call_tool(
                        "delete_training_session",
                        {"session_id": session_id, "expected_revision": 1},
                    )
                    self.assertTrue(protected.isError)

                    # The audit trail retains every reasoned revision in order.
                    history = await session.call_tool(
                        "get_training_plan_history", {"plan_id": plan_id}
                    )
                    self.assertFalse(history.isError)
                    self.assertEqual(
                        [r["kind"] for r in history.structuredContent["revisions"]],
                        ["create", "lifecycle", "adjustment", "fulfilment_correction"],
                    )

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
