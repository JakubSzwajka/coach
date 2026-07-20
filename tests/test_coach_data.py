from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from coach.data import CoachData, ContextWindow, DataIntegrityError


class CoachDataTest(unittest.TestCase):
    def test_read_context_combines_bounded_records_and_preserves_unknowns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "derived" / "athlete.json",
                {
                    "snapshot_date": "2026-07-18",
                    "full_name": "Synthetic Athlete",
                    "user_profile_id": "synthetic-profile-id",
                    "gender": None,
                    "vo2max_running": None,
                    "available_training_days": ["MONDAY", "THURSDAY"],
                },
            )
            self._write_json(
                root / "derived" / "daily" / "2026-07-17.json",
                {
                    "date": "2026-07-17",
                    "sleep_score": 82,
                    "hrv_avg": None,
                    "training_readiness": 71,
                },
            )
            self._write_json(
                root / "derived" / "daily" / "2026-07-18.json",
                {
                    "date": "2026-07-18",
                    "sleep_score": None,
                    "hrv_avg": 54,
                    "training_readiness": None,
                },
            )
            self._write_json(
                root / "derived" / "activities.json",
                [
                    {
                        "activity_id": "fixture-recent",
                        "name": "Synthetic Run",
                        "type": "running",
                        "start_local": "2026-07-18 07:30:00",
                        "distance_m": 5000,
                        "duration_s": 1800,
                        "avg_hr": None,
                        "max_hr": 165,
                        "elevation_gain_m": None,
                        "avg_speed_mps": None,
                        "calories": None,
                        "training_effect_aerobic": 3.1,
                        "training_effect_anaerobic": None,
                    },
                    {
                        "activity_id": "fixture-old",
                        "name": "Old Synthetic Run",
                        "type": "running",
                        "start_local": "2026-07-10 07:30:00",
                        "distance_m": 3000,
                        "duration_s": 1200,
                    },
                ],
            )
            health = {
                "schema_version": 1,
                "run": {
                    "outcome": "degraded",
                    "attempted_at": "2026-07-18T08:00:00Z",
                    "finished_at": "2026-07-18T08:01:00Z",
                    "last_success_at": "2026-07-17T08:01:00Z",
                },
                "domains": {
                    "daily": {
                        "outcome": "degraded",
                        "attempted_at": "2026-07-18T08:00:00Z",
                        "finished_at": "2026-07-18T08:01:00Z",
                        "last_success_at": "2026-07-17T08:01:00Z",
                        "dates": {},
                    },
                    "activities": {
                        "outcome": "successful",
                        "attempted_at": "2026-07-18T08:00:00Z",
                        "finished_at": "2026-07-18T08:01:00Z",
                        "last_success_at": "2026-07-18T08:01:00Z",
                        "date": "2026-07-18",
                    },
                    "plans": {
                        "outcome": "not_attempted",
                        "attempted_at": None,
                        "finished_at": None,
                        "last_success_at": None,
                        "date": None,
                    },
                    "profile": {
                        "outcome": "not_attempted",
                        "attempted_at": None,
                        "finished_at": None,
                        "last_success_at": "2026-07-18T08:01:00Z",
                        "date": None,
                    },
                },
            }
            self._write_json(root / "index" / "collector-health.json", health)

            context = CoachData(root).read_context(
                ContextWindow(days=3, end_date=date(2026, 7, 18))
            )

            self.assertEqual(
                context["window"],
                {
                    "start_date": "2026-07-16",
                    "end_date": "2026-07-18",
                    "days": 3,
                },
            )
            self.assertEqual(
                context["athlete"],
                {
                    "snapshot_date": "2026-07-18",
                    "full_name": "Synthetic Athlete",
                    "gender": None,
                    "vo2max_running": None,
                    "available_training_days": ["MONDAY", "THURSDAY"],
                },
            )
            self.assertNotIn("user_profile_id", context["athlete"])
            self.assertEqual(
                context["wellness"],
                {
                    "records": [
                        {
                            "date": "2026-07-17",
                            "sleep_score": 82,
                            "hrv_avg": None,
                            "training_readiness": 71,
                        },
                        {
                            "date": "2026-07-18",
                            "sleep_score": None,
                            "hrv_avg": 54,
                            "training_readiness": None,
                        },
                    ],
                    "missing_dates": ["2026-07-16"],
                },
            )
            self.assertEqual(context["collection_health"], health)
            self.assertEqual(
                context["freshness"],
                {
                    "athlete_snapshot_date": "2026-07-18",
                    "wellness_latest_date": "2026-07-18",
                    "training_history_latest_date": "2026-07-18",
                },
            )
            self.assertEqual(len(context["training_history"]), 1)
            session = context["training_history"][0]
            self.assertEqual(session["id"], "garmin:fixture-recent")
            self.assertEqual(session["origin"], "garmin_derived")
            self.assertEqual(session["local_date"], "2026-07-18")
            self.assertEqual(session["local_start"], "2026-07-18 07:30:00")
            self.assertEqual(session["timing_precision"], "local_datetime")
            self.assertEqual(session["sport"], "running")
            self.assertIsNone(session["session_type"])
            self.assertIsNone(session["session_rpe"])
            self.assertIsNone(session["loads"])
            self.assertEqual(
                session["duration"],
                {"value": 1800, "unit": "seconds", "basis": "source_reported"},
            )
            self.assertEqual(session["distance"], {"value": 5000, "unit": "meters"})
            self.assertEqual(
                session["source_detail"],
                {
                    "provider": "garmin",
                    "activity_type": "running",
                    "average_heart_rate": None,
                    "maximum_heart_rate": 165,
                    "elevation_gain_m": None,
                    "average_speed_mps": None,
                    "calories": None,
                    "training_effect_aerobic": 3.1,
                    "training_effect_anaerobic": None,
                },
            )

    def test_read_context_rejects_an_unknown_health_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_json(root / "derived" / "activities.json", [])
            self._write_json(
                root / "index" / "collector-health.json",
                {"schema_version": 99, "run": {}, "domains": {}},
            )

            with self.assertRaisesRegex(
                DataIntegrityError, "Unsupported collector health schema_version"
            ):
                CoachData(root).read_context(
                    ContextWindow(days=1, end_date=date(2026, 7, 18))
                )

    def test_read_context_reports_invalid_utf8_as_data_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "derived" / "athlete.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\xff")

            with self.assertRaisesRegex(
                DataIntegrityError, "Malformed JSON in derived/athlete.json"
            ):
                CoachData(root).read_context(
                    ContextWindow(days=1, end_date=date(2026, 7, 18))
                )

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
