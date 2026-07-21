from __future__ import annotations

import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from psycopg.types.json import Jsonb

from coach.postgres import DatabaseSettings
from coach.postgres._capture_store import (
    CaptureInput,
    CaptureStore,
    CaptureStoreError,
)
from coach.postgres.encryption import EncryptedBlob
from coach.postgres.migrate import migrate
from tests.postgres.support import test_database


class ProfileSourceCaptureSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database = test_database()
        cls.database_url = database.application_url
        cls.migration_url = database.migration_url
        cls.settings = DatabaseSettings.from_url(cls.database_url)
        cls.encryption_key = Fernet.generate_key()
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
        self.store = CaptureStore(self.settings)

    def _profile_and_connection(self, suffix: str = "a"):
        profile_id = self.store.ensure_profile(
            "https://identity.example.test", f"synthetic_{suffix}"
        )
        connection_id = self.store.ensure_source_connection(
            profile_id,
            "synthetic-provider",
            f"connection-{suffix}",
            encrypted_credentials=EncryptedBlob.encrypt(
                f"synthetic-credentials-{suffix}".encode(), self.encryption_key
            ),
            encrypted_tokens=EncryptedBlob.encrypt(
                f"synthetic-tokens-{suffix}".encode(), self.encryption_key
            ),
        )
        return profile_id, connection_id

    def test_profile_binding_is_idempotent_and_isolated(self) -> None:
        first = self.store.ensure_profile(
            "https://identity.example.test", "synthetic_a"
        )
        repeated = self.store.ensure_profile(
            "https://identity.example.test", "synthetic_a"
        )
        second = self.store.ensure_profile(
            "https://identity.example.test", "synthetic_b"
        )

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, second)
        with psycopg.connect(self.database_url) as connection:
            count = connection.execute(
                "SELECT count(*) FROM profiles"
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_concurrent_profile_binding_converges_without_orphans(self) -> None:
        barrier = Barrier(2)

        def bind():
            barrier.wait()
            return CaptureStore(self.settings).ensure_profile(
                "https://identity.example.test", "synthetic_concurrent"
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: bind(), range(2)))

        self.assertEqual(results[0], results[1])
        with psycopg.connect(self.database_url) as connection:
            count = connection.execute(
                "SELECT count(*) FROM profiles"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_one_profile_per_clerk_identity_is_enforced_by_postgresql(self) -> None:
        self.store.ensure_profile(
            "https://identity.example.test", "synthetic_a"
        )

        with self.assertRaises(psycopg.errors.UniqueViolation):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    """
                    INSERT INTO profiles (id, clerk_issuer, clerk_subject)
                    VALUES (%s, %s, %s)
                    """,
                    (
                        uuid4(),
                        "https://identity.example.test",
                        "synthetic_a",
                    ),
                )

    def test_profile_cannot_exist_without_one_identity(self) -> None:
        with self.assertRaises(psycopg.errors.NotNullViolation):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "INSERT INTO profiles (id) VALUES (%s)",
                    (uuid4(),),
                )

    def test_profile_identity_cannot_be_rebound_or_replaced(self) -> None:
        profile_id = self.store.ensure_profile(
            "https://identity.example.test", "synthetic_a"
        )

        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "UPDATE profiles SET clerk_subject = 'synthetic_other'"
                )
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with psycopg.connect(self.database_url) as connection:
                connection.execute("DELETE FROM profiles WHERE id = %s", (profile_id,))

    def test_source_connection_keeps_only_ciphertext_bytes(self) -> None:
        profile_id, connection_id = self._profile_and_connection()

        with psycopg.connect(self.database_url) as connection:
            stored = connection.execute(
                """
                SELECT encrypted_credentials, encrypted_tokens, state
                FROM source_connections
                WHERE profile_id = %s AND id = %s
                """,
                (profile_id, connection_id),
            ).fetchone()

        self.assertEqual(stored[2], "disconnected")
        self.assertTrue(stored[0].startswith(b"gc1:"))
        self.assertTrue(stored[1].startswith(b"gc1:"))
        self.assertNotIn(b"synthetic-credentials", stored[0])
        self.assertNotIn(b"synthetic-tokens", stored[1])
        self.assertEqual(
            EncryptedBlob.from_envelope(
                stored[0], self.encryption_key
            ).decrypt(self.encryption_key),
            b"synthetic-credentials-a",
        )
        with self.assertRaises(InvalidToken):
            EncryptedBlob.from_envelope(stored[1], Fernet.generate_key())

    def test_source_connection_rejects_plain_bytes_as_encrypted_material(self) -> None:
        profile_id = self.store.ensure_profile(
            "https://identity.example.test", "synthetic_a"
        )

        with self.assertRaisesRegex(CaptureStoreError, "EncryptedBlob"):
            self.store.ensure_source_connection(
                profile_id,
                "synthetic-provider",
                "connection-a",
                encrypted_credentials=b"plaintext" * 16,  # type: ignore[arg-type]
            )

    def test_source_connection_does_not_discard_conflicting_encrypted_material(self) -> None:
        profile_id, _ = self._profile_and_connection()

        with self.assertRaisesRegex(CaptureStoreError, "different encrypted material"):
            self.store.ensure_source_connection(
                profile_id,
                "synthetic-provider",
                "connection-a",
                encrypted_credentials=EncryptedBlob.encrypt(
                    b"different-synthetic-material", self.encryption_key
                ),
            )

    def test_identical_capture_is_idempotent_and_changed_capture_appends(self) -> None:
        profile_id, connection_id = self._profile_and_connection()
        original = CaptureInput(
            record_kind="daily-observation",
            source_key="synthetic-date",
            payload={"value": 7, "unknown": None},
            provenance={"endpoint": "synthetic"},
        )

        first = self.store.ingest_captures(profile_id, connection_id, [original])
        repeated = self.store.ingest_captures(
            profile_id,
            connection_id,
            [
                CaptureInput(
                    record_kind="daily-observation",
                    source_key="synthetic-date",
                    payload={"unknown": None, "value": 7},
                    provenance={"endpoint": "synthetic"},
                )
            ],
        )
        changed = self.store.ingest_captures(
            profile_id,
            connection_id,
            [
                CaptureInput(
                    record_kind="daily-observation",
                    source_key="synthetic-date",
                    payload={"value": 8, "unknown": None},
                )
            ],
        )

        self.assertEqual((first.inserted, first.unchanged), (1, 0))
        self.assertEqual((repeated.inserted, repeated.unchanged), (0, 1))
        self.assertEqual((changed.inserted, changed.unchanged), (1, 0))
        with psycopg.connect(self.database_url) as connection:
            rows = connection.execute(
                "SELECT payload FROM collected_record_captures"
            ).fetchall()
        self.assertCountEqual(
            [row[0] for row in rows],
            [
                {"value": 7, "unknown": None},
                {"value": 8, "unknown": None},
            ],
        )

    def test_content_hash_uses_canonical_parsed_json(self) -> None:
        profile_id, connection_id = self._profile_and_connection()
        self.store.ingest_captures(
            profile_id,
            connection_id,
            [
                CaptureInput(
                    record_kind="summary",
                    source_key="synthetic-key",
                    payload={"b": 2, "a": 1},
                )
            ],
        )

        with psycopg.connect(self.database_url) as connection:
            content_hash = connection.execute(
                "SELECT content_hash FROM collected_record_captures"
            ).fetchone()[0]

        self.assertEqual(content_hash, hashlib.sha256(b'{"a":1,"b":2}').digest())
        self.assertEqual(len(content_hash), 32)

    def test_equivalent_json_numbers_share_one_content_hash(self) -> None:
        profile_id, connection_id = self._profile_and_connection()

        first = self.store.ingest_captures(
            profile_id,
            connection_id,
            [
                CaptureInput(
                    record_kind="summary",
                    source_key="numeric",
                    payload={"whole": 1.0, "zero": -0.0},
                )
            ],
        )
        repeated = self.store.ingest_captures(
            profile_id,
            connection_id,
            [
                CaptureInput(
                    record_kind="summary",
                    source_key="numeric",
                    payload={"whole": 1, "zero": 0},
                )
            ],
        )

        self.assertEqual((first.inserted, repeated.unchanged), (1, 1))

    def test_same_source_identity_is_isolated_between_profiles(self) -> None:
        first_profile, first_connection = self._profile_and_connection("a")
        second_profile, second_connection = self._profile_and_connection("b")
        capture = CaptureInput(
            record_kind="summary",
            source_key="shared-synthetic-key",
            payload={"value": 1},
        )

        self.store.ingest_captures(first_profile, first_connection, [capture])
        self.store.ingest_captures(second_profile, second_connection, [capture])

        with psycopg.connect(self.database_url) as connection:
            grouped = connection.execute(
                """
                SELECT profile_id, count(*)
                FROM collected_record_captures
                GROUP BY profile_id
                """
            ).fetchall()
        self.assertEqual({profile_id for profile_id, _ in grouped}, {
            first_profile,
            second_profile,
        })
        self.assertTrue(all(count == 1 for _, count in grouped))

    def test_cross_profile_connection_is_rejected(self) -> None:
        first_profile, _ = self._profile_and_connection("a")
        _, second_connection = self._profile_and_connection("b")

        with self.assertRaisesRegex(
            CaptureStoreError, "source connection is not available"
        ):
            self.store.ingest_captures(
                first_profile,
                second_connection,
                [
                    CaptureInput(
                        record_kind="summary",
                        source_key="synthetic-key",
                        payload={},
                    )
                ],
            )

    def test_database_failure_rolls_back_the_whole_transaction(self) -> None:
        profile_id, connection_id = self._profile_and_connection()
        record_id = uuid4()

        with self.assertRaises(psycopg.errors.CheckViolation):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    """
                    INSERT INTO collected_records (
                        profile_id, id, source_connection_id, record_kind, source_key
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (profile_id, record_id, connection_id, "summary", "valid"),
                )
                connection.execute(
                    """
                    INSERT INTO collected_record_captures (
                        profile_id, id, collected_record_id, content_hash,
                        payload, provenance
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        profile_id,
                        uuid4(),
                        record_id,
                        hashlib.sha256(b"valid").digest(),
                        Jsonb({"value": 1}),
                        Jsonb({}),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO collected_record_captures (
                        profile_id, id, collected_record_id, content_hash,
                        payload, provenance
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        profile_id,
                        uuid4(),
                        record_id,
                        b"too-short",
                        Jsonb({"value": 2}),
                        Jsonb({}),
                    ),
                )

        with psycopg.connect(self.database_url) as connection:
            counts = connection.execute(
                "SELECT (SELECT count(*) FROM collected_records), "
                "(SELECT count(*) FROM collected_record_captures)"
            ).fetchone()
        self.assertEqual(counts, (0, 0))

    def test_invalid_batch_is_rejected_before_any_write(self) -> None:
        profile_id, connection_id = self._profile_and_connection()

        with self.assertRaises(CaptureStoreError):
            self.store.ingest_captures(
                profile_id,
                connection_id,
                [
                    CaptureInput(
                        record_kind="summary",
                        source_key="valid",
                        payload={"value": 1},
                    ),
                    CaptureInput(
                        record_kind="summary",
                        source_key="invalid",
                        payload={1: "non-string key"},
                    ),
                ],
            )

        with psycopg.connect(self.database_url) as connection:
            count = connection.execute(
                "SELECT count(*) FROM collected_record_captures"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_concurrent_overlapping_ingest_uses_stable_lock_order(self) -> None:
        profile_id, connection_id = self._profile_and_connection()
        first = CaptureInput(
            record_kind="summary",
            source_key="first",
            payload={"value": 1},
            source_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        second = CaptureInput(
            record_kind="summary",
            source_key="second",
            payload={"value": 2},
            source_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        barrier = Barrier(2)

        def ingest(batch):
            barrier.wait()
            return CaptureStore(self.settings).ingest_captures(
                profile_id, connection_id, batch
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            left = executor.submit(ingest, [first, second])
            right = executor.submit(ingest, [second, first])
            results = [left.result(timeout=10), right.result(timeout=10)]

        self.assertEqual(sum(result.inserted for result in results), 2)
        self.assertEqual(sum(result.unchanged for result in results), 2)
        with psycopg.connect(self.database_url) as connection:
            count = connection.execute(
                "SELECT count(*) FROM collected_record_captures"
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_capture_rows_reject_update_and_delete(self) -> None:
        profile_id, connection_id = self._profile_and_connection()
        self.store.ingest_captures(
            profile_id,
            connection_id,
            [
                CaptureInput(
                    record_kind="summary",
                    source_key="immutable",
                    payload={"value": 1},
                )
            ],
        )

        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "UPDATE collected_record_captures SET source_at = now()"
                )
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with psycopg.connect(self.database_url) as connection:
                connection.execute("DELETE FROM collected_record_captures")

    def test_parent_source_and_record_identity_cannot_rewrite_capture_meaning(self) -> None:
        profile_id, connection_id = self._profile_and_connection()
        self.store.ingest_captures(
            profile_id,
            connection_id,
            [
                CaptureInput(
                    record_kind="summary",
                    source_key="stable-identity",
                    payload={"value": 1},
                )
            ],
        )

        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "UPDATE source_connections SET provider = 'rewritten'"
                )
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "UPDATE collected_records SET record_kind = 'rewritten'"
                )

    def test_application_role_cannot_bypass_immutability_or_create_tables(self) -> None:
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(self.database_url) as connection:
                connection.execute("TRUNCATE TABLE collected_record_captures")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(self.database_url) as connection:
                connection.execute("CREATE TABLE privilege_escape (id integer)")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(self.database_url) as connection:
                connection.execute("CREATE TEMP TABLE profiles (id integer)")

    def test_database_rejects_cross_profile_record_relationship(self) -> None:
        first_profile, _ = self._profile_and_connection("a")
        _, second_connection = self._profile_and_connection("b")

        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    """
                    INSERT INTO collected_records (
                        profile_id, id, source_connection_id, record_kind, source_key
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        first_profile,
                        uuid4(),
                        second_connection,
                        "summary",
                        "cross-profile",
                    ),
                )


if __name__ == "__main__":
    unittest.main()
