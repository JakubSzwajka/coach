from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from datetime import date
from pathlib import Path
from uuid import uuid4

import psycopg

from coach.application import (
    CapturePointer,
    ClerkActor,
    CoachApplication,
    CollectedBatch,
    CollectedCapture,
    ControlledObservation,
    EnsureProfile,
    EnsureSourceConnection,
)
from coach.postgres import DatabaseSettings
from tests.postgres.support import test_database

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class PostgreSQLRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database = test_database()
        cls.database_url = database.migration_url
        cls.application_url = database.application_url

    def _migration(self, *arguments: str, database_url: str | None = None):
        environment = {
            key: os.environ[key]
            for key in ("HOME", "LANG", "LC_ALL", "PATH", "SSL_CERT_FILE", "TMPDIR")
            if key in os.environ
        }
        environment.update(
            {
                "GARMIN_COACH_MIGRATION_DATABASE_URL": (
                    database_url or self.database_url
                ),
                "GARMIN_COACH_DISABLE_DOTENV": "1",
                "PYTHONPATH": str(_PROJECT_ROOT),
            }
        )
        return subprocess.run(
            [sys.executable, "-m", "coach.postgres.migrate", *arguments],
            cwd=_PROJECT_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_0008_downgrade_refuses_to_discard_import_evidence(self) -> None:
        self.assertEqual(self._migration("upgrade").returncode, 0)
        profile_id = uuid4()
        with psycopg.connect(self.database_url) as connection:
            connection.execute("TRUNCATE TABLE profiles CASCADE")
            connection.execute(
                "INSERT INTO profiles (id, clerk_issuer, clerk_subject) "
                "VALUES (%s, %s, %s)",
                (profile_id, "https://identity.example.test", "import-evidence"),
            )
            connection.execute(
                """
                INSERT INTO file_import_units (
                    profile_id, importer_version, source_manifest_hash,
                    inventory_counts
                ) VALUES (%s, 1, %s, '{}'::jsonb)
                """,
                (profile_id, b"i" * 32),
            )

        refused = self._migration("downgrade", "0007_collection_job_admission")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("database migration failed", refused.stderr)
        self.assertNotIn("import-evidence", refused.stdout + refused.stderr)
        with psycopg.connect(self.database_url) as connection:
            self.assertEqual(
                connection.execute("SELECT version_num FROM alembic_version").fetchone(),
                ("0008_file_import_units",),
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM file_import_units").fetchone(),
                (1,),
            )
            # Production correctly blocks destruction of committed import
            # evidence. This disposable database cleanup deliberately disables
            # only that guard and restores it in the same transaction.
            connection.execute(
                "ALTER TABLE file_import_units "
                "DISABLE TRIGGER file_import_units_truncate_guard"
            )
            connection.execute("TRUNCATE TABLE profiles CASCADE")
            connection.execute(
                "ALTER TABLE file_import_units "
                "ENABLE TRIGGER file_import_units_truncate_guard"
            )

    def test_0005_downgrade_preserves_collected_state_and_refuses_app_loss(self) -> None:
        self.assertEqual(self._migration("upgrade").returncode, 0)
        profile_id, source_id, record_id, capture_id, session_id = (
            uuid4() for _ in range(5)
        )
        with psycopg.connect(self.database_url) as connection:
            connection.execute("TRUNCATE TABLE profiles CASCADE")
            connection.execute(
                "INSERT INTO profiles (id, clerk_issuer, clerk_subject) VALUES (%s, %s, %s)",
                (profile_id, "https://identity.example.test", "migration-synthetic"),
            )
            connection.execute(
                """
                INSERT INTO source_connections (
                    profile_id, id, provider, connection_key
                ) VALUES (%s, %s, 'synthetic', 'migration-source')
                """,
                (profile_id, source_id),
            )
            connection.execute(
                """
                INSERT INTO collected_records (
                    profile_id, id, source_connection_id, record_kind, source_key
                ) VALUES (%s, %s, %s, 'activity', 'migration-record')
                """,
                (profile_id, record_id, source_id),
            )
            connection.execute(
                """
                INSERT INTO collected_record_captures (
                    profile_id, id, collected_record_id, content_hash, payload
                ) VALUES (%s, %s, %s, %s, '{}'::jsonb)
                """,
                (profile_id, capture_id, record_id, b"x" * 32),
            )
            connection.execute(
                """
                INSERT INTO training_sessions (
                    profile_id, id, ownership, collected_record_id,
                    current_capture_id, local_date, timing_precision, sport
                ) VALUES (%s, %s, 'collected', %s, %s, DATE '2026-07-20',
                          'date_only', 'running')
                """,
                (profile_id, session_id, record_id, capture_id),
            )

        downgraded = self._migration("downgrade", "0004_ingestion_collection_state")
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        with psycopg.connect(self.database_url) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT ownership FROM training_sessions WHERE id = %s",
                    (session_id,),
                ).fetchone(),
                ("collected",),
            )
            self.assertIsNone(
                connection.execute(
                    """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'training_sessions'
                      AND column_name = 'app_revision'
                    """
                ).fetchone()
            )
        self.assertEqual(self._migration("upgrade").returncode, 0)

        with psycopg.connect(self.database_url) as connection:
            connection.execute(
                """
                INSERT INTO training_sessions (
                    profile_id, id, ownership, app_revision, local_date,
                    timing_precision, sport
                ) VALUES (%s, %s, 'app', 1, DATE '2026-07-20',
                          'date_only', 'running')
                """,
                (profile_id, uuid4()),
            )
        refused = self._migration("downgrade", "0004_ingestion_collection_state")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("database migration failed", refused.stderr)
        with psycopg.connect(self.database_url) as connection:
            self.assertEqual(
                connection.execute("SELECT version_num FROM alembic_version").fetchone(),
                ("0008_file_import_units",),
            )
            connection.execute("TRUNCATE TABLE profiles CASCADE")

    def test_0005_downgrade_waits_for_writer_then_refuses_committed_app_data(self) -> None:
        self.assertEqual(self._migration("upgrade").returncode, 0)
        profile_id = uuid4()
        goal_id = uuid4()
        with psycopg.connect(self.database_url) as connection:
            connection.execute("TRUNCATE TABLE profiles CASCADE")
            connection.execute(
                "INSERT INTO profiles (id, clerk_issuer, clerk_subject) VALUES (%s, %s, %s)",
                (profile_id, "https://identity.example.test", "downgrade-writer"),
            )

        writer = psycopg.connect(self.application_url)
        process: subprocess.Popen[str] | None = None
        try:
            writer.execute(
                """
                INSERT INTO goal_events (
                    profile_id, id, local_date, timing_precision,
                    sport, name, priority, status
                ) VALUES (%s, %s, DATE '2026-10-11', 'date_only',
                          'running', 'Synthetic writer race', 'primary', 'scheduled')
                """,
                (profile_id, goal_id),
            )
            environment = {
                key: os.environ[key]
                for key in (
                    "HOME", "LANG", "LC_ALL", "PATH", "SSL_CERT_FILE", "TMPDIR"
                )
                if key in os.environ
            }
            environment.update(
                {
                    "GARMIN_COACH_MIGRATION_DATABASE_URL": self.database_url,
                    "GARMIN_COACH_DISABLE_DOTENV": "1",
                    "PYTHONPATH": str(_PROJECT_ROOT),
                }
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "coach.postgres.migrate",
                    "downgrade",
                    "0004_ingestion_collection_state",
                ],
                cwd=_PROJECT_ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with psycopg.connect(self.database_url) as observer:
                    waiting = observer.execute(
                        """
                        SELECT count(*) FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND wait_event_type = 'Lock'
                          AND position('LOCK TABLE profiles' in query) > 0
                        """
                    ).fetchone()[0]
                if waiting:
                    break
                time.sleep(0.02)
            else:
                self.fail("downgrade did not contend on its authority-root lock")

            writer.commit()
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("database migration failed", stderr)
            self.assertNotIn("downgrade-writer", stdout + stderr)
        finally:
            writer.rollback()
            writer.close()
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

        with psycopg.connect(self.database_url) as connection:
            self.assertEqual(
                connection.execute("SELECT version_num FROM alembic_version").fetchone(),
                ("0008_file_import_units",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM goal_events WHERE profile_id = %s AND id = %s",
                    (profile_id, goal_id),
                ).fetchone(),
                ("Synthetic writer race",),
            )
            connection.execute("TRUNCATE TABLE profiles CASCADE")

    def test_upgrade_is_idempotent_and_records_revision(self) -> None:
        first = self._migration("upgrade")
        second = self._migration("upgrade")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        with psycopg.connect(self.database_url) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        self.assertEqual(revision, ("0008_file_import_units",))

    def test_current_command_reports_the_applied_revision(self) -> None:
        self.assertEqual(self._migration("upgrade").returncode, 0)

        current = self._migration("current")

        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertIn("0008_file_import_units", current.stdout)

    def test_0006_populated_downgrade_preserves_capture_authority_and_rebuilds(self) -> None:
        self.assertEqual(self._migration("upgrade").returncode, 0)
        with psycopg.connect(self.database_url) as connection:
            connection.execute("TRUNCATE TABLE profiles CASCADE")
        application = CoachApplication(DatabaseSettings.from_url(self.application_url))
        actor = ClerkActor("https://identity.example.test", "observation-downgrade")
        profile = application.execute(actor, EnsureProfile()).profile
        source = application.execute(
            actor, EnsureSourceConnection("synthetic", "observation-downgrade")
        )
        pointer = CapturePointer("daily.stats", "2026-07-20")
        batch = CollectedBatch(
            source=source,
            idempotency_key="observation-downgrade",
            captures=(CollectedCapture("daily.stats", "2026-07-20", {"steps": 12}),),
            observations=(
                ControlledObservation(
                    capture=pointer,
                    definition="daily_steps",
                    value_type="integer",
                    unit="count",
                    window_kind="calendar_day",
                    method="source_reported",
                    status="observed",
                    value=12,
                    local_date=date(2026, 7, 20),
                ),
            ),
        )
        application.ingest(profile, batch)

        downgraded = self._migration(
            "downgrade", "0005_transactional_app_records"
        )
        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        with psycopg.connect(self.database_url) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM collected_record_captures"
                ).fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM controlled_observations"
                ).fetchone(),
                (0,),
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM observation_definitions WHERE key = 'daily_steps'"
                ).fetchone()
            )

        reupgraded = self._migration("upgrade")
        self.assertEqual(reupgraded.returncode, 0, reupgraded.stderr)
        replay = application.ingest(profile, batch)
        self.assertEqual((replay.inserted, replay.unchanged), (0, 1))
        with psycopg.connect(self.database_url) as connection:
            rebuilt = connection.execute(
                "SELECT integer_value FROM controlled_observations "
                "WHERE definition_key = 'daily_steps'"
            ).fetchone()
            captures = connection.execute(
                "SELECT count(*) FROM collected_record_captures"
            ).fetchone()
        self.assertEqual(rebuilt, (12,))
        self.assertEqual(captures, (1,))

    def test_downgrade_and_reupgrade_round_trip(self) -> None:
        self.assertEqual(self._migration("upgrade").returncode, 0)

        downgraded = self._migration("downgrade", "base")

        self.assertEqual(downgraded.returncode, 0, downgraded.stderr)
        with psycopg.connect(self.database_url) as connection:
            table = connection.execute(
                "SELECT to_regclass('public.profiles')"
            ).fetchone()
        self.assertEqual(table, (None,))
        self.assertEqual(self._migration("upgrade").returncode, 0)

    def test_connection_failure_is_nonzero_and_redacts_password(self) -> None:
        password = "synthetic-secret-must-not-leak"
        failed = self._migration(
            "upgrade",
            database_url=(
                f"postgresql://coach_test:{password}@127.0.0.1:1/coach_test"
            ),
        )

        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn(password, failed.stdout + failed.stderr)
        self.assertIn("database migration failed", failed.stderr)


if __name__ == "__main__":
    unittest.main()
