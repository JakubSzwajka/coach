"""Safety checks for the disposable PostgreSQL integration suite."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

import psycopg


class UnsafeTestDatabase(RuntimeError):
    """The integration suite was pointed at a non-disposable database."""


@dataclass(frozen=True, slots=True)
class TestDatabase:
    migration_url: str
    application_url: str


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise UnsafeTestDatabase(f"{name} is required; use scripts/test-postgres")
    return value


def _parse(url: str) -> SplitResult:
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError as exc:
        raise UnsafeTestDatabase("PostgreSQL test URL is invalid") from exc
    if parsed.query or parsed.fragment:
        raise UnsafeTestDatabase("PostgreSQL test URL overrides are forbidden")
    return parsed


def test_database() -> TestDatabase:
    application_url = _required("GARMIN_COACH_TEST_DATABASE_URL")
    migration_url = _required("GARMIN_COACH_TEST_MIGRATION_DATABASE_URL")
    database = _required("GARMIN_COACH_TEST_DATABASE_NAME")
    application_password = _required("GARMIN_COACH_TEST_DATABASE_PASSWORD")
    migration_password = _required("GARMIN_COACH_TEST_MIGRATION_DATABASE_PASSWORD")
    port = _required("GARMIN_COACH_TEST_DATABASE_PORT")
    run_id = _required("GARMIN_COACH_TEST_RUN_ID")
    application = _parse(application_url)
    migration = _parse(migration_url)
    common_is_safe = (
        application.scheme in {"postgres", "postgresql"}
        and migration.scheme in {"postgres", "postgresql"}
        and application.hostname in {"127.0.0.1", "localhost", "::1"}
        and migration.hostname in {"127.0.0.1", "localhost", "::1"}
        and application.path == f"/{database}"
        and migration.path == f"/{database}"
        and database == f"coach_test_{run_id}"
        and str(application.port) == port
        and str(migration.port) == port
    )
    roles_are_safe = (
        application.username == "coach_test"
        and application.password == application_password
        and migration.username == "coach_test_admin"
        and migration.password == migration_password
    )
    if not common_is_safe or not roles_are_safe:
        raise UnsafeTestDatabase(
            "PostgreSQL integration tests require the script-owned loopback database"
        )

    try:
        with psycopg.connect(application_url) as connection:
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
    return TestDatabase(
        migration_url=migration_url,
        application_url=application_url,
    )
