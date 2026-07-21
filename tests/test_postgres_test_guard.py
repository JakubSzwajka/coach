from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests.postgres.support import UnsafeTestDatabase, test_database_url


class PostgreSQLTestGuardTest(unittest.TestCase):
    def _environment(self, url: str) -> dict[str, str]:
        return {
            "GARMIN_COACH_TEST_DATABASE_URL": url,
            "GARMIN_COACH_TEST_DATABASE_NAME": "coach_test_run123",
            "GARMIN_COACH_TEST_DATABASE_PASSWORD": "synthetic",
            "GARMIN_COACH_TEST_DATABASE_PORT": "5432",
            "GARMIN_COACH_TEST_RUN_ID": "run123",
        }

    def test_connection_field_query_override_is_rejected(self) -> None:
        environment = self._environment(
            "postgresql://coach_test:synthetic@127.0.0.1:5432/"
            "coach_test_run123?host=db.example"
        )

        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(UnsafeTestDatabase):
                test_database_url()

    def test_database_name_must_match_the_per_run_identity(self) -> None:
        environment = self._environment(
            "postgresql://coach_test:synthetic@127.0.0.1:5432/coach_test_production"
        )

        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(UnsafeTestDatabase):
                test_database_url()


if __name__ == "__main__":
    unittest.main()
