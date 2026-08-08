from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from cryptography.fernet import Fernet

from coach.postgres import DatabaseSettings
from coach.postgres.file_import import (
    MaintenanceError,
    import_store,
    inventory_store,
    main,
    reconcile_store,
)
from coach.postgres.migrate import migrate
from tests.postgres.support import test_database


def _reset_database(database_url: str) -> None:
    """Clear disposable import evidence without weakening production guards."""
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "ALTER TABLE file_import_units "
            "DISABLE TRIGGER file_import_units_truncate_guard"
        )
        connection.execute("TRUNCATE TABLE profiles CASCADE")
        connection.execute(
            "ALTER TABLE file_import_units "
            "ENABLE TRIGGER file_import_units_truncate_guard"
        )


class PostgreSQLFileImportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database = test_database()
        cls.database_url = database.migration_url
        cls.application_url = database.application_url
        cls.settings = DatabaseSettings.from_url(cls.database_url)
        migrate(DatabaseSettings.from_url(cls.database_url))

    def setUp(self) -> None:
        _reset_database(self.database_url)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "frozen-store"
        self.root.mkdir()
        self.issuer = "https://identity.example.test"
        self.key = Fernet.generate_key()
        self.fixture = _legacy_fixture(self.root)

    def test_two_profile_import_is_idempotent_and_preserves_authority(self) -> None:
        first = import_store(self.root, self.settings, self.issuer, self.key)
        second = import_store(self.root, self.settings, self.issuer, self.key)

        self.assertTrue(first.ok, first.public())
        self.assertTrue(second.ok, second.public())
        self.assertEqual(first.public()["counts"], second.public()["counts"])
        with psycopg.connect(self.database_url) as connection:
            counts = connection.execute(
                """
                SELECT
                  (SELECT count(*) FROM profiles),
                  (SELECT count(*) FROM source_connections),
                  (SELECT count(*) FROM collected_record_captures),
                  (SELECT count(*) FROM training_sessions WHERE ownership = 'collected'),
                  (SELECT count(*) FROM controlled_observations),
                  (SELECT count(*) FROM training_sessions WHERE ownership = 'app'),
                  (SELECT count(*) FROM goal_events),
                  (SELECT count(*) FROM training_plans),
                  (SELECT count(*) FROM plan_revisions),
                  (SELECT count(*) FROM file_import_units)
                """
            ).fetchone()
            revisions = connection.execute(
                "SELECT array_agg(app_revision ORDER BY profile_id) "
                "FROM training_sessions WHERE ownership = 'app'"
            ).fetchone()[0]
            goal_revisions = connection.execute(
                "SELECT array_agg(revision ORDER BY profile_id) FROM goal_events"
            ).fetchone()[0]
            plan_revisions = connection.execute(
                "SELECT array_agg(current_revision ORDER BY profile_id) FROM training_plans"
            ).fetchone()[0]
            matches = connection.execute(
                "SELECT count(*) FROM plan_revision_matches"
            ).fetchone()[0]
            current_matches = connection.execute(
                "SELECT count(*) FROM planned_session_current_matches"
            ).fetchone()[0]
            isolation = connection.execute(
                """
                SELECT count(*) FROM plan_revision_matches m
                JOIN training_sessions s ON s.id = m.training_session_id
                WHERE s.profile_id <> m.profile_id
                """
            ).fetchone()[0]
            material = connection.execute(
                "SELECT encrypted_credentials, encrypted_tokens "
                "FROM source_connections ORDER BY profile_id"
            ).fetchall()

        expected = self.fixture["expected_counts"]
        self.assertEqual(counts, expected)
        self.assertEqual(revisions, [2, 2])
        self.assertEqual(goal_revisions, [2, 2])
        self.assertEqual(plan_revisions, [2, 2])
        self.assertEqual((matches, current_matches, isolation), (2, 2, 0))
        self.assertTrue(all(row[0] is not None and row[1] is not None for row in material))
        self.assertTrue(all(row[0].startswith(b"gc1:") and row[1].startswith(b"gc1:") for row in material))
        report_text = json.dumps(second.public())
        for forbidden in self.fixture["forbidden"]:
            self.assertNotIn(forbidden, report_text)
        self.assertNotIn(str(self.root), report_text)
        self.assertNotIn(self.application_url, report_text)

    def test_profile_transaction_rolls_back_and_rerun_recovers(self) -> None:
        def fail(stage: str, ordinal: int) -> None:
            if stage == "app_records" and ordinal == 1:
                raise RuntimeError("synthetic injected failure")

        with self.assertRaisesRegex(MaintenanceError, "database_import_failed"):
            import_store(
                self.root,
                self.settings,
                self.issuer,
                self.key,
                failure_hook=fail,
            )
        with psycopg.connect(self.database_url) as connection:
            committed = connection.execute(
                "SELECT count(*) FROM file_import_units"
            ).fetchone()[0]
            profiles = connection.execute("SELECT count(*) FROM profiles").fetchone()[0]
        self.assertEqual((committed, profiles), (1, 1))

        recovered = import_store(self.root, self.settings, self.issuer, self.key)
        self.assertTrue(recovered.ok, recovered.public())
        with psycopg.connect(self.database_url) as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM file_import_units").fetchone(),
                (2,),
            )

    def test_shadow_detects_gap_stale_revision_projection_and_import_drift(self) -> None:
        self.assertTrue(
            import_store(self.root, self.settings, self.issuer, self.key).ok
        )
        profile_id = self.fixture["profiles"][0]
        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                "ALTER TABLE collected_record_captures "
                "DISABLE TRIGGER collected_record_captures_immutable"
            )
            removable = connection.execute(
                """
                SELECT c.profile_id, c.id
                FROM collected_record_captures c
                JOIN collected_records r
                  ON r.profile_id = c.profile_id AND r.id = c.collected_record_id
                WHERE r.profile_id = %s AND r.record_kind = 'profile.full_name'
                """,
                (profile_id,),
            ).fetchone()
            connection.execute(
                "DELETE FROM observation_projection_heads "
                "WHERE profile_id = %s AND current_capture_id = %s",
                removable,
            )
            connection.execute(
                "DELETE FROM collected_record_captures WHERE profile_id = %s AND id = %s",
                removable,
            )
            connection.execute(
                "ALTER TABLE collected_record_captures "
                "ENABLE TRIGGER collected_record_captures_immutable"
            )
            connection.execute(
                "UPDATE training_sessions SET app_revision = app_revision + 1 "
                "WHERE profile_id = %s AND ownership = 'app'",
                (profile_id,),
            )
            connection.execute(
                "UPDATE training_sessions SET sport = 'synthetic-drift' "
                "WHERE profile_id = %s AND ownership = 'collected'",
                (profile_id,),
            )
            source_id = connection.execute(
                "SELECT id FROM source_connections WHERE profile_id = %s",
                (profile_id,),
            ).fetchone()[0]
            extra_record, extra_capture = uuid4(), uuid4()
            connection.execute(
                """
                INSERT INTO collected_records (
                    profile_id, id, source_connection_id, record_kind, source_key
                ) VALUES (%s, %s, %s, 'synthetic.extra', 'synthetic-extra')
                """,
                (profile_id, extra_record, source_id),
            )
            connection.execute(
                """
                INSERT INTO collected_record_captures (
                    profile_id, id, collected_record_id, content_hash, payload
                ) VALUES (%s, %s, %s, %s, '{}'::jsonb)
                """,
                (profile_id, extra_capture, extra_record, b"z" * 32),
            )

        report = reconcile_store(self.root, self.settings, self.issuer)
        codes = {(issue.category, issue.code) for issue in report.issues}
        self.assertFalse(report.ok)
        self.assertIn(("capture", "gap"), codes)
        self.assertIn(("capture", "import_drift"), codes)
        self.assertIn(("app_record", "stale_revision"), codes)
        self.assertIn(("collected_session", "projection_mismatch"), codes)
        rendered = json.dumps(report.public(), sort_keys=True)
        for forbidden in self.fixture["forbidden"]:
            self.assertNotIn(forbidden, rendered)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn(self.application_url, rendered)
        self.assertNotIn("synthetic-drift", rendered)

    def test_inventory_detects_duplicates_bad_references_and_cross_profile_isolation(self) -> None:
        first_id, second_id = self.fixture["profiles"]
        activities_path = self.root / "profiles" / first_id.hex / "derived" / "activities.json"
        activities = json.loads(activities_path.read_text(encoding="utf-8"))
        activities.append(dict(activities[0]))
        activities[0]["duration_s"] = 999
        activities_path.write_text(json.dumps(activities), encoding="utf-8")

        first_plan_path = next(
            (self.root / "profiles" / first_id.hex / "app" / "plans").glob("*.json")
        )
        second_goal_path = next(
            (self.root / "profiles" / second_id.hex / "app" / "goal_events").glob("*.json")
        )
        first_plan = json.loads(first_plan_path.read_text(encoding="utf-8"))
        second_goal = json.loads(second_goal_path.read_text(encoding="utf-8"))
        for revision in first_plan["content"]["revisions"]:
            revision["plan"]["goal_events"] = [
                {
                    "goal_event_id": second_goal["id"],
                    "goal_event_revision": 1,
                },
                {
                    "goal_event_id": f"app:{uuid4().hex}",
                    "goal_event_revision": 1,
                },
            ]
        first_plan_path.write_text(json.dumps(first_plan), encoding="utf-8")

        inventory = inventory_store(self.root, self.issuer)
        codes = {(issue.category, issue.code) for issue in inventory.all_issues()}
        self.assertIn(("projection", "duplicate_identity"), codes)
        self.assertIn(("projection", "projection_mismatch"), codes)
        self.assertIn(("isolation", "cross_profile_reference"), codes)
        self.assertIn(("reference", "missing_target"), codes)
        report = json.dumps(
            {
                "issues": [issue.public() for issue in inventory.all_issues()],
                "counts": inventory.counts(),
            },
            sort_keys=True,
        )
        for forbidden in self.fixture["forbidden"]:
            self.assertNotIn(forbidden, report)
        self.assertNotIn(str(self.root), report)

    def test_cli_requires_explicit_absolute_inputs_and_redacts_failures(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(
                [
                    "shadow",
                    "--source-root",
                    "relative-store",
                    "--database-url",
                    self.application_url,
                    "--clerk-issuer",
                    self.issuer,
                ]
            )
        self.assertEqual(result, 2)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertIn("source_must_be_absolute", output)
        self.assertNotIn("relative-store", output)
        self.assertNotIn(self.application_url, output)

        key_path = Path(self.temporary.name).resolve() / "target.key"
        key_path.write_bytes(self.key)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rejected = main(
                [
                    "import",
                    "--source-root",
                    str(self.root),
                    "--database-url",
                    self.application_url,
                    "--clerk-issuer",
                    self.issuer,
                    "--encryption-key-file",
                    str(key_path),
                ]
            )
        self.assertEqual(rejected, 2)
        self.assertIn(
            "maintenance_owner_required", stdout.getvalue() + stderr.getvalue()
        )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            imported = main(
                [
                    "import",
                    "--source-root",
                    str(self.root),
                    "--database-url",
                    self.database_url,
                    "--clerk-issuer",
                    self.issuer,
                    "--encryption-key-file",
                    str(key_path),
                ]
            )
        self.assertEqual(imported, 0, stderr.getvalue())
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(str(self.root), output)
        self.assertNotIn(str(key_path), output)
        self.assertNotIn(self.application_url, output)
        self.assertNotIn(self.database_url, output)
        for forbidden in self.fixture["forbidden"]:
            self.assertNotIn(forbidden, output)


def _legacy_fixture(root: Path) -> dict[str, object]:
    profile_ids = [uuid4(), uuid4()]
    subjects = [f"synthetic-subject-{uuid4().hex}", f"synthetic-subject-{uuid4().hex}"]
    registry: dict[str, object] = {"schema_version": 1, "profiles": {}}
    forbidden: list[str] = []
    for index, (profile_id, subject) in enumerate(zip(profile_ids, subjects, strict=True), start=1):
        display = f"Synthetic Profile {index}"
        created = f"2026-08-0{index}T00:00:00Z"
        registry["profiles"][profile_id.hex] = {
            "clerk_subject": subject,
            "display_name": display,
            "created_at": created,
            "seq": index,
        }
        forbidden.extend([profile_id.hex, subject, display])
        profile_root = root / "profiles" / profile_id.hex
        activity_key = str(8000 + index)
        local_date = f"2026-08-0{index}"
        raw_daily = profile_root / "raw" / "daily" / "2026" / "08" / local_date
        _write_json(
            raw_daily / "stats.json",
            {
                "totalSteps": 1000 + index,
                "totalDistanceMeters": 2000 + index,
                "activeKilocalories": float(300 + index),
                "restingHeartRate": 50 + index,
            },
        )
        _write_json(raw_daily / "_complete.json", {"collected_at": local_date})
        raw_activity = profile_root / "raw" / "activities" / "2026" / "08" / activity_key
        _write_json(
            raw_activity / "summary.json",
            {
                "activityName": f"Synthetic Session {index}",
                "activityTypeDTO": {"typeKey": "running"},
                "summaryDTO": {
                    "startTimeLocal": f"{local_date}T06:00:00",
                    "distance": 5000 + index,
                    "duration": 1800 + index,
                },
            },
        )
        _write_json(
            profile_root / "raw" / "profile" / local_date / "full_name.json",
            {"fullName": f"Synthetic Athlete {index}"},
        )
        _write_json(
            profile_root / "derived" / "daily" / f"{local_date}.json",
            {
                "date": local_date,
                "steps": 1000 + index,
                "distance_m": 2000 + index,
                "active_kcal": float(300 + index),
                "resting_hr": 50 + index,
                "min_hr": None,
                "max_hr": None,
                "avg_stress": None,
                "intensity_min_moderate": None,
                "intensity_min_vigorous": None,
                "floors_ascended": None,
                "sleep_seconds": None,
                "sleep_score": None,
                "deep_sleep_s": None,
                "light_sleep_s": None,
                "rem_sleep_s": None,
                "awake_s": None,
                "hrv_avg": None,
                "hrv_status": None,
                "training_readiness": None,
                "training_readiness_level": None,
                "training_status": False,
                "vo2max_running": None,
            },
        )
        _write_json(
            profile_root / "derived" / "activities.json",
            [
                {
                    "activity_id": int(activity_key),
                    "name": f"Synthetic Session {index}",
                    "type": "running",
                    "start_local": f"{local_date}T06:00:00",
                    "distance_m": 5000 + index,
                    "duration_s": 1800 + index,
                    "avg_hr": None,
                    "max_hr": None,
                    "elevation_gain_m": None,
                    "avg_speed_mps": None,
                    "calories": None,
                    "training_effect_aerobic": None,
                    "training_effect_anaerobic": None,
                }
            ],
        )
        session_id, goal_id, plan_id, planned_id = (uuid4() for _ in range(4))
        forbidden.extend([session_id.hex, goal_id.hex, plan_id.hex, planned_id.hex, activity_key])
        app_session = {
            "id": f"app:{session_id.hex}",
            "origin": "app_record",
            "provenance": {"source": "manual"},
            "created_at": f"{local_date}T07:00:00Z",
            "updated_at": f"{local_date}T08:00:00Z",
            "revision": 2,
            "content": {
                "sport": "strength",
                "session_type": None,
                "local_date": local_date,
                "local_start": None,
                "timing_precision": "date_only",
                "time_zone": None,
                "utc_offset": None,
                "title": f"Synthetic Manual {index}",
                "notes": None,
                "duration": None,
                "distance": None,
                "session_rpe": None,
                "loads": None,
            },
        }
        _write_json(profile_root / "app" / "sessions" / f"{session_id.hex}.json", app_session)
        goal = {
            "id": f"app:{goal_id.hex}",
            "origin": "app_record",
            "provenance": {"source": "manual"},
            "created_at": f"{local_date}T07:00:00Z",
            "updated_at": f"{local_date}T08:00:00Z",
            "revision": 2,
            "content": {
                "name": f"Synthetic Goal {index}",
                "sport": "running",
                "priority": "primary",
                "status": "scheduled",
                "local_date": "2026-12-01",
                "local_start": None,
                "timing_precision": "date_only",
                "time_zone": None,
                "utc_offset": None,
                "distance": None,
                "goal": None,
                "outcome": None,
                "notes": None,
            },
        }
        _write_json(profile_root / "app" / "goal_events" / f"{goal_id.hex}.json", goal)
        planned_v1 = {
            "id": planned_id.hex,
            "scheduled_date": "2026-11-01",
            "sport": "strength",
            "session_type": None,
            "prescription": "Synthetic prescription",
            "target_duration_seconds": None,
            "target_distance_meters": None,
            "effort_guidance": None,
            "disposition": "scheduled",
            "matches": [],
            "fulfilment_note": None,
        }
        planned_v2 = {
            **planned_v1,
            "disposition": "fulfilled",
            "matches": [f"app:{session_id.hex}"],
        }
        plan = {
            "id": f"app:{plan_id.hex}",
            "origin": "app_record",
            "provenance": {"source": "manual"},
            "created_at": f"{local_date}T07:00:00Z",
            "updated_at": f"{local_date}T08:00:00Z",
            "revision": 2,
            "content": {
                "revisions": [
                    {
                        "revision": 1,
                        "kind": "create",
                        "recorded_at": f"{local_date}T07:00:00Z",
                        "recorded_date": local_date,
                        "reason": "Synthetic create",
                        "effective_from": None,
                        "plan": {
                            "name": f"Synthetic Plan {index}",
                            "starts_on": "2026-10-01",
                            "ends_on": "2026-12-31",
                            "status": "draft",
                            "goal_events": [
                                {
                                    "goal_event_id": f"app:{goal_id.hex}",
                                    "goal_event_revision": 1,
                                }
                            ],
                            "constraints": [],
                            "planned_sessions": [planned_v1],
                        },
                    },
                    {
                        "revision": 2,
                        "kind": "fulfilment_correction",
                        "recorded_at": f"{local_date}T08:00:00Z",
                        "recorded_date": local_date,
                        "reason": "Synthetic match",
                        "effective_from": None,
                        "plan": {
                            "name": f"Synthetic Plan {index}",
                            "starts_on": "2026-10-01",
                            "ends_on": "2026-12-31",
                            "status": "draft",
                            "goal_events": [
                                {
                                    "goal_event_id": f"app:{goal_id.hex}",
                                    "goal_event_revision": 1,
                                }
                            ],
                            "constraints": [],
                            "planned_sessions": [planned_v2],
                        },
                    },
                ]
            },
        }
        _write_json(profile_root / "app" / "plans" / f"{plan_id.hex}.json", plan)
        _write_json(
            profile_root / "index" / "state.json",
            {"last_run": f"{local_date}T10:00:00", "seen_activities": [int(activity_key)]},
        )
        _write_json(
            profile_root / "index" / "collector-health.json",
            {
                "schema_version": 1,
                "run": {
                    "outcome": "successful",
                    "attempted_at": f"{local_date}T09:00:00Z",
                    "finished_at": f"{local_date}T10:00:00Z",
                    "last_success_at": f"{local_date}T10:00:00Z",
                },
                "domains": {},
            },
        )
        encrypted = Fernet(Fernet.generate_key()).encrypt(
            f"synthetic-legacy-material-{index}".encode()
        )
        credentials = profile_root / "secrets" / "garmin.enc"
        credentials.parent.mkdir(parents=True, exist_ok=True)
        credentials.write_bytes(encrypted)
        tokens = profile_root / "garmin-tokens"
        tokens.mkdir(parents=True, exist_ok=True)
        (tokens / "oauth.json").write_text(
            json.dumps({"synthetic": index}), encoding="utf-8"
        )
        job_key = __import__("hashlib").sha256(subject.encode()).hexdigest()[:32]
        _write_json(
            root / "jobs" / f"{job_key}.json",
            {
                "schema_version": 1,
                "state": "succeeded",
                "action": "refresh",
                "started_at": f"{local_date}T09:00:00Z",
                "finished_at": f"{local_date}T10:00:00Z",
                "updated_at": f"{local_date}T10:00:00Z",
            },
        )
    _write_json(root / "profiles" / "registry.json", registry)
    # profiles, sources, captures (4 each), sessions, observations (10 each),
    # app sessions, goals, plans, plan revisions, evidence.
    expected_counts = (2, 2, 8, 2, 20, 2, 2, 2, 4, 2)
    return {
        "profiles": profile_ids,
        "forbidden": forbidden,
        "expected_counts": expected_counts,
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
