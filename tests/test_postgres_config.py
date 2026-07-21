from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from coach.postgres import DatabaseConfigurationError, DatabaseSettings


class DatabaseSettingsTest(unittest.TestCase):
    def test_missing_database_url_fails_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                DatabaseConfigurationError,
                "GARMIN_COACH_DATABASE_URL is required",
            ):
                DatabaseSettings.from_env()

    def test_invalid_url_error_does_not_echo_secret(self) -> None:
        secret = "do-not-echo-this"

        with self.assertRaises(DatabaseConfigurationError) as raised:
            DatabaseSettings.from_url(f"https://user:{secret}@db.example/coach")

        self.assertNotIn(secret, str(raised.exception))

    def test_sqlalchemy_url_selects_psycopg_three(self) -> None:
        settings = DatabaseSettings.from_url(
            "postgresql://coach:synthetic@127.0.0.1:5432/coach_test?sslmode=require"
        )

        self.assertEqual(
            settings.sqlalchemy_url,
            "postgresql+psycopg://coach:synthetic@127.0.0.1:5432/coach_test?sslmode=require",
        )
        self.assertNotIn("synthetic", repr(settings))


if __name__ == "__main__":
    unittest.main()
