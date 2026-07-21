"""Safety checks for the disposable PostgreSQL integration suite."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import psycopg


class UnsafeTestDatabase(RuntimeError):
    """The integration suite was pointed at a non-disposable database."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise UnsafeTestDatabase(f"{name} is required; use scripts/test-postgres")
    return value


def test_database_url() -> str:
    url = _required("GARMIN_COACH_TEST_DATABASE_URL")
    database = _required("GARMIN_COACH_TEST_DATABASE_NAME")
    password = _required("GARMIN_COACH_TEST_DATABASE_PASSWORD")
    port = _required("GARMIN_COACH_TEST_DATABASE_PORT")
    run_id = _required("GARMIN_COACH_TEST_RUN_ID")
    try:
        parsed = urlsplit(url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise UnsafeTestDatabase("PostgreSQL test URL is invalid") from exc
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username != "coach_test"
        or parsed.password != password
        or parsed.path != f"/{database}"
        or database != f"coach_test_{run_id}"
        or str(parsed_port) != port
        or parsed.query
        or parsed.fragment
    ):
        raise UnsafeTestDatabase(
            "PostgreSQL integration tests require the script-owned loopback database"
        )

    try:
        with psycopg.connect(url) as connection:
            marker = connection.execute(
                "SELECT run_id FROM garmin_coach_test_harness"
            ).fetchone()
    except Exception as exc:
        raise UnsafeTestDatabase(
            "PostgreSQL integration test harness marker is unavailable"
        ) from exc
    if marker != (run_id,):
        raise UnsafeTestDatabase(
            "PostgreSQL integration test harness marker does not match"
        )
    return url
