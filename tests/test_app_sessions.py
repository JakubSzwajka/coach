from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from coach.data import (
    AlreadyExists,
    CoachData,
    ContextWindow,
    DataIntegrityError,
    InvalidRecord,
    NotFound,
    ReadOnlyRecord,
    RevisionConflict,
)


def _boulder_content(**overrides: object) -> dict[str, object]:
    content: dict[str, object] = {
        "sport": "bouldering",
        "local_date": "2026-07-18",
        "duration": {"value": 5400, "unit": "seconds", "basis": "active"},
        "session_rpe": 7,
        "notes": "Worked the overhang project.",
    }
    content.update(overrides)
    return content


class CreateSessionTest(unittest.TestCase):
    def test_create_date_only_session_projects_app_record_with_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))

            created = coach.create_session(_boulder_content())

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

            # Persisted and readable back through the façade.
            fetched = coach.get_session(created["id"])
            self.assertEqual(fetched, created)
            stem = created["id"].split(":", 1)[1]
            self.assertTrue(
                (Path(tmp) / "app" / "sessions" / f"{stem}.json").exists()
            )

    def test_create_with_local_start_sets_local_datetime_precision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))

            created = coach.create_session(
                _boulder_content(local_start="2026-07-18 18:30:00")
            )

            self.assertEqual(created["timing_precision"], "local_datetime")
            self.assertEqual(created["local_start"], "2026-07-18 18:30:00")

    def test_create_normalizes_method_tagged_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))

            created = coach.create_session(
                _boulder_content(
                    loads=[
                        {
                            "value": 630,
                            "unit": "rpe_minutes",
                            "method": "session_rpe",
                            "source": "athlete",
                        }
                    ]
                )
            )

            self.assertEqual(
                created["loads"],
                [
                    {
                        "value": 630,
                        "unit": "rpe_minutes",
                        "method": "session_rpe",
                        "source": "athlete",
                    }
                ],
            )

    def test_create_always_makes_distinct_records_without_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))

            first = coach.create_session(_boulder_content())
            second = coach.create_session(_boulder_content())

            self.assertNotEqual(first["id"], second["id"])
            listed = coach.list_sessions(
                ContextWindow(days=7, end_date=date(2026, 7, 18))
            )
            self.assertEqual(
                {s["id"] for s in listed}, {first["id"], second["id"]}
            )


class ValidationTest(unittest.TestCase):
    def _reject(self, **overrides: object) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(InvalidRecord):
                CoachData(Path(tmp)).create_session(_boulder_content(**overrides))

    def test_missing_sport_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(InvalidRecord):
                CoachData(Path(tmp)).create_session(
                    {"local_date": "2026-07-18"}
                )

    def test_invalid_fields_are_rejected(self) -> None:
        self._reject(sport="   ")
        self._reject(local_date="2026-13-40")
        self._reject(session_rpe=0)
        self._reject(session_rpe=11)
        self._reject(session_rpe=7.5)
        self._reject(duration={"value": 60, "unit": "seconds"})  # no basis
        self._reject(duration={"value": -1, "unit": "seconds", "basis": "active"})
        self._reject(distance={"value": 100})  # no unit
        self._reject(loads=[{"value": 10, "unit": "x", "method": "m"}])  # no source
        self._reject(unexpected="nope")
        self._reject(local_start="2026-07-19 06:00:00")  # date mismatch
        self._reject(local_start="2026-07-18")  # date only, no time
        self._reject(local_start="2026-07-18 not-a-time")  # unparseable time
        self._reject(local_start="2026-07-18Tbroken")  # unparseable iso


class ReplaceDeleteTest(unittest.TestCase):
    def test_replace_with_expected_revision_advances_and_preserves_provenance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            created = coach.create_session(_boulder_content())

            replaced = coach.replace_session(
                created["id"],
                created["revision"],
                _boulder_content(sport="strength", session_rpe=8, notes="Heavy day."),
            )

            self.assertEqual(replaced["id"], created["id"])
            self.assertEqual(replaced["revision"], 2)
            self.assertEqual(replaced["sport"], "strength")
            self.assertEqual(replaced["session_rpe"], 8)
            self.assertEqual(replaced["origin"], "app_record")
            self.assertEqual(replaced["provenance"], created["provenance"])
            self.assertEqual(replaced["created_at"], created["created_at"])
            self.assertEqual(coach.get_session(created["id"])["revision"], 2)

    def test_replace_with_stale_revision_conflicts_and_preserves_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            created = coach.create_session(_boulder_content())

            with self.assertRaises(RevisionConflict):
                coach.replace_session(
                    created["id"], 99, _boulder_content(sport="strength")
                )

            unchanged = coach.get_session(created["id"])
            self.assertEqual(unchanged["revision"], 1)
            self.assertEqual(unchanged["sport"], "bouldering")

    def test_delete_with_expected_revision_removes_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            created = coach.create_session(_boulder_content())

            result = coach.delete_session(created["id"], created["revision"])

            self.assertEqual(
                result, {"id": created["id"], "deleted": True, "revision": 1}
            )
            with self.assertRaises(NotFound):
                coach.get_session(created["id"])
            with self.assertRaises(NotFound):
                coach.delete_session(created["id"], 1)

    def test_delete_with_stale_revision_conflicts_and_keeps_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            created = coach.create_session(_boulder_content())

            with self.assertRaises(RevisionConflict):
                coach.delete_session(created["id"], 99)

            self.assertEqual(coach.get_session(created["id"])["revision"], 1)

    def test_unknown_app_session_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            coach = CoachData(Path(tmp))
            missing = "app:" + "0" * 32
            with self.assertRaises(NotFound):
                coach.get_session(missing)
            with self.assertRaises(NotFound):
                coach.replace_session(missing, 1, _boulder_content())
            with self.assertRaises(NotFound):
                coach.delete_session(missing, 1)


class GarminReadOnlyTest(unittest.TestCase):
    def _write_activities(self, root: Path) -> None:
        path = root / "derived" / "activities.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {
                        "activity_id": "fixture-1",
                        "start_local": "2026-07-18 07:30:00",
                        "type": "running",
                        "duration_s": 1800,
                        "distance_m": 5000,
                        "name": "Morning run",
                    }
                ]
            ),
            encoding="utf-8",
        )

    def test_garmin_session_is_readable_but_not_mutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_activities(root)
            coach = CoachData(root)

            session = coach.get_session("garmin:fixture-1")
            self.assertEqual(session["origin"], "garmin_derived")
            self.assertEqual(session["sport"], "running")

            with self.assertRaises(ReadOnlyRecord):
                coach.replace_session(
                    "garmin:fixture-1", 1, _boulder_content(sport="running")
                )
            with self.assertRaises(ReadOnlyRecord):
                coach.delete_session("garmin:fixture-1", 1)

    def test_list_unifies_garmin_and_app_sessions_and_filters_by_sport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_activities(root)
            coach = CoachData(root)
            created = coach.create_session(_boulder_content())

            window = ContextWindow(days=7, end_date=date(2026, 7, 18))
            everything = coach.list_sessions(window)
            self.assertEqual(
                {s["id"] for s in everything},
                {"garmin:fixture-1", created["id"]},
            )

            only_boulder = coach.list_sessions(window, sport="bouldering")
            self.assertEqual([s["id"] for s in only_boulder], [created["id"]])


class StoredIntegrityTest(unittest.TestCase):
    def test_malformed_stored_record_is_reported_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coach = CoachData(root)
            created = coach.create_session(_boulder_content())
            stem = created["id"].split(":", 1)[1]
            (root / "app" / "sessions" / f"{stem}.json").write_text(
                json.dumps({"id": created["id"], "origin": "app_record"}),
                encoding="utf-8",
            )

            with self.assertRaises(DataIntegrityError):
                coach.get_session(created["id"])
            with self.assertRaises(DataIntegrityError):
                coach.list_sessions(ContextWindow(days=7, end_date=date(2026, 7, 18)))

    def test_incomplete_or_mismatched_envelope_is_data_integrity_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coach = CoachData(root)
            created = coach.create_session(_boulder_content())
            stem = created["id"].split(":", 1)[1]
            path = root / "app" / "sessions" / f"{stem}.json"
            record = json.loads(path.read_text(encoding="utf-8"))

            without_provenance = {k: v for k, v in record.items() if k != "provenance"}
            path.write_text(json.dumps(without_provenance), encoding="utf-8")
            with self.assertRaises(DataIntegrityError):
                coach.get_session(created["id"])

            mismatched = {**record, "id": "app:" + "f" * 32}
            path.write_text(json.dumps(mismatched), encoding="utf-8")
            with self.assertRaises(DataIntegrityError):
                coach.get_session(created["id"])


if __name__ == "__main__":
    unittest.main()
