from __future__ import annotations

import math
import unittest
from datetime import datetime, timezone

import psycopg

from coach.application import (
    AccessDenied,
    ApplicationUnavailable,
    ClerkActor,
    CoachApplication,
    CollectedBatch,
    CollectedCapture,
    EnsureProfile,
    EnsureSourceConnection,
    GetCollectionSummary,
    GetProfile,
    InvalidRequest,
    NotFound,
    StaleRevision,
    UpdateProfileDisplayName,
)
from coach.postgres import DatabaseSettings
from coach.postgres.migrate import migrate
from tests.postgres.support import test_database


class CoachApplicationPostgreSQLTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database = test_database()
        cls.database_url = database.application_url
        cls.migration_url = database.migration_url
        cls.settings = DatabaseSettings.from_url(cls.database_url)
        migrate(DatabaseSettings.from_url(cls.migration_url))

    def setUp(self) -> None:
        with psycopg.connect(self.migration_url) as connection:
            connection.execute(
                """
                TRUNCATE TABLE
                    collected_record_captures,
                    collected_records,
                    source_connections,
                    profiles
                """
            )
        self.application = CoachApplication(self.settings)
        self.actor_a = ClerkActor(
            "https://identity.example.test", "synthetic_a"
        )
        self.actor_b = ClerkActor(
            "https://identity.example.test", "synthetic_b"
        )

    def _profile_and_source(self, actor: ClerkActor, suffix: str):
        profile = self.application.execute(
            actor, EnsureProfile(display_name=f"Synthetic {suffix}")
        )
        source = self.application.execute(
            actor,
            EnsureSourceConnection(
                provider="synthetic-provider",
                connection_key=f"connection-{suffix}",
            ),
        )
        return profile.profile, source

    def test_actor_scoped_read_and_commands_do_not_accept_a_profile_id(self) -> None:
        created = self.application.execute(
            self.actor_a, EnsureProfile(display_name="Synthetic A")
        )
        repeated = self.application.execute(self.actor_a, EnsureProfile())

        self.assertEqual(repeated.profile, created.profile)
        self.assertEqual(
            self.application.read(self.actor_a, GetProfile()),
            created,
        )
        self.assertIsNone(self.application.read(self.actor_b, GetProfile()))

    def test_profile_command_uses_optimistic_revision_and_is_atomic(self) -> None:
        created = self.application.execute(
            self.actor_a, EnsureProfile(display_name="Synthetic original")
        )
        updated = self.application.execute(
            self.actor_a,
            UpdateProfileDisplayName(
                expected_revision=created.revision,
                display_name="Synthetic updated",
            ),
        )

        self.assertEqual(updated.revision, 1)
        self.assertEqual(updated.display_name, "Synthetic updated")
        with self.assertRaises(StaleRevision):
            self.application.execute(
                self.actor_a,
                UpdateProfileDisplayName(
                    expected_revision=created.revision,
                    display_name="Synthetic stale write",
                ),
            )
        current = self.application.read(self.actor_a, GetProfile())
        self.assertIsNotNone(current)
        self.assertEqual(current.display_name, "Synthetic updated")
        self.assertEqual(current.revision, 1)

    def test_representative_ingest_is_idempotent_and_reads_only_a_summary(self) -> None:
        profile, source = self._profile_and_source(self.actor_a, "a")
        capture = CollectedCapture(
            record_kind="daily-summary",
            source_key="synthetic-2026-01-09",
            payload={"metric": 42, "optional": None},
            source_at=datetime(2026, 1, 9, tzinfo=timezone.utc),
            provenance={"method": "synthetic"},
        )

        inserted = self.application.ingest(
            profile, CollectedBatch(source=source, captures=(capture,))
        )
        replayed = self.application.ingest(
            profile, CollectedBatch(source=source, captures=(capture,))
        )
        summary = self.application.read(
            self.actor_a, GetCollectionSummary()
        )

        self.assertEqual((inserted.inserted, inserted.unchanged), (1, 0))
        self.assertEqual((replayed.inserted, replayed.unchanged), (0, 1))
        self.assertIsNotNone(summary)
        self.assertEqual((summary.records, summary.captures), (1, 1))
        self.assertFalse(hasattr(summary, "payload"))

    def test_invalid_batch_rolls_back_before_any_write(self) -> None:
        profile, source = self._profile_and_source(self.actor_a, "a")
        batch = CollectedBatch(
            source=source,
            captures=(
                CollectedCapture("summary", "valid", {"value": 1}),
                CollectedCapture("summary", "invalid", {"value": math.nan}),
            ),
        )

        with self.assertRaises(InvalidRequest):
            self.application.ingest(profile, batch)

        summary = self.application.read(
            self.actor_a, GetCollectionSummary()
        )
        self.assertIsNotNone(summary)
        self.assertEqual((summary.records, summary.captures), (0, 0))

    def test_cross_profile_source_capability_is_denied(self) -> None:
        profile_a, _ = self._profile_and_source(self.actor_a, "a")
        _, source_b = self._profile_and_source(self.actor_b, "b")

        with self.assertRaises(AccessDenied):
            self.application.ingest(
                profile_a,
                CollectedBatch(
                    source=source_b,
                    captures=(
                        CollectedCapture("summary", "synthetic", {"value": 1}),
                    ),
                ),
            )

    def test_source_command_requires_an_actor_owned_profile(self) -> None:
        with self.assertRaises(NotFound):
            self.application.execute(
                self.actor_a,
                EnsureSourceConnection("synthetic-provider", "connection-a"),
            )

    def test_collected_payloads_are_accepted_only_by_internal_ingest(self) -> None:
        profile, source = self._profile_and_source(self.actor_a, "a")
        batch = CollectedBatch(
            source=source,
            captures=(CollectedCapture("summary", "synthetic", {"value": 1}),),
        )

        with self.assertRaises(InvalidRequest):
            self.application.execute(self.actor_a, batch)  # type: ignore[arg-type]
        self.assertEqual(self.application.ingest(profile, batch).inserted, 1)

    def test_errors_and_reprs_do_not_disclose_identity_payload_or_dsn(self) -> None:
        password = "synthetic-password-must-not-leak"
        unavailable = CoachApplication(
            DatabaseSettings.from_url(
                f"postgresql://coach:{password}@127.0.0.1:1/coach"
            )
        )
        capture = CollectedCapture(
            "summary", "private-source-key", {"private": "payload"}
        )

        with self.assertRaises(ApplicationUnavailable) as raised:
            unavailable.read(self.actor_a, GetProfile())

        rendered = repr(self.actor_a) + repr(capture) + str(raised.exception)
        self.assertNotIn("synthetic_a", rendered)
        self.assertNotIn("private-source-key", rendered)
        self.assertNotIn("payload", rendered)
        self.assertNotIn(password, rendered)


if __name__ == "__main__":
    unittest.main()
