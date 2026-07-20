from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from coach.data import (
    CoachData,
    ContextWindow,
    DataIntegrityError,
    InvalidRecord,
    NotFound,
    ReadOnlyRecord,
    RevisionConflict,
)


def _marathon_content(**overrides: object) -> dict[str, object]:
    content: dict[str, object] = {
        "name": "Autumn City Marathon",
        "sport": "running",
        "local_date": "2026-10-11",
        "priority": "primary",
    }
    content.update(overrides)
    return content


class CreateGoalEventTest(unittest.TestCase):
    def test_create_date_only_event_projects_app_record_with_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))

            created = coach.create_goal_event(_marathon_content())

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

            fetched = coach.get_goal_event(created["id"])
            self.assertEqual(fetched, created)
            stem = created["id"].split(":", 1)[1]
            self.assertTrue(
                (Path(tmp) / "app" / "goal_events" / f"{stem}.json").exists()
            )

    def test_create_with_local_start_sets_local_datetime_precision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))

            created = coach.create_goal_event(
                _marathon_content(
                    local_start="2026-10-11 09:00:00", time_zone="Europe/Warsaw"
                )
            )

            self.assertEqual(created["timing_precision"], "local_datetime")
            self.assertEqual(created["local_start"], "2026-10-11 09:00:00")
            self.assertEqual(created["time_zone"], "Europe/Warsaw")

    def test_create_records_distance_goal_and_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))

            created = coach.create_goal_event(
                _marathon_content(
                    distance={"value": 42195, "unit": "meters"},
                    goal={
                        "target_duration": {"value": 10800, "unit": "seconds"},
                        "statement": "Sub-3 hours",
                    },
                    outcome={
                        "actual_duration": {"value": 10950, "unit": "seconds"},
                        "statement": "Faded at 35k",
                    },
                )
            )

            self.assertEqual(created["distance"], {"value": 42195, "unit": "meters"})
            self.assertEqual(
                created["goal"],
                {
                    "target_duration": {"value": 10800, "unit": "seconds"},
                    "statement": "Sub-3 hours",
                },
            )
            self.assertEqual(
                created["outcome"],
                {
                    "actual_duration": {"value": 10950, "unit": "seconds"},
                    "statement": "Faded at 35k",
                },
            )

    def test_create_accepts_each_canonical_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            for priority in ("primary", "secondary", "practice"):
                created = coach.create_goal_event(_marathon_content(priority=priority))
                self.assertEqual(created["priority"], priority)

    def test_goal_statement_or_target_alone_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))

            statement_only = coach.create_goal_event(
                _marathon_content(goal={"statement": "Finish smiling"})
            )
            self.assertEqual(
                statement_only["goal"],
                {"target_duration": None, "statement": "Finish smiling"},
            )

            target_only = coach.create_goal_event(
                _marathon_content(
                    goal={"target_duration": {"value": 10800, "unit": "seconds"}}
                )
            )
            self.assertEqual(
                target_only["goal"],
                {
                    "target_duration": {"value": 10800, "unit": "seconds"},
                    "statement": None,
                },
            )

    def test_empty_goal_object_normalizes_to_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            created = coach.create_goal_event(_marathon_content(goal={}))
            self.assertIsNone(created["goal"])

    def test_create_always_makes_distinct_records_without_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))

            first = coach.create_goal_event(_marathon_content())
            second = coach.create_goal_event(_marathon_content())

            self.assertNotEqual(first["id"], second["id"])
            listed = coach.list_goal_events()
            self.assertEqual({e["id"] for e in listed}, {first["id"], second["id"]})


class ValidationTest(unittest.TestCase):
    def _reject(self, **overrides: object) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(InvalidRecord):
                CoachData(Path(tmp)).create_goal_event(_marathon_content(**overrides))

    def test_required_fields_are_rejected_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            with self.assertRaises(InvalidRecord):
                coach.create_goal_event({"sport": "running", "priority": "primary"})
            with self.assertRaises(InvalidRecord):
                coach.create_goal_event(
                    {"name": "x", "local_date": "2026-10-11", "priority": "primary"}
                )
            with self.assertRaises(InvalidRecord):
                coach.create_goal_event(
                    {"name": "x", "sport": "running", "local_date": "2026-10-11"}
                )

    def test_invalid_fields_are_rejected(self) -> None:
        self._reject(name="   ")
        self._reject(sport="   ")
        self._reject(local_date="2026-13-40")
        self._reject(priority="A")  # A/B/C is not the canonical vocabulary
        self._reject(priority="Primary")  # case sensitive canonical value
        self._reject(status="planned")  # not a canonical lifecycle state
        self._reject(distance={"value": 100})  # no unit
        self._reject(goal={"unexpected": 1})
        self._reject(goal={"target_duration": {"value": -1, "unit": "seconds"}})
        self._reject(goal={"target_duration": {"value": 10800, "unit": "minutes"}})
        self._reject(outcome={"unexpected": 1})
        self._reject(outcome={"actual_duration": {"value": 0, "unit": "seconds"}})
        self._reject(unexpected="nope")
        self._reject(local_start="2026-10-12 09:00:00")  # date mismatch
        self._reject(local_start="2026-10-11")  # date only, no time
        # Non-hashable inputs must raise InvalidRecord, not an unhandled TypeError.
        self._reject(priority=[])
        self._reject(status={})
        self._reject(goal={"target_duration": {"value": 100, "unit": []}})


class ReplaceDeleteTest(unittest.TestCase):
    def test_replace_with_expected_revision_advances_and_preserves_provenance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            created = coach.create_goal_event(_marathon_content())

            replaced = coach.replace_goal_event(
                created["id"],
                created["revision"],
                _marathon_content(
                    priority="secondary",
                    local_date="2026-10-18",
                    notes="Moved a week and downgraded to B.",
                ),
            )

            self.assertEqual(replaced["id"], created["id"])
            self.assertEqual(replaced["revision"], 2)
            self.assertEqual(replaced["priority"], "secondary")
            self.assertEqual(replaced["local_date"], "2026-10-18")
            self.assertEqual(replaced["origin"], "app_record")
            self.assertEqual(replaced["provenance"], created["provenance"])
            self.assertEqual(replaced["created_at"], created["created_at"])
            self.assertEqual(coach.get_goal_event(created["id"])["revision"], 2)

    def test_cancellation_is_a_revisioned_change_that_keeps_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            created = coach.create_goal_event(_marathon_content())

            cancelled = coach.replace_goal_event(
                created["id"], 1, _marathon_content(status="cancelled")
            )

            self.assertEqual(cancelled["id"], created["id"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(cancelled["revision"], 2)

    def test_recording_outcome_never_infers_status_or_creates_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            created = coach.create_goal_event(_marathon_content())

            with_outcome = coach.replace_goal_event(
                created["id"],
                1,
                _marathon_content(
                    outcome={"actual_duration": {"value": 10950, "unit": "seconds"}}
                ),
            )

            # Outcome is recorded, but lifecycle stays exactly as supplied.
            self.assertEqual(with_outcome["status"], "scheduled")
            self.assertEqual(
                with_outcome["outcome"],
                {
                    "actual_duration": {"value": 10950, "unit": "seconds"},
                    "statement": None,
                },
            )
            # An outcome never becomes a Training Session.
            window = ContextWindow(days=30, end_date=date(2026, 10, 31))
            self.assertEqual(coach.list_sessions(window), [])

    def test_replace_with_stale_revision_conflicts_and_preserves_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            created = coach.create_goal_event(_marathon_content())

            with self.assertRaises(RevisionConflict):
                coach.replace_goal_event(
                    created["id"], 99, _marathon_content(priority="practice")
                )

            unchanged = coach.get_goal_event(created["id"])
            self.assertEqual(unchanged["revision"], 1)
            self.assertEqual(unchanged["priority"], "primary")

    def test_delete_with_expected_revision_removes_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            created = coach.create_goal_event(_marathon_content())

            result = coach.delete_goal_event(created["id"], created["revision"])

            self.assertEqual(
                result, {"id": created["id"], "deleted": True, "revision": 1}
            )
            with self.assertRaises(NotFound):
                coach.get_goal_event(created["id"])
            with self.assertRaises(NotFound):
                coach.delete_goal_event(created["id"], 1)

    def test_delete_with_stale_revision_conflicts_and_keeps_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            created = coach.create_goal_event(_marathon_content())

            with self.assertRaises(RevisionConflict):
                coach.delete_goal_event(created["id"], 99)

            self.assertEqual(coach.get_goal_event(created["id"])["revision"], 1)

    def test_unknown_goal_event_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            missing = "app:" + "0" * 32
            with self.assertRaises(NotFound):
                coach.get_goal_event(missing)
            with self.assertRaises(NotFound):
                coach.replace_goal_event(missing, 1, _marathon_content())
            with self.assertRaises(NotFound):
                coach.delete_goal_event(missing, 1)


class ListGoalEventsTest(unittest.TestCase):
    def test_list_sorts_by_date_ascending_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            spring = coach.create_goal_event(
                _marathon_content(name="Spring 10k", sport="running", local_date="2026-04-05", priority="practice")
            )
            autumn = coach.create_goal_event(_marathon_content(local_date="2026-10-11"))
            climb = coach.create_goal_event(
                _marathon_content(name="Boulder comp", sport="bouldering", local_date="2026-06-01", priority="secondary")
            )

            ordered = coach.list_goal_events()
            self.assertEqual(
                [e["id"] for e in ordered], [spring["id"], climb["id"], autumn["id"]]
            )

            only_running = coach.list_goal_events(sport="running")
            self.assertEqual(
                {e["id"] for e in only_running}, {spring["id"], autumn["id"]}
            )

            only_secondary = coach.list_goal_events(status="scheduled")
            self.assertEqual(len(only_secondary), 3)
            practice = coach.list_goal_events(sport="running", status="scheduled")
            self.assertEqual({e["id"] for e in practice}, {spring["id"], autumn["id"]})

    def test_same_day_events_sort_by_real_time_across_separators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            # A space separator sorts before "T" lexicographically, so a raw
            # string sort would misorder the later "T"-formatted 08:00 event.
            nine = coach.create_goal_event(
                _marathon_content(local_start="2026-10-11 09:00:00")
            )
            eight = coach.create_goal_event(
                _marathon_content(local_start="2026-10-11T08:00:00")
            )

            ordered = [e["id"] for e in coach.list_goal_events()]
            self.assertEqual(ordered, [eight["id"], nine["id"]])

    def test_goal_events_and_sessions_are_separate_stores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            event = coach.create_goal_event(_marathon_content())
            session = coach.create_session(
                {
                    "sport": "running",
                    "local_date": "2026-10-11",
                    "session_rpe": 5,
                }
            )

            self.assertEqual([e["id"] for e in coach.list_goal_events()], [event["id"]])
            window = ContextWindow(days=1, end_date=date(2026, 10, 11))
            self.assertEqual(
                [s["id"] for s in coach.list_sessions(window)], [session["id"]]
            )


class GarminReadOnlyTest(unittest.TestCase):
    def test_garmin_ids_cannot_be_mutated_as_goal_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            with self.assertRaises(ReadOnlyRecord):
                coach.replace_goal_event("garmin:fixture-1", 1, _marathon_content())
            with self.assertRaises(ReadOnlyRecord):
                coach.delete_goal_event("garmin:fixture-1", 1)
            with self.assertRaises(NotFound):
                coach.get_goal_event("garmin:fixture-1")


class StoredIntegrityTest(unittest.TestCase):
    def test_malformed_stored_record_is_reported_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coach = CoachData(root)
            created = coach.create_goal_event(_marathon_content())
            stem = created["id"].split(":", 1)[1]
            (root / "app" / "goal_events" / f"{stem}.json").write_text(
                json.dumps({"id": created["id"], "origin": "app_record"}),
                encoding="utf-8",
            )

            with self.assertRaises(DataIntegrityError):
                coach.get_goal_event(created["id"])
            with self.assertRaises(DataIntegrityError):
                coach.list_goal_events()

    def test_mismatched_envelope_id_is_data_integrity_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coach = CoachData(root)
            created = coach.create_goal_event(_marathon_content())
            stem = created["id"].split(":", 1)[1]
            path = root / "app" / "goal_events" / f"{stem}.json"
            record = json.loads(path.read_text(encoding="utf-8"))

            mismatched = {**record, "id": "app:" + "f" * 32}
            path.write_text(json.dumps(mismatched), encoding="utf-8")
            with self.assertRaises(DataIntegrityError):
                coach.get_goal_event(created["id"])

    def test_stored_content_missing_status_is_reported_not_projected(self) -> None:
        # A defaulted field must not let a malformed stored record slip past
        # coercion into an uncaught projection KeyError.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coach = CoachData(root)
            created = coach.create_goal_event(_marathon_content())
            stem = created["id"].split(":", 1)[1]
            path = root / "app" / "goal_events" / f"{stem}.json"
            record = json.loads(path.read_text(encoding="utf-8"))

            record["content"].pop("status")
            path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(DataIntegrityError):
                coach.get_goal_event(created["id"])
            with self.assertRaises(DataIntegrityError):
                coach.list_goal_events()


if __name__ == "__main__":
    unittest.main()
