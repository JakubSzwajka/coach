from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

import psycopg

from tests.postgres.support import test_database

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class PostgreSQLRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.database_url = test_database().migration_url

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

    def test_upgrade_is_idempotent_and_records_revision(self) -> None:
        first = self._migration("upgrade")
        second = self._migration("upgrade")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        with psycopg.connect(self.database_url) as connection:
            revision = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        self.assertEqual(revision, ("0004_ingestion_collection_state",))

    def test_current_command_reports_the_applied_revision(self) -> None:
        self.assertEqual(self._migration("upgrade").returncode, 0)

        current = self._migration("current")

        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertIn("0004_ingestion_collection_state", current.stdout)

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
