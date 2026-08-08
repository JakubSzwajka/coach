from __future__ import annotations

import json
import os
import stat
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg
from cryptography.fernet import Fernet

from coach.application import (
    AccessDenied,
    ApplicationUnavailable,
    CapturePointer,
    ClerkActor,
    CoachApplication,
    CollectedBatch,
    CollectedCapture,
    CollectedTrainingSession,
    CollectionCheckpoint,
    CollectionFailed,
    ConfigureSourceCredentials,
    Conflict,
    ControlledObservation,
    EnsureProfile,
    EnsureSourceConnection,
    GetCollectedRecord,
    GetCollectionJob,
    GetSourceStatus,
    InvalidRequest,
    RequestCollection,
    RunCollectionJob,
    SecretBundle,
    SessionLoad,
    TransientCollectionError,
    RepairableAuthenticationError,
    _AuthenticatedTokenSession,
    _ExternalCollection,
    _store_graph,
)
from coach.postgres import DatabaseSettings
from coach.postgres._capture_store import CaptureStore
from coach.postgres._collection_store import _RunLease, _StoredJob, _StoredStatus
from coach.postgres.encryption import EncryptedBlob
from coach.postgres.migrate import migrate
from tests.postgres.support import test_database


class SyntheticAdapter:
    def __init__(self, external: _ExternalCollection) -> None:
        self.external = external
        self.paths: list[Path] = []
        self.cached_flags: list[bool] = []
        self.credentials: list[bytes | None] = []
        self.before_collect = None
        self.auth_error: Exception | None = None
        self.auth_errors: list[Exception | None] = []
        self.collection_error: Exception | None = None
        self.expected_cached_tokens = b"cached-token-bundle"
        self.prior_checkpoints: list[dict | None] = []

    def authenticate(self, token_directory, credentials, cached_tokens):
        self.paths.append(token_directory)
        self.cached_flags.append(cached_tokens)
        self.credentials.append(credentials)
        if stat.S_IMODE(os.stat(token_directory).st_mode) != 0o700:
            raise AssertionError("token directory is not private")
        if cached_tokens:
            token_file = token_directory / "tokens.bundle"
            if stat.S_IMODE(os.stat(token_file).st_mode) != 0o600:
                raise AssertionError("token file is not private")
            if token_file.read_bytes() != self.expected_cached_tokens:
                raise AssertionError("cached tokens were not preferred")
        if self.auth_errors:
            error = self.auth_errors.pop(0)
            if error is not None:
                raise error
        elif self.auth_error is not None:
            raise self.auth_error
        return _AuthenticatedTokenSession("synthetic-session", b"rotated-token-bundle")

    def collect(self, session, kind, prior_checkpoint):
        self.prior_checkpoints.append(
            dict(prior_checkpoint) if prior_checkpoint is not None else None
        )
        if self.before_collect is not None:
            self.before_collect()
        if self.collection_error is not None:
            raise self.collection_error
        return self.external


class IngestionAndCollectionPostgreSQLTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database = test_database()
        cls.database_url = database.application_url
        cls.migration_url = database.migration_url
        cls.settings = DatabaseSettings.from_url(cls.database_url)
        migrate(DatabaseSettings.from_url(cls.migration_url))

    def setUp(self) -> None:
        with psycopg.connect(self.migration_url) as connection:
            connection.execute("TRUNCATE TABLE profiles CASCADE")
        self.key = Fernet.generate_key()
        self.actor_a = ClerkActor("https://identity.example.test", "synthetic_a")
        self.actor_b = ClerkActor("https://identity.example.test", "synthetic_b")
        self.application = CoachApplication(
            self.settings, encryption_key=self.key
        )

    def _profile_source(
        self,
        actor: ClerkActor | None = None,
        suffix: str = "a",
        *,
        tokens: bool = False,
    ):
        actor = actor or self.actor_a
        profile = self.application.execute(actor, EnsureProfile())
        source = self.application.execute(
            actor,
            EnsureSourceConnection(
                "synthetic-provider",
                f"connection-{suffix}",
            ),
        )
        self.application.execute(
            actor,
            ConfigureSourceCredentials(
                source, SecretBundle(b"synthetic-credential-bundle")
            ),
        )
        if tokens:
            with psycopg.connect(self.migration_url) as connection:
                connection.execute(
                    """
                    UPDATE source_connections
                    SET encrypted_tokens = %s
                    WHERE connection_key = %s
                    """,
                    (
                        EncryptedBlob.encrypt(
                            b"cached-token-bundle", self.key
                        ).envelope,
                        f"connection-{suffix}",
                    ),
                )
        return profile.profile, source

    def _batch(
        self,
        source,
        *,
        key: str = "initial-batch",
        payload_value: int = 1,
        duration: int = 1200,
        heart_rate: int = 50,
        source_key: str = "synthetic-session",
    ) -> CollectedBatch:
        pointer = CapturePointer("training-session", source_key)
        return CollectedBatch(
            source=source,
            idempotency_key=key,
            captures=(
                CollectedCapture(
                    pointer.record_kind,
                    pointer.source_key,
                    {"revision": payload_value, "private_detail": "synthetic"},
                    source_at=datetime(2026, 1, 9, 8, tzinfo=timezone.utc),
                    provenance={"collector": "synthetic"},
                ),
            ),
            sessions=(
                CollectedTrainingSession(
                    capture=pointer,
                    local_date=date(2026, 1, 9),
                    local_start=datetime(2026, 1, 9, 8),
                    timing_precision="local_datetime",
                    time_zone="Etc/UTC",
                    utc_offset="+00:00",
                    sport="running",
                    session_type="easy",
                    title="Synthetic session",
                    session_rpe=4,
                    duration_value=duration,
                    duration_unit="seconds",
                    duration_basis="elapsed",
                    distance_value=5000,
                    distance_unit="metres",
                    loads=(
                        SessionLoad(
                            "source_load", "points", duration // 60,
                            "synthetic-provider",
                        ),
                    ),
                ),
            ),
            observations=(
                ControlledObservation(
                    capture=pointer,
                    definition="daily_resting_heart_rate",
                    value_type="integer",
                    unit="beats_per_minute",
                    window_kind="calendar_day",
                    method="source_reported",
                    status="observed",
                    value=heart_rate,
                    local_date=date(2026, 1, 9),
                    provenance={"quality": "source_reported"},
                ),
                ControlledObservation(
                    capture=pointer,
                    definition="daily_hrv_status",
                    value_type="text",
                    unit="status",
                    window_kind="calendar_day",
                    method="source_reported",
                    status="missing",
                    local_date=date(2026, 1, 9),
                ),
            ),
        )

    def test_canonical_batch_is_atomic_idempotent_and_updates_only_current_projection(self) -> None:
        profile, source = self._profile_source()
        original = self._batch(source)

        first = self.application.ingest(profile, original)
        replay = self.application.ingest(profile, original)
        changed = self._batch(
            source,
            key="changed-capture",
            payload_value=2,
            duration=1500,
            heart_rate=48,
        )
        changed_result = self.application.ingest(profile, changed)
        # A delayed replay of the old immutable capture cannot regress current.
        self.application.ingest(profile, original)

        projection = self.application.read(
            self.actor_a,
            GetCollectedRecord(source, "training-session", "synthetic-session"),
        )
        self.assertEqual((first.inserted, first.sessions, first.observations), (1, 1, 2))
        self.assertEqual((replay.inserted, replay.unchanged), (0, 1))
        self.assertEqual(changed_result.inserted, 1)
        self.assertIsNotNone(projection)
        self.assertEqual(projection.captures, 2)
        self.assertEqual(projection.session.session_rpe, 4)
        self.assertEqual(projection.session.local_start, datetime(2026, 1, 9, 8))
        self.assertEqual(projection.session.timing_precision, "local_datetime")
        self.assertEqual(projection.session.utc_offset, "+00:00")
        self.assertEqual(projection.session.title, "Synthetic session")
        self.assertEqual(projection.session.duration_value, Decimal("1500"))
        self.assertEqual(projection.session.duration_basis, "elapsed")
        self.assertEqual(projection.session.loads[0].value, Decimal("25"))
        self.assertEqual(projection.session.loads[0].source, "synthetic-provider")
        observations = {item.definition: item for item in projection.observations}
        self.assertEqual(observations["daily_resting_heart_rate"].value, 48)
        self.assertEqual(
            observations["daily_resting_heart_rate"].provenance,
            {"quality": "source_reported"},
        )
        self.assertEqual(observations["daily_hrv_status"].status, "missing")
        self.assertIsNone(observations["daily_hrv_status"].value)
        self.assertNotIn("recovery_score", observations)  # absent means unknown

    def test_concurrent_old_replay_cannot_regress_session_or_observations(self) -> None:
        profile, source = self._profile_source()
        original = self._batch(source, key="projection-original")
        changed = self._batch(
            source,
            key="projection-changed",
            payload_value=2,
            duration=1500,
            heart_rate=48,
        )
        unrelated = self._batch(
            source,
            key="projection-unrelated",
            payload_value=3,
            duration=900,
            heart_rate=60,
            source_key="unrelated-session",
        )
        self.application.ingest(profile, original)

        old_projection_reached = threading.Barrier(2)
        release_old_projection = threading.Event()
        changed_lock_attempted = threading.Event()

        class ProbedConnection:
            def __init__(self, connection, *, pause_old=False, signal_lock=False):
                self.connection = connection
                self.pause_old = pause_old
                self.signal_lock = signal_lock
                self.paused = False

            def execute(self, query, params=None):
                statement = str(query)
                if (
                    self.pause_old
                    and not self.paused
                    and "INSERT INTO training_sessions" in statement
                ):
                    # The old replay has already made its stale-session decision.
                    self.paused = True
                    old_projection_reached.wait(timeout=5)
                    if not release_old_projection.wait(timeout=10):
                        raise AssertionError("old projection replay was not released")
                if self.signal_lock and "FOR NO KEY UPDATE" in statement:
                    changed_lock_attempted.set()
                return self.connection.execute(query, params)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        def ingest_direct(batch, *, pause_old=False, signal_lock=False):
            captures, sessions, observations = _store_graph(batch)
            with psycopg.connect(self.database_url) as connection:
                probed = ProbedConnection(
                    connection,
                    pause_old=pause_old,
                    signal_lock=signal_lock,
                )
                return CaptureStore(self.settings).ingest_graph(
                    profile._id,
                    source._id,
                    idempotency_key=batch.idempotency_key,
                    captures=captures,
                    sessions=sessions,
                    observations=observations,
                    connection=probed,
                )

        with ThreadPoolExecutor(max_workers=3) as executor:
            delayed_old = executor.submit(
                ingest_direct, original, pause_old=True
            )
            try:
                try:
                    old_projection_reached.wait(timeout=5)
                except threading.BrokenBarrierError:
                    delayed_old.result(timeout=5)
                    raise
                concurrent_changed = executor.submit(
                    ingest_direct, changed, signal_lock=True
                )
                self.assertTrue(changed_lock_attempted.wait(timeout=5))

                # The per-record lock must not serialize an unrelated record.
                unrelated_result = executor.submit(
                    self.application.ingest, profile, unrelated
                ).result(timeout=5)
                self.assertEqual(unrelated_result.inserted, 1)
                self.assertFalse(concurrent_changed.done())
            finally:
                release_old_projection.set()
            delayed_old.result(timeout=10)
            concurrent_changed.result(timeout=10)

        projection = self.application.read(
            self.actor_a,
            GetCollectedRecord(
                source, "training-session", "synthetic-session"
            ),
        )
        observations = {item.definition: item for item in projection.observations}
        self.assertEqual(projection.session.duration_value, Decimal("1500"))
        self.assertEqual(projection.session.loads[0].value, Decimal("25"))
        self.assertEqual(observations["daily_resting_heart_rate"].value, 48)

    def test_collected_duration_accepts_source_reported_and_rejects_unknown_basis(self) -> None:
        profile, source = self._profile_source()
        source_reported = self._batch(
            source,
            key="source-reported-duration",
        )
        source_reported = replace(
            source_reported,
            sessions=(
                replace(
                    source_reported.sessions[0],
                    duration_basis="source_reported",
                ),
            ),
        )

        self.application.ingest(profile, source_reported)
        projection = self.application.read(
            self.actor_a,
            GetCollectedRecord(source, "training-session", "synthetic-session"),
        )
        self.assertEqual(projection.session.duration_basis, "source_reported")

        invalid = self._batch(
            source,
            key="invalid-duration-basis",
            payload_value=2,
        )
        invalid = replace(
            invalid,
            sessions=(replace(invalid.sessions[0], duration_basis="estimated"),),
        )
        with self.assertRaises(InvalidRequest):
            self.application.ingest(profile, invalid)

    def test_empty_observation_set_advances_head_and_old_replay_cannot_resurrect(self) -> None:
        profile, source = self._profile_source()
        observed = self._batch(source, key="observed-capture")
        self.application.ingest(profile, observed)

        empty = self._batch(
            source,
            key="empty-observation-capture",
            payload_value=2,
        )
        empty = replace(empty, observations=())
        result = self.application.ingest(profile, empty)
        projection = self.application.read(
            self.actor_a,
            GetCollectedRecord(source, "training-session", "synthetic-session"),
        )
        self.assertEqual(result.observations, 0)
        self.assertEqual(projection.observations, ())
        with psycopg.connect(self.database_url) as connection:
            current = connection.execute(
                """
                SELECT h.current_capture_id, c.payload ->> 'revision'
                FROM observation_projection_heads AS h
                JOIN collected_record_captures AS c
                  ON c.profile_id = h.profile_id
                 AND c.collected_record_id = h.collected_record_id
                 AND c.id = h.current_capture_id
                """
            ).fetchone()
        self.assertEqual(current[1], "2")

        self.application.ingest(profile, observed)
        replayed = self.application.read(
            self.actor_a,
            GetCollectedRecord(source, "training-session", "synthetic-session"),
        )
        self.assertEqual(replayed.observations, ())
        with psycopg.connect(self.database_url) as connection:
            replayed_head = connection.execute(
                "SELECT current_capture_id FROM observation_projection_heads"
            ).fetchone()[0]
        self.assertEqual(replayed_head, current[0])

    def test_health_session_observation_and_private_lifecycle_reprs_are_redacted(self) -> None:
        profile, source = self._profile_source(tokens=True)
        batch = self._batch(source)
        self.application.ingest(profile, batch)
        projection = self.application.read(
            self.actor_a,
            GetCollectedRecord(source, "training-session", "synthetic-session"),
        )
        requested = self.application.execute(
            self.actor_a, RequestCollection(source, "private-request-key")
        )
        job_view = self.application.read(
            self.actor_a, GetCollectionJob(requested.job)
        )
        status_view = self.application.read(
            self.actor_a, GetSourceStatus(source)
        )
        private_uuid = uuid4()
        private_date = datetime(2026, 1, 9, tzinfo=timezone.utc)
        stored_job = _StoredJob(
            private_uuid, private_uuid, private_uuid, "private-kind",
            "running", None, private_date, private_date, None,
        )
        stored_status = _StoredStatus(
            "needs_reconnect", "failed", "mfa_required",
            private_date, private_date, private_date, 42,
        )
        lease = _RunLease(
            stored_job, private_uuid, private_uuid,
            b"plaintext-credential", b"plaintext-token",
            "needs_reconnect", "mfa_required",
        )
        rendered = " ".join(
            repr(item)
            for item in (
                batch,
                batch.sessions[0],
                batch.sessions[0].capture,
                batch.sessions[0].loads[0],
                batch.observations[0],
                projection,
                projection.session,
                projection.session.loads[0],
                projection.observations[0],
                requested,
                job_view,
                status_view,
                stored_job,
                stored_status,
                lease,
            )
        )
        forbidden = (
            "synthetic-session",
            "2026-01-09",
            "source_load",
            "daily_resting_heart_rate",
            "source_reported",
            "plaintext-credential",
            "plaintext-token",
            str(private_uuid),
            "private-request-key",
            "private-kind",
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, rendered)

    def test_changed_semantics_for_one_idempotency_key_conflict_and_invalid_graph_rolls_back(self) -> None:
        profile, source = self._profile_source()
        self.application.ingest(profile, self._batch(source, key="stable-key"))

        with self.assertRaises(Conflict):
            self.application.ingest(
                profile,
                self._batch(source, key="stable-key", payload_value=2),
            )
        invalid = self._batch(source, key="invalid-graph", payload_value=3)
        invalid = CollectedBatch(
            source=invalid.source,
            captures=invalid.captures,
            idempotency_key=invalid.idempotency_key,
            sessions=invalid.sessions,
            observations=(
                *invalid.observations,
                ControlledObservation(
                    capture=CapturePointer("training-session", "synthetic-session"),
                    definition="recovery_score",
                    value_type="decimal",
                    unit="wrong-unit",
                    window_kind="instant",
                    method="source_reported",
                    status="observed",
                    value=7,
                    observed_at=datetime(2026, 1, 9, tzinfo=timezone.utc),
                ),
            ),
        )
        with self.assertRaises(InvalidRequest):
            self.application.ingest(profile, invalid)

        projection = self.application.read(
            self.actor_a,
            GetCollectedRecord(source, "training-session", "synthetic-session"),
        )
        self.assertEqual(projection.captures, 1)
        with psycopg.connect(self.database_url) as connection:
            batches = connection.execute("SELECT count(*) FROM ingest_batches").fetchone()[0]
        self.assertEqual(batches, 1)

    def test_ingest_validation_reads_migration_controlled_observation_definitions(self) -> None:
        profile, source = self._profile_source()
        with psycopg.connect(self.migration_url) as connection:
            connection.execute(
                "ALTER TABLE observation_definitions DISABLE TRIGGER "
                "observation_definitions_controlled"
            )
            connection.execute(
                "UPDATE observation_definitions SET unit = 'migration_score' "
                "WHERE key = 'recovery_score'"
            )
            connection.execute(
                "ALTER TABLE observation_definitions ENABLE TRIGGER "
                "observation_definitions_controlled"
            )
        try:
            pointer = CapturePointer("recovery", "synthetic-recovery")
            batch = CollectedBatch(
                source=source,
                idempotency_key="migration-definition",
                captures=(CollectedCapture("recovery", "synthetic-recovery", {"v": 1}),),
                observations=(
                    ControlledObservation(
                        capture=pointer,
                        definition="recovery_score",
                        value_type="decimal",
                        unit="migration_score",
                        window_kind="instant",
                        method="source_reported",
                        status="observed",
                        value=7,
                        observed_at=datetime(2026, 1, 9, tzinfo=timezone.utc),
                    ),
                ),
            )
            result = self.application.ingest(profile, batch)
            self.assertEqual(result.observations, 1)
            stale = CollectedBatch(
                source=source,
                idempotency_key="stale-static-definition",
                captures=(CollectedCapture("recovery", "another-recovery", {"v": 2}),),
                observations=(
                    ControlledObservation(
                        capture=CapturePointer("recovery", "another-recovery"),
                        definition="recovery_score",
                        value_type="decimal",
                        unit="score",
                        window_kind="instant",
                        method="source_reported",
                        status="observed",
                        value=8,
                        observed_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
                    ),
                ),
            )
            with self.assertRaises(InvalidRequest):
                self.application.ingest(profile, stale)
        finally:
            with psycopg.connect(self.migration_url) as connection:
                connection.execute(
                    "ALTER TABLE observation_definitions DISABLE TRIGGER "
                    "observation_definitions_controlled"
                )
                connection.execute(
                    "UPDATE observation_definitions SET unit = 'score' "
                    "WHERE key = 'recovery_score'"
                )
                connection.execute(
                    "ALTER TABLE observation_definitions ENABLE TRIGGER "
                    "observation_definitions_controlled"
                )

    def test_controlled_definitions_and_capture_relationships_are_database_enforced(self) -> None:
        profile, source = self._profile_source()
        self.application.ingest(profile, self._batch(source))

        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "UPDATE observation_definitions SET unit = 'other' "
                    "WHERE key = 'daily_resting_heart_rate'"
                )
        for assignment in (
            "unit = 'other'",
            "method = 'other'",
            "value_type = 'text'",
            "window_kind = 'instant'",
        ):
            with self.subTest(assignment=assignment):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    with psycopg.connect(self.database_url) as connection:
                        connection.execute(
                            f"UPDATE controlled_observations SET {assignment} "
                            "WHERE definition_key = 'daily_resting_heart_rate'"
                        )
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "UPDATE training_sessions SET current_capture_id = %s",
                    (uuid4(),),
                )
        with self.assertRaises(psycopg.errors.CheckViolation):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "UPDATE training_sessions SET session_rpe = 11"
                )
        with psycopg.connect(self.database_url) as connection:
            profile_id = connection.execute(
                "SELECT id FROM profiles WHERE clerk_subject = 'synthetic_a'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO training_sessions ("
                "profile_id, id, ownership, local_date, timing_precision, sport"
                ") VALUES (%s, %s, 'app', %s, 'date_only', 'strength')",
                (profile_id, uuid4(), date(2026, 1, 10)),
            )
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "DELETE FROM training_sessions WHERE ownership = 'collected'"
                )

    def test_shared_training_session_root_supports_app_vocabulary_and_strict_measurements(self) -> None:
        self.application.execute(self.actor_a, EnsureProfile())
        with psycopg.connect(self.database_url) as connection:
            profile_id = connection.execute(
                "SELECT id FROM profiles WHERE clerk_subject = 'synthetic_a'"
            ).fetchone()[0]
            session_id = uuid4()
            connection.execute(
                """
                INSERT INTO training_sessions (
                    profile_id, id, ownership, local_date, local_start,
                    timing_precision, time_zone, utc_offset, sport,
                    session_type, title, notes, duration_value, duration_unit,
                    duration_basis, distance_value, distance_unit, session_rpe
                ) VALUES (
                    %s, %s, 'app', %s, %s, 'local_datetime', %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    profile_id,
                    session_id,
                    date(2026, 2, 3),
                    datetime(2026, 2, 3, 18, 30),
                    "Europe/Warsaw",
                    "+01:00",
                    "bouldering",
                    "technique",
                    "Evening session",
                    "Synthetic note",
                    3600,
                    "seconds",
                    "active",
                    1.2,
                    "kilometres",
                    7,
                ),
            )
            connection.execute(
                """
                INSERT INTO session_loads (
                    profile_id, id, training_session_id, ownership,
                    collected_record_id, source_capture_id,
                    method, unit, value, source
                ) VALUES (%s, %s, %s, 'app', NULL, NULL, %s, %s, %s, %s)
                """,
                (
                    profile_id,
                    uuid4(),
                    session_id,
                    "session_rpe_load",
                    "arbitrary_units",
                    420,
                    "athlete_reported",
                ),
            )
            stored = connection.execute(
                """
                SELECT local_date, local_start, timing_precision, time_zone,
                       utc_offset, sport, session_type, title, notes,
                       duration_value, duration_unit, duration_basis,
                       distance_value, distance_unit, session_rpe,
                       collected_record_id, current_capture_id
                FROM training_sessions WHERE id = %s
                """,
                (session_id,),
            ).fetchone()
        self.assertEqual(stored[0], date(2026, 2, 3))
        self.assertEqual(stored[1], datetime(2026, 2, 3, 18, 30))
        self.assertEqual(stored[2:9], (
            "local_datetime", "Europe/Warsaw", "+01:00", "bouldering",
            "technique", "Evening session", "Synthetic note",
        ))
        self.assertEqual(stored[11], "active")
        self.assertEqual(stored[-2:], (None, None))

        invalid_statements = (
            (
                "duration-partial",
                "INSERT INTO training_sessions (profile_id, id, ownership, "
                "local_date, timing_precision, sport, duration_value) "
                "VALUES (%s, %s, 'app', %s, 'date_only', 'running', 1)",
            ),
            (
                "distance-zero",
                "INSERT INTO training_sessions (profile_id, id, ownership, "
                "local_date, timing_precision, sport, distance_value, "
                "distance_unit) VALUES (%s, %s, 'app', %s, 'date_only', "
                "'running', 0, 'metres')",
            ),
        )
        for label, statement in invalid_statements:
            with self.subTest(label=label):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    with psycopg.connect(self.database_url) as connection:
                        connection.execute(
                            statement,
                            (profile_id, uuid4(), date(2026, 2, 4)),
                        )

    def test_duration_basis_is_scoped_by_training_session_ownership(self) -> None:
        profile, source = self._profile_source()
        self.application.ingest(profile, self._batch(source))
        app_session_id = uuid4()
        with psycopg.connect(self.database_url) as connection:
            profile_id = connection.execute(
                "SELECT id FROM profiles WHERE clerk_subject = 'synthetic_a'"
            ).fetchone()[0]
            collected_basis = connection.execute(
                """
                UPDATE training_sessions
                SET duration_basis = 'source_reported'
                WHERE ownership = 'collected'
                RETURNING duration_basis
                """
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO training_sessions (
                    profile_id, id, ownership, local_date, timing_precision,
                    sport, duration_value, duration_unit, duration_basis
                ) VALUES (
                    %s, %s, 'app', %s, 'date_only', 'strength',
                    1800, 'seconds', 'active'
                )
                """,
                (profile_id, app_session_id, date(2026, 1, 10)),
            )
        self.assertEqual(collected_basis, "source_reported")

        with self.assertRaises(psycopg.errors.CheckViolation):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    """
                    UPDATE training_sessions
                    SET duration_basis = 'source_reported'
                    WHERE profile_id = %s AND id = %s
                    """,
                    (profile_id, app_session_id),
                )

    def test_session_load_ownership_must_match_training_session_owner(self) -> None:
        profile, source = self._profile_source()
        self.application.ingest(profile, self._batch(source))
        app_session_id = uuid4()
        with psycopg.connect(self.database_url) as connection:
            collected = connection.execute(
                """
                SELECT profile_id, id
                FROM training_sessions
                WHERE ownership = 'collected'
                """
            ).fetchone()
            connection.execute(
                """
                INSERT INTO training_sessions (
                    profile_id, id, ownership, local_date,
                    timing_precision, sport
                ) VALUES (%s, %s, 'app', %s, 'date_only', 'strength')
                """,
                (collected[0], app_session_id, date(2026, 1, 10)),
            )
            connection.execute(
                """
                INSERT INTO session_loads (
                    profile_id, id, training_session_id, ownership,
                    method, unit, value, source
                ) VALUES (%s, %s, %s, 'app', %s, %s, %s, %s)
                """,
                (
                    collected[0],
                    uuid4(),
                    app_session_id,
                    "session_rpe_load",
                    "arbitrary_units",
                    120,
                    "athlete_reported",
                ),
            )
            valid_owners = connection.execute(
                "SELECT ownership FROM session_loads ORDER BY ownership"
            ).fetchall()
        self.assertEqual(valid_owners, [("app",), ("collected",)])

        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    """
                    INSERT INTO session_loads (
                        profile_id, id, training_session_id, ownership,
                        collected_record_id, source_capture_id,
                        method, unit, value, source
                    ) VALUES (
                        %s, %s, %s, 'app', NULL, NULL,
                        'mismatched', 'points', 1, 'app'
                    )
                    """,
                    (collected[0], uuid4(), collected[1]),
                )

    def test_ingest_identity_and_lifecycle_evidence_are_immutable_to_app_role(self) -> None:
        _profile, source = self._profile_source(tokens=True)
        self.application.ingest(_profile, self._batch(source))
        job = self.application.execute(
            self.actor_a, RequestCollection(source, "immutable-lifecycle")
        )

        for statement in (
            "UPDATE ingest_batches SET created_at = now()",
            "DELETE FROM ingest_batches",
            "DELETE FROM collection_jobs",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with psycopg.connect(self.database_url) as connection:
                        connection.execute(statement)
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "UPDATE collection_jobs SET diagnostics = diagnostics"
                )
        with self.assertRaises(psycopg.errors.CheckViolation):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    """
                    UPDATE collection_jobs
                    SET state = 'running', started_at = now()
                    WHERE state = 'requested'
                    """
                )
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with psycopg.connect(self.migration_url) as connection:
                connection.execute("DELETE FROM ingest_batches")
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with psycopg.connect(self.migration_url) as connection:
                connection.execute("DELETE FROM collection_jobs")
        self.assertEqual(
            self.application.read(self.actor_a, GetCollectionJob(job.job)).state,
            "requested",
        )

    def test_collected_load_capture_integrity_is_deferred_for_projection_refresh(self) -> None:
        profile, source = self._profile_source()
        self.application.ingest(profile, self._batch(source, key="first"))
        self.application.ingest(
            profile,
            self._batch(
                source, key="second", payload_value=2, duration=1500
            ),
        )
        with psycopg.connect(self.database_url) as connection:
            current, older = connection.execute(
                """
                SELECT ts.current_capture_id,
                       (SELECT c.id FROM collected_record_captures AS c
                        WHERE c.profile_id = ts.profile_id
                          AND c.collected_record_id = ts.collected_record_id
                          AND c.id <> ts.current_capture_id LIMIT 1)
                FROM training_sessions AS ts
                WHERE ts.ownership = 'collected'
                """
            ).fetchone()
        self.assertNotEqual(current, older)
        connection = psycopg.connect(self.database_url)
        try:
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                connection.execute(
                    "UPDATE training_sessions SET current_capture_id = %s",
                    (older,),
                )
                connection.commit()
        finally:
            connection.close()
        projection = self.application.read(
            self.actor_a,
            GetCollectedRecord(source, "training-session", "synthetic-session"),
        )
        self.assertEqual(projection.session.duration_value, Decimal("1500"))

    def test_profiles_are_isolated_and_concurrent_identical_ingest_converges(self) -> None:
        profile_a, source_a = self._profile_source(self.actor_a, "a")
        profile_b, source_b = self._profile_source(self.actor_b, "b")
        batch_a = self._batch(source_a, key="concurrent")
        batch_b = self._batch(source_b, key="concurrent")

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(self.application.ingest, profile_a, batch_a),
                executor.submit(self.application.ingest, profile_a, batch_a),
                executor.submit(self.application.ingest, profile_b, batch_b),
            ]
            results = [future.result(timeout=10) for future in futures]

        self.assertEqual(sum(result.inserted for result in results), 2)
        self.assertEqual(sum(result.unchanged for result in results), 1)
        first = self.application.read(
            self.actor_a,
            GetCollectedRecord(source_a, "training-session", "synthetic-session"),
        )
        second = self.application.read(
            self.actor_b,
            GetCollectedRecord(source_b, "training-session", "synthetic-session"),
        )
        self.assertEqual((first.captures, second.captures), (1, 1))
        self.assertIsNone(
            self.application.read(
                self.actor_a,
                GetCollectedRecord(source_b, "training-session", "synthetic-session"),
            )
        )

    def test_job_requests_are_durable_idempotent_and_illegal_transition_is_rejected(self) -> None:
        _profile, source = self._profile_source(tokens=True)
        requested = self.application.execute(
            self.actor_a, RequestCollection(source, "request-1")
        )
        replay = self.application.execute(
            self.actor_a, RequestCollection(source, "request-1")
        )
        fresh = CoachApplication(self.settings)

        self.assertEqual(requested.job, replay.job)
        self.assertEqual(
            fresh.read(self.actor_a, GetCollectionJob(requested.job)).state,
            "requested",
        )
        with self.assertRaises(Conflict):
            self.application.execute(
                self.actor_a,
                RequestCollection(source, "request-1", kind="incremental"),
            )
        with self.assertRaises(psycopg.errors.CheckViolation):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "UPDATE collection_jobs SET state = 'succeeded'"
                )
        with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
            with psycopg.connect(self.database_url) as connection:
                connection.execute(
                    "UPDATE collection_jobs SET diagnostics = %s",
                    (json.dumps({"upstream_exception": "must not persist"}),),
                )

    def test_concurrent_duplicate_requests_admit_one_active_job_transactionally(self) -> None:
        _profile, source = self._profile_source(tokens=True)
        barrier = threading.Barrier(12)

        def request(index: int):
            barrier.wait(timeout=5)
            try:
                return self.application.execute(
                    self.actor_a,
                    RequestCollection(source, f"concurrent-request-{index}"),
                )
            except Conflict:
                return None

        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(request, range(12)))
        admitted = [result for result in results if result is not None]
        self.assertEqual(len(admitted), 1)
        with psycopg.connect(self.database_url) as connection:
            rows = connection.execute(
                "SELECT id, state, created_at FROM collection_jobs "
                "ORDER BY created_at, id"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "requested")
        overview = self.application.read(
            self.actor_a, GetCollectionJob(admitted[0].job)
        )
        self.assertEqual((overview.state, overview.created_at), ("requested", rows[0][2]))

    def test_credential_only_initial_auth_and_stale_token_fallback_are_bounded(self) -> None:
        _profile, source = self._profile_source(tokens=False)
        external = _ExternalCollection(
            self._batch(source, key="credential-only"),
            CollectionCheckpoint("initial_sync", {"complete": True}),
        )
        credential_adapter = SyntheticAdapter(external)
        credential_application = CoachApplication(
            self.settings,
            collection_adapter=credential_adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        credential_job = credential_application.execute(
            self.actor_a, RequestCollection(source, "credential-only")
        )

        completed = credential_application.execute(
            self.actor_a, RunCollectionJob(credential_job.job)
        )

        self.assertEqual(completed.state, "succeeded")
        self.assertEqual(credential_adapter.cached_flags, [False])
        self.assertEqual(
            credential_adapter.credentials,
            [b"synthetic-credential-bundle"],
        )

        # The first run persisted cached tokens. A typed cache failure gets one
        # and only one credential fallback, which can repair unattended auth.
        fallback_adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source, key="fallback-success", payload_value=2),
                CollectionCheckpoint("initial_sync", {"complete": True, "page": 2}),
            )
        )
        fallback_adapter.expected_cached_tokens = b"rotated-token-bundle"
        fallback_adapter.auth_errors = [
            RepairableAuthenticationError("authentication_required"),
            None,
        ]
        fallback_application = CoachApplication(
            self.settings,
            collection_adapter=fallback_adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        fallback_job = fallback_application.execute(
            self.actor_a, RequestCollection(source, "fallback-success")
        )

        repaired = fallback_application.execute(
            self.actor_a, RunCollectionJob(fallback_job.job)
        )

        self.assertEqual(repaired.state, "succeeded")
        self.assertEqual(fallback_adapter.cached_flags, [True, False])
        self.assertEqual(
            fallback_adapter.credentials,
            [None, b"synthetic-credential-bundle"],
        )
        self.assertTrue(all(not path.exists() for path in fallback_adapter.paths))

    def test_incremental_collection_receives_the_latest_committed_checkpoint(self) -> None:
        _profile, source = self._profile_source(tokens=True)
        initial_cursor = {"through_date": "2026-01-10", "initial": True}
        adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source, key="checkpoint-initial"),
                CollectionCheckpoint("initial_sync", initial_cursor),
            )
        )
        application = CoachApplication(
            self.settings,
            collection_adapter=adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        initial = application.execute(
            self.actor_a,
            RequestCollection(source, "checkpoint-initial", "initial_sync"),
        )
        application.execute(self.actor_a, RunCollectionJob(initial.job))

        incremental_cursor = {"through_date": "2026-01-11", "incremental": True}
        adapter.expected_cached_tokens = b"rotated-token-bundle"
        adapter.external = _ExternalCollection(
            self._batch(source, key="checkpoint-incremental"),
            CollectionCheckpoint("incremental", incremental_cursor),
        )
        incremental = application.execute(
            self.actor_a,
            RequestCollection(source, "checkpoint-incremental", "incremental"),
        )
        application.execute(self.actor_a, RunCollectionJob(incremental.job))

        adapter.external = _ExternalCollection(
            self._batch(source, key="checkpoint-incremental-2"),
            CollectionCheckpoint(
                "incremental", {"through_date": "2026-01-12"}
            ),
        )
        next_incremental = application.execute(
            self.actor_a,
            RequestCollection(source, "checkpoint-incremental-2", "incremental"),
        )
        application.execute(self.actor_a, RunCollectionJob(next_incremental.job))

        self.assertEqual(
            adapter.prior_checkpoints,
            [None, initial_cursor, incremental_cursor],
        )

    def test_initial_sync_tracer_rotates_before_collection_and_is_durable_and_redacted(self) -> None:
        profile, source = self._profile_source(tokens=True)
        batch = self._batch(source, key="initial-sync-capture")
        external = _ExternalCollection(
            batch,
            CollectionCheckpoint("initial_sync", {"page": 1, "complete": True}),
        )
        adapter = SyntheticAdapter(external)
        temp_root = Path(self.enterContext(TemporaryDirectory()))

        def verify_rotation_committed() -> None:
            with psycopg.connect(self.database_url) as connection:
                envelope, state = connection.execute(
                    "SELECT encrypted_tokens, state FROM source_connections"
                ).fetchone()
            plaintext = EncryptedBlob.from_envelope(envelope, self.key).decrypt(self.key)
            self.assertEqual(plaintext, b"rotated-token-bundle")
            self.assertEqual(state, "connected")
            durable = CoachApplication(self.settings)
            self.assertEqual(
                durable.read(self.actor_a, GetCollectionJob(requested.job)).state,
                "running",
            )
            self.assertEqual(
                durable.read(self.actor_a, GetSourceStatus(source)).outcome,
                "running",
            )

        adapter.before_collect = verify_rotation_committed
        application = CoachApplication(
            self.settings,
            collection_adapter=adapter,
            encryption_key=self.key,
            temporary_root=temp_root,
        )
        requested = application.execute(
            self.actor_a, RequestCollection(source, "initial-sync")
        )
        completed = application.execute(
            self.actor_a, RunCollectionJob(requested.job)
        )
        replayed = application.execute(
            self.actor_a, RunCollectionJob(requested.job)
        )

        self.assertEqual((completed.state, replayed.state), ("succeeded", "succeeded"))
        self.assertEqual(adapter.cached_flags, [True])
        self.assertEqual(adapter.credentials, [None])
        self.assertTrue(all(not path.exists() for path in adapter.paths))
        self.assertEqual(list(temp_root.iterdir()), [])

        fresh = CoachApplication(self.settings)
        status = fresh.read(self.actor_a, GetSourceStatus(source))
        projection = fresh.read(
            self.actor_a,
            GetCollectedRecord(source, "training-session", "synthetic-session"),
        )
        self.assertEqual((status.state, status.outcome), ("connected", "successful"))
        self.assertEqual(status.checkpoint_revision, 0)
        self.assertIsNotNone(status.last_success_at)
        self.assertEqual(projection.captures, 1)
        self.assertEqual(len(projection.observations), 2)

        with psycopg.connect(self.database_url) as connection:
            columns = {
                row[0]
                for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'collection_status_safe'"
                )
            }
            document = json.dumps(
                connection.execute(
                    "SELECT source_state, outcome, safe_code, diagnostics "
                    "FROM collection_status_safe"
                ).fetchall(),
                default=str,
            )
            encrypted_material = connection.execute(
                "SELECT encrypted_credentials, encrypted_tokens "
                "FROM source_connections"
            ).fetchone()
            lifecycle = connection.execute(
                "SELECT "
                "(SELECT array_agg(state) FROM collection_jobs), "
                "(SELECT array_agg(state) FROM collection_runs), "
                "(SELECT array_agg(state) FROM collection_attempts)"
            ).fetchone()
        self.assertFalse(
            {"encrypted_credentials", "encrypted_tokens", "connection_key"} & columns
        )
        self.assertNotIn("cached-token-bundle", document)
        self.assertNotIn("synthetic-credential-bundle", document)
        self.assertNotIn(b"synthetic-credential-bundle", encrypted_material[0])
        self.assertNotIn(b"rotated-token-bundle", encrypted_material[1])
        self.assertEqual(lifecycle, (["succeeded"], ["succeeded"], ["succeeded"]))
        for table in (
            "collection_jobs", "collection_runs", "collection_attempts"
        ):
            with self.subTest(table=table, operation="same-state"):
                with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                    with psycopg.connect(self.database_url) as connection:
                        connection.execute(
                            f"UPDATE {table} SET state = state"
                        )
            with self.subTest(table=table, operation="app-delete"):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with psycopg.connect(self.database_url) as connection:
                        connection.execute(f"DELETE FROM {table}")
            with self.subTest(table=table, operation="owner-delete"):
                with self.assertRaises(psycopg.errors.ObjectNotInPrerequisiteState):
                    with psycopg.connect(self.migration_url) as connection:
                        connection.execute(f"DELETE FROM {table}")

    def test_repairable_auth_failure_needs_reconnect_without_progress_and_cleans_up(self) -> None:
        _profile, source = self._profile_source(tokens=True)
        adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source),
                CollectionCheckpoint("initial_sync", {"complete": True}),
            )
        )
        adapter.auth_error = RepairableAuthenticationError("mfa_required")
        temp_root = Path(self.enterContext(TemporaryDirectory()))
        application = CoachApplication(
            self.settings,
            collection_adapter=adapter,
            encryption_key=self.key,
            temporary_root=temp_root,
        )
        job = application.execute(self.actor_a, RequestCollection(source, "auth-fails"))

        with self.assertRaises(CollectionFailed):
            application.execute(self.actor_a, RunCollectionJob(job.job))

        status = CoachApplication(self.settings).read(
            self.actor_a, GetSourceStatus(source)
        )
        failed_job = CoachApplication(self.settings).read(
            self.actor_a, GetCollectionJob(job.job)
        )
        self.assertEqual((status.state, status.outcome), ("needs_reconnect", "failed"))
        self.assertEqual((status.safe_code, failed_job.safe_code), ("mfa_required", "mfa_required"))
        self.assertEqual(adapter.cached_flags, [True, False])
        self.assertEqual(
            adapter.credentials,
            [None, b"synthetic-credential-bundle"],
        )
        self.assertIsNone(status.checkpoint_revision)
        self.assertIsNone(status.last_success_at)
        self.assertTrue(all(not path.exists() for path in adapter.paths))
        self.assertEqual(list(temp_root.iterdir()), [])
        with psycopg.connect(self.database_url) as connection:
            reconnect = connection.execute(
                "SELECT reconnect_safe_code, reconnect_at FROM source_connections"
            ).fetchone()
        self.assertEqual(reconnect[0], "mfa_required")
        self.assertIsNotNone(reconnect[1])

        second = application.execute(
            self.actor_a, RequestCollection(source, "auth-fails-fast")
        )
        with self.assertRaises(CollectionFailed):
            application.execute(self.actor_a, RunCollectionJob(second.job))
        second_view = application.read(self.actor_a, GetCollectionJob(second.job))
        self.assertEqual((second_view.state, second_view.safe_code), ("failed", "mfa_required"))
        self.assertEqual(adapter.cached_flags, [True, False])

    def test_actor_scoped_credential_replacement_resets_reconnect_and_keeps_ciphertext_only(self) -> None:
        _profile, source = self._profile_source(tokens=True)
        failing_adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source),
                CollectionCheckpoint("initial_sync", {"complete": True}),
            )
        )
        failing_adapter.auth_error = RepairableAuthenticationError(
            "credentials_rejected"
        )
        application = CoachApplication(
            self.settings,
            collection_adapter=failing_adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        failed = application.execute(
            self.actor_a, RequestCollection(source, "before-replacement")
        )
        with self.assertRaises(CollectionFailed):
            application.execute(self.actor_a, RunCollectionJob(failed.job))

        replacement_plaintext = b"replacement-credential-bundle"
        self.application.execute(self.actor_b, EnsureProfile())
        with self.assertRaises(AccessDenied):
            application.execute(
                self.actor_b,
                ConfigureSourceCredentials(
                    source, SecretBundle(b"cross-actor-secret")
                ),
            )
        command = ConfigureSourceCredentials(
            source, SecretBundle(replacement_plaintext)
        )
        returned = application.execute(self.actor_a, command)

        self.assertEqual(returned, source)
        self.assertNotIn(replacement_plaintext.decode(), repr(command))
        with psycopg.connect(self.database_url) as connection:
            stored = connection.execute(
                """
                SELECT encrypted_credentials, encrypted_tokens, state,
                       reconnect_safe_code, reconnect_at,
                       last_authenticated_at
                FROM source_connections
                """
            ).fetchone()
        self.assertNotIn(replacement_plaintext, stored[0])
        self.assertEqual(
            EncryptedBlob.from_envelope(stored[0], self.key).decrypt(self.key),
            replacement_plaintext,
        )
        self.assertEqual(stored[1:], (None, "disconnected", None, None, None))

        repaired_adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source, key="after-replacement", payload_value=2),
                CollectionCheckpoint("initial_sync", {"complete": True}),
            )
        )
        repaired_application = CoachApplication(
            self.settings,
            collection_adapter=repaired_adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        repaired_job = repaired_application.execute(
            self.actor_a, RequestCollection(source, "after-replacement")
        )
        repaired_application.execute(
            self.actor_a, RunCollectionJob(repaired_job.job)
        )
        self.assertEqual(repaired_adapter.cached_flags, [False])
        self.assertEqual(repaired_adapter.credentials, [replacement_plaintext])
        with psycopg.connect(self.database_url) as connection:
            connected = connection.execute(
                """
                SELECT state, reconnect_safe_code, reconnect_at,
                       encrypted_tokens
                FROM source_connections
                """
            ).fetchone()
        self.assertEqual(connected[:3], ("connected", None, None))
        self.assertNotIn(b"rotated-token-bundle", connected[3])

    def test_credential_replacement_waits_for_collection_profile_lock(self) -> None:
        _profile, source = self._profile_source(tokens=False)
        collection_paused = threading.Event()
        release_collection = threading.Event()
        replacement_started = threading.Event()
        replacement_finished = threading.Event()
        adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source, key="credential-race"),
                CollectionCheckpoint("initial_sync", {"complete": True}),
            )
        )

        def pause_after_token_rotation() -> None:
            collection_paused.set()
            if not release_collection.wait(timeout=10):
                raise AssertionError("collection race was not released")

        adapter.before_collect = pause_after_token_rotation
        application = CoachApplication(
            self.settings,
            collection_adapter=adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        job = application.execute(
            self.actor_a, RequestCollection(source, "credential-race")
        )
        replacement_plaintext = b"race-replacement-credential-bundle"

        def replace_credentials():
            replacement_started.set()
            try:
                return application.execute(
                    self.actor_a,
                    ConfigureSourceCredentials(
                        source, SecretBundle(replacement_plaintext)
                    ),
                )
            finally:
                replacement_finished.set()

        with ThreadPoolExecutor(max_workers=2) as executor:
            collection_future = executor.submit(
                application.execute,
                self.actor_a,
                RunCollectionJob(job.job),
            )
            self.assertTrue(collection_paused.wait(timeout=5))
            replacement_future = executor.submit(replace_credentials)
            self.assertTrue(replacement_started.wait(timeout=5))
            try:
                self.assertFalse(replacement_finished.wait(timeout=0.2))
                with psycopg.connect(self.database_url) as connection:
                    running_state = connection.execute(
                        "SELECT state, encrypted_tokens IS NOT NULL "
                        "FROM source_connections"
                    ).fetchone()
                self.assertEqual(running_state, ("connected", True))
            finally:
                release_collection.set()
            completed = collection_future.result(timeout=10)
            replaced = replacement_future.result(timeout=10)

        self.assertEqual(completed.state, "succeeded")
        self.assertEqual(replaced, source)
        self.assertEqual(adapter.credentials, [b"synthetic-credential-bundle"])
        with psycopg.connect(self.database_url) as connection:
            stored = connection.execute(
                """
                SELECT encrypted_credentials, encrypted_tokens, state,
                       reconnect_safe_code, reconnect_at, last_authenticated_at
                FROM source_connections
                """
            ).fetchone()
        self.assertEqual(
            EncryptedBlob.from_envelope(stored[0], self.key).decrypt(self.key),
            replacement_plaintext,
        )
        self.assertEqual(stored[1:], (None, "disconnected", None, None, None))

    def test_transient_collection_failure_preserves_reconnect_classification_and_progress(self) -> None:
        _profile, source = self._profile_source(tokens=True)
        adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source),
                CollectionCheckpoint("initial_sync", {"complete": True}),
            )
        )
        adapter.collection_error = TransientCollectionError("rate_limited")
        temp_root = Path(self.enterContext(TemporaryDirectory()))
        application = CoachApplication(
            self.settings,
            collection_adapter=adapter,
            encryption_key=self.key,
            temporary_root=temp_root,
        )
        job = application.execute(
            self.actor_a, RequestCollection(source, "collection-fails")
        )

        with self.assertRaises(CollectionFailed):
            application.execute(self.actor_a, RunCollectionJob(job.job))

        status = CoachApplication(self.settings).read(
            self.actor_a, GetSourceStatus(source)
        )
        self.assertEqual(status.state, "connected")
        self.assertNotEqual(status.state, "needs_reconnect")
        self.assertEqual((status.outcome, status.safe_code), ("failed", "rate_limited"))
        self.assertIsNone(status.checkpoint_revision)
        self.assertIsNone(status.last_success_at)
        self.assertTrue(all(not path.exists() for path in adapter.paths))
        self.assertEqual(list(temp_root.iterdir()), [])

    def test_arbitrary_provider_exception_text_never_drives_reconnect_or_leaks(self) -> None:
        _profile, source = self._profile_source(tokens=True)
        private_detail = "mfa credential path and identity must remain private"
        adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source),
                CollectionCheckpoint("initial_sync", {"complete": True}),
            )
        )
        adapter.auth_error = RuntimeError(private_detail)
        application = CoachApplication(
            self.settings,
            collection_adapter=adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        job = application.execute(
            self.actor_a, RequestCollection(source, "untyped-auth-failure")
        )

        with self.assertRaises(CollectionFailed) as raised:
            application.execute(self.actor_a, RunCollectionJob(job.job))
        status = CoachApplication(self.settings).read(
            self.actor_a, GetSourceStatus(source)
        )
        with psycopg.connect(self.database_url) as connection:
            persisted = json.dumps(
                connection.execute(
                    "SELECT safe_code, diagnostics FROM collection_jobs"
                ).fetchall(),
                default=str,
            )

        self.assertEqual((status.state, status.safe_code), ("disconnected", "external_failure"))
        self.assertNotIn(private_detail, str(raised.exception))
        self.assertNotIn(private_detail, persisted)
        self.assertTrue(all(not path.exists() for path in adapter.paths))

    def test_late_database_failure_rolls_back_ingest_checkpoint_and_success_together(self) -> None:
        _profile, source = self._profile_source(tokens=True)
        adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source, key="late-failure-batch"),
                CollectionCheckpoint("initial_sync", {"page": 1}),
            )
        )
        application = CoachApplication(
            self.settings,
            collection_adapter=adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        job = application.execute(
            self.actor_a, RequestCollection(source, "late-database-failure")
        )
        with psycopg.connect(self.migration_url) as connection:
            connection.execute(
                """
                CREATE FUNCTION garmin_coach_test_fail_success()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.outcome = 'successful' THEN
                        RAISE EXCEPTION 'synthetic late failure';
                    END IF;
                    RETURN NEW;
                END;
                $$
                """
            )
            connection.execute(
                """
                CREATE TRIGGER garmin_coach_test_fail_success
                BEFORE UPDATE ON collection_health
                FOR EACH ROW EXECUTE FUNCTION garmin_coach_test_fail_success()
                """
            )
        try:
            with self.assertRaises(ApplicationUnavailable):
                application.execute(self.actor_a, RunCollectionJob(job.job))
        finally:
            with psycopg.connect(self.migration_url) as connection:
                connection.execute(
                    "DROP TRIGGER IF EXISTS garmin_coach_test_fail_success "
                    "ON collection_health"
                )
                connection.execute(
                    "DROP FUNCTION IF EXISTS garmin_coach_test_fail_success()"
                )

        with psycopg.connect(self.database_url) as connection:
            rolled_back = connection.execute(
                """
                SELECT
                    (SELECT count(*) FROM ingest_batches),
                    (SELECT count(*) FROM collected_record_captures),
                    (SELECT count(*) FROM training_sessions),
                    (SELECT count(*) FROM session_loads),
                    (SELECT count(*) FROM controlled_observations),
                    (SELECT count(*) FROM observation_projection_heads),
                    (SELECT count(*) FROM collection_checkpoints),
                    (SELECT state FROM collection_jobs),
                    (SELECT outcome FROM collection_health)
                """
            ).fetchone()
            token_envelope = connection.execute(
                "SELECT encrypted_tokens FROM source_connections"
            ).fetchone()[0]
        self.assertEqual(
            rolled_back,
            (0, 0, 0, 0, 0, 0, 0, "running", "running"),
        )
        self.assertEqual(
            EncryptedBlob.from_envelope(token_envelope, self.key).decrypt(self.key),
            b"rotated-token-bundle",
        )
        self.assertIsNone(
            application.read(
                self.actor_a,
                GetCollectedRecord(
                    source, "training-session", "synthetic-session"
                ),
            )
        )

        recovery_adapter = SyntheticAdapter(adapter.external)
        fresh = CoachApplication(
            self.settings,
            collection_adapter=recovery_adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        recovered = fresh.execute(self.actor_a, RunCollectionJob(job.job))
        status = fresh.read(self.actor_a, GetSourceStatus(source))
        self.assertEqual((recovered.state, recovered.safe_code), ("failed", "worker_lost"))
        self.assertEqual((status.outcome, status.safe_code), ("failed", "worker_lost"))
        self.assertIsNone(status.checkpoint_revision)
        self.assertIsNone(status.last_success_at)
        self.assertEqual(recovery_adapter.cached_flags, [])

    def test_fresh_application_recovers_stranded_running_job_as_worker_lost(self) -> None:
        _profile, source = self._profile_source(tokens=True)
        successful_adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source, key="recovery-baseline"),
                CollectionCheckpoint("initial_sync", {"page": 1}),
            )
        )
        successful = CoachApplication(
            self.settings,
            collection_adapter=successful_adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        baseline_job = successful.execute(
            self.actor_a, RequestCollection(source, "recovery-baseline")
        )
        successful.execute(self.actor_a, RunCollectionJob(baseline_job.job))
        before = successful.read(self.actor_a, GetSourceStatus(source))

        crashing_adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source, key="must-not-ingest", payload_value=2),
                CollectionCheckpoint("initial_sync", {"page": 2}),
            )
        )
        crashing_adapter.expected_cached_tokens = b"rotated-token-bundle"
        crashing_adapter.auth_error = psycopg.OperationalError(
            "synthetic process/database loss"
        )
        crash_root = Path(self.enterContext(TemporaryDirectory()))
        crashing = CoachApplication(
            self.settings,
            collection_adapter=crashing_adapter,
            encryption_key=self.key,
            temporary_root=crash_root,
        )
        stranded_job = crashing.execute(
            self.actor_a, RequestCollection(source, "stranded-worker")
        )
        with self.assertRaises(ApplicationUnavailable):
            crashing.execute(self.actor_a, RunCollectionJob(stranded_job.job))
        self.assertEqual(
            crashing.read(self.actor_a, GetCollectionJob(stranded_job.job)).state,
            "running",
        )
        self.assertEqual(list(crash_root.iterdir()), [])

        recovery_adapter = SyntheticAdapter(crashing_adapter.external)
        recovery_root = Path(self.enterContext(TemporaryDirectory()))
        fresh = CoachApplication(
            self.settings,
            collection_adapter=recovery_adapter,
            encryption_key=self.key,
            temporary_root=recovery_root,
        )
        recovered = fresh.execute(
            self.actor_a, RunCollectionJob(stranded_job.job)
        )
        after = fresh.read(self.actor_a, GetSourceStatus(source))
        projection = fresh.read(
            self.actor_a,
            GetCollectedRecord(source, "training-session", "synthetic-session"),
        )

        self.assertEqual((recovered.state, recovered.safe_code), ("failed", "worker_lost"))
        self.assertEqual(recovery_adapter.cached_flags, [])
        self.assertEqual(recovery_adapter.credentials, [])
        self.assertEqual(list(recovery_root.iterdir()), [])
        self.assertEqual(after.checkpoint_revision, before.checkpoint_revision)
        self.assertEqual(after.last_success_at, before.last_success_at)
        self.assertEqual((after.outcome, after.safe_code), ("failed", "worker_lost"))
        self.assertEqual(projection.captures, 1)
        with psycopg.connect(self.database_url) as connection:
            evidence = connection.execute(
                """
                SELECT j.state, j.safe_code, r.state, r.safe_code,
                       a.state, a.safe_code
                FROM collection_jobs AS j
                JOIN collection_runs AS r
                  ON r.profile_id = j.profile_id AND r.job_id = j.id
                JOIN collection_attempts AS a
                  ON a.profile_id = r.profile_id AND a.run_id = r.id
                WHERE j.request_key = 'stranded-worker'
                """
            ).fetchone()
        self.assertEqual(
            evidence,
            ("failed", "worker_lost", "failed", "worker_lost", "failed", "worker_lost"),
        )

    def test_stranded_job_blocks_successor_until_recovered(self) -> None:
        _profile, source = self._profile_source(tokens=True)
        crashing_adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source, key="stranded-a-capture"),
                CollectionCheckpoint("initial_sync", {"page": 1}),
            )
        )
        crashing_adapter.auth_error = psycopg.OperationalError(
            "synthetic worker loss"
        )
        crashing = CoachApplication(
            self.settings,
            collection_adapter=crashing_adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        job_a = crashing.execute(
            self.actor_a, RequestCollection(source, "stranded-job-a")
        )
        with self.assertRaises(ApplicationUnavailable):
            crashing.execute(self.actor_a, RunCollectionJob(job_a.job))
        with self.assertRaises(Conflict):
            crashing.execute(
                self.actor_a, RequestCollection(source, "blocked-successor")
            )

        recovery = CoachApplication(
            self.settings,
            collection_adapter=SyntheticAdapter(crashing_adapter.external),
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        recovered = recovery.execute(self.actor_a, RunCollectionJob(job_a.job))
        successor = recovery.execute(
            self.actor_a, RequestCollection(source, "accepted-after-recovery")
        )
        self.assertEqual(
            (recovered.state, recovered.safe_code), ("failed", "worker_lost")
        )
        self.assertEqual(successor.state, "requested")
        with psycopg.connect(self.database_url) as connection:
            active = connection.execute(
                "SELECT request_key, state FROM collection_jobs "
                "WHERE state IN ('requested', 'running')"
            ).fetchall()
        self.assertEqual(active, [("accepted-after-recovery", "requested")])

    def test_failure_after_success_does_not_advance_checkpoint_or_freshness(self) -> None:
        _profile, source = self._profile_source(tokens=True)
        success_adapter = SyntheticAdapter(
            _ExternalCollection(
                self._batch(source, key="successful-capture"),
                CollectionCheckpoint("initial_sync", {"page": 1}),
            )
        )
        successful = CoachApplication(
            self.settings,
            collection_adapter=success_adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        job = successful.execute(
            self.actor_a, RequestCollection(source, "successful-job")
        )
        successful.execute(self.actor_a, RunCollectionJob(job.job))
        before = CoachApplication(self.settings).read(
            self.actor_a, GetSourceStatus(source)
        )

        class FailingAfterRotationAdapter(SyntheticAdapter):
            def authenticate(self, token_directory, credentials, cached_tokens):
                self.paths.append(token_directory)
                self.cached_flags.append(cached_tokens)
                self.credentials.append(credentials)
                self.asserted_cached = (token_directory / "tokens.bundle").read_bytes()
                return _AuthenticatedTokenSession(
                    "synthetic-session", b"second-rotated-token-bundle"
                )

        failing_adapter = FailingAfterRotationAdapter(
            _ExternalCollection(
                self._batch(source, key="must-not-ingest", payload_value=2),
                CollectionCheckpoint("initial_sync", {"page": 2}),
            )
        )
        failing_adapter.collection_error = TransientCollectionError(
            "provider_unavailable"
        )
        failing = CoachApplication(
            self.settings,
            collection_adapter=failing_adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        failed_job = failing.execute(
            self.actor_a, RequestCollection(source, "failed-job")
        )
        with self.assertRaises(CollectionFailed):
            failing.execute(self.actor_a, RunCollectionJob(failed_job.job))
        after = CoachApplication(self.settings).read(
            self.actor_a, GetSourceStatus(source)
        )
        projection = CoachApplication(self.settings).read(
            self.actor_a,
            GetCollectedRecord(source, "training-session", "synthetic-session"),
        )

        self.assertEqual(after.last_success_at, before.last_success_at)
        self.assertEqual(after.checkpoint_revision, before.checkpoint_revision)
        self.assertEqual(projection.captures, 1)
        self.assertEqual(
            failing_adapter.asserted_cached, b"rotated-token-bundle"
        )
        self.assertTrue(all(not path.exists() for path in failing_adapter.paths))

    def test_same_profile_serializes_while_different_profiles_can_collect_concurrently(self) -> None:
        class ConcurrentAdapter:
            def __init__(self, outputs):
                self.outputs = outputs
                self.active = 0
                self.maximum = 0
                self.lock = threading.Lock()

            def authenticate(self, token_directory, credentials, cached_tokens):
                handle = (token_directory / "tokens.bundle").read_bytes().decode()
                while handle.startswith("rotated-"):
                    handle = handle.removeprefix("rotated-")
                return _AuthenticatedTokenSession(handle, f"rotated-{handle}".encode())

            def collect(self, handle, kind, prior_checkpoint):
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                time.sleep(0.2)
                with self.lock:
                    self.active -= 1
                return self.outputs[handle]

        def configured(actor, suffix, token_text):
            base = CoachApplication(self.settings)
            profile = base.execute(actor, EnsureProfile()).profile
            source = base.execute(
                actor,
                EnsureSourceConnection(
                    "synthetic-provider",
                    f"connection-{suffix}",
                ),
            )
            with psycopg.connect(self.migration_url) as connection:
                connection.execute(
                    """
                    UPDATE source_connections
                    SET encrypted_tokens = %s
                    WHERE connection_key = %s
                    """,
                    (
                        EncryptedBlob.encrypt(
                            token_text.encode(), self.key
                        ).envelope,
                        f"connection-{suffix}",
                    ),
                )
            return profile, source

        profile_a, source_a1 = configured(self.actor_a, "a1", "token-a1")
        _, source_a2 = configured(self.actor_a, "a2", "token-a2")
        profile_b, source_b = configured(self.actor_b, "b", "token-b")
        outputs = {
            "token-a1": _ExternalCollection(
                self._batch(source_a1, key="a1"),
                CollectionCheckpoint("initial_sync", {"source": "a1"}),
            ),
            "token-a2": _ExternalCollection(
                self._batch(source_a2, key="a2"),
                CollectionCheckpoint("initial_sync", {"source": "a2"}),
            ),
            "token-b": _ExternalCollection(
                self._batch(source_b, key="b"),
                CollectionCheckpoint("initial_sync", {"source": "b"}),
            ),
        }
        adapter = ConcurrentAdapter(outputs)
        application = CoachApplication(
            self.settings,
            collection_adapter=adapter,
            encryption_key=self.key,
            temporary_root=Path(self.enterContext(TemporaryDirectory())),
        )
        jobs = [
            application.execute(self.actor_a, RequestCollection(source_a1, "a1")),
            application.execute(self.actor_a, RequestCollection(source_a2, "a2")),
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda job: application.execute(
                        self.actor_a, RunCollectionJob(job.job)
                    ),
                    jobs,
                )
            )
        self.assertEqual({result.state for result in results}, {"succeeded"})
        self.assertEqual(adapter.maximum, 1)

        # Fresh jobs on different Profiles share no advisory-lock key.
        adapter.maximum = 0
        jobs = [
            (self.actor_a, application.execute(self.actor_a, RequestCollection(source_a1, "a3"))),
            (self.actor_b, application.execute(self.actor_b, RequestCollection(source_b, "b1"))),
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    application.execute, actor, RunCollectionJob(job.job)
                )
                for actor, job in jobs
            ]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual({result.state for result in results}, {"succeeded"})
        self.assertEqual(adapter.maximum, 2)
        self.assertNotEqual(profile_a, profile_b)


if __name__ == "__main__":
    unittest.main()
