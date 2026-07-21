"""Private transactional capture and canonical-ingest persistence.

``CoachApplication`` owns the public use-case boundary. This module preserves
immutable source history and regenerable typed projections without exposing
PostgreSQL details to adapters.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb

from .config import DatabaseSettings
from .encryption import EncryptedBlob


class CaptureStoreError(RuntimeError):
    """A profile-scoped capture operation is invalid."""


class _ProfileNotFound(CaptureStoreError):
    pass


class _StaleRevision(CaptureStoreError):
    pass


class _AccessDenied(CaptureStoreError):
    pass


class _IdempotencyConflict(CaptureStoreError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class _StoredProfile:
    id: UUID
    display_name: str | None
    revision: int

    def __repr__(self) -> str:
        return f"_StoredProfile(revision={self.revision}, <redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _CollectionSummary:
    records: int
    captures: int
    latest_collected_at: datetime | None

    def __repr__(self) -> str:
        return (
            "_CollectionSummary("
            f"records={self.records}, captures={self.captures}, <redacted>)"
        )


@dataclass(frozen=True, slots=True)
class CaptureInput:
    record_kind: str = field(repr=False)
    source_key: str = field(repr=False)
    payload: Any = field(repr=False)
    source_at: datetime | None = field(default=None, repr=False)
    provenance: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return "CaptureInput(<redacted>)"


@dataclass(frozen=True, slots=True)
class CaptureBatchResult:
    inserted: int
    unchanged: int
    sessions: int = 0
    observations: int = 0


@dataclass(frozen=True, slots=True)
class RecordPointerInput:
    record_kind: str = field(repr=False)
    source_key: str = field(repr=False)

    def __repr__(self) -> str:
        return "RecordPointerInput(<redacted>)"


@dataclass(frozen=True, slots=True)
class SessionLoadInput:
    method: str = field(repr=False)
    unit: str = field(repr=False)
    value: Any = field(repr=False)
    source: str = field(repr=False)

    def __repr__(self) -> str:
        return "SessionLoadInput(<redacted>)"


@dataclass(frozen=True, slots=True)
class TrainingSessionInput:
    capture: RecordPointerInput = field(repr=False)
    local_date: date = field(repr=False)
    sport: str = field(repr=False)
    timing_precision: str = field(default="date_only", repr=False)
    local_start: datetime | None = field(default=None, repr=False)
    time_zone: str | None = field(default=None, repr=False)
    utc_offset: str | None = field(default=None, repr=False)
    session_type: str | None = field(default=None, repr=False)
    title: str | None = field(default=None, repr=False)
    session_rpe: int | None = field(default=None, repr=False)
    duration_value: Any | None = field(default=None, repr=False)
    duration_unit: str | None = field(default=None, repr=False)
    duration_basis: str | None = field(default=None, repr=False)
    distance_value: Any | None = field(default=None, repr=False)
    distance_unit: str | None = field(default=None, repr=False)
    notes: str | None = field(default=None, repr=False)
    loads: Sequence[SessionLoadInput] = field(default_factory=tuple, repr=False)

    def __repr__(self) -> str:
        return "TrainingSessionInput(<redacted>)"


@dataclass(frozen=True, slots=True)
class ObservationInput:
    capture: RecordPointerInput = field(repr=False)
    definition: str = field(repr=False)
    value_type: str = field(repr=False)
    unit: str = field(repr=False)
    window_kind: str = field(repr=False)
    method: str = field(repr=False)
    status: str = field(repr=False)
    value: Any | None = field(default=None, repr=False)
    local_date: date | None = field(default=None, repr=False)
    observed_at: datetime | None = field(default=None, repr=False)
    window_start: datetime | None = field(default=None, repr=False)
    window_end: datetime | None = field(default=None, repr=False)
    provenance: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return "ObservationInput(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _StoredLoad:
    method: str
    unit: str
    value: Decimal
    source: str

    def __repr__(self) -> str:
        return "_StoredLoad(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _StoredSession:
    id: UUID
    local_date: date
    local_start: datetime | None
    timing_precision: str
    time_zone: str | None
    utc_offset: str | None
    sport: str
    session_type: str | None
    title: str | None
    session_rpe: int | None
    duration_value: Decimal | None
    duration_unit: str | None
    duration_basis: str | None
    distance_value: Decimal | None
    distance_unit: str | None
    notes: str | None
    loads: tuple[_StoredLoad, ...]

    def __repr__(self) -> str:
        return "_StoredSession(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _StoredObservation:
    definition: str
    status: str
    value: Any | None
    value_type: str
    unit: str
    window_kind: str
    method: str
    local_date: date | None
    observed_at: datetime | None
    window_start: datetime | None
    window_end: datetime | None
    provenance: Mapping[str, Any]

    def __repr__(self) -> str:
        return "_StoredObservation(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _StoredProjection:
    captures: int
    session: _StoredSession | None
    observations: tuple[_StoredObservation, ...]

    def __repr__(self) -> str:
        return f"_StoredProjection(captures={self.captures}, <redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedCapture:
    record_kind: str
    source_key: str
    payload: Any
    source_at: datetime | None
    provenance: Any
    content_hash: bytes

    def __repr__(self) -> str:
        return "_PreparedCapture(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedSession:
    capture: tuple[str, str]
    local_date: date
    local_start: datetime | None
    timing_precision: str
    time_zone: str | None
    utc_offset: str | None
    sport: str
    session_type: str | None
    title: str | None
    session_rpe: int | None
    duration_value: Decimal | None
    duration_unit: str | None
    duration_basis: str | None
    distance_value: Decimal | None
    distance_unit: str | None
    notes: str | None
    loads: tuple[SessionLoadInput, ...]

    def __repr__(self) -> str:
        return "_PreparedSession(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedObservation:
    capture: tuple[str, str]
    definition: str
    value_type: str
    unit: str
    window_kind: str
    method: str
    status: str
    value: Any | None
    local_date: date | None
    observed_at: datetime | None
    window_start: datetime | None
    window_end: datetime | None
    provenance: Any


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedGraph:
    idempotency_key: str
    semantics_hash: bytes
    captures: tuple[_PreparedCapture, ...]
    sessions: tuple[_PreparedSession, ...]
    observations: tuple[_PreparedObservation, ...]

    def __repr__(self) -> str:
        return "_PreparedGraph(<redacted>)"


class CaptureStore:
    """Private PostgreSQL implementation for Profile and capture operations."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings

    def ensure_profile(
        self, issuer: str, subject: str, display_name: str | None = None
    ) -> UUID:
        issuer = _required_text(issuer, "issuer")
        subject = _required_text(subject, "subject")
        display_name = _optional_text(display_name, "display_name")
        try:
            with psycopg.connect(self._settings.url) as connection:
                existing = connection.execute(
                    """
                    SELECT id
                    FROM profiles
                    WHERE clerk_issuer = %s AND clerk_subject = %s
                    """,
                    (issuer, subject),
                ).fetchone()
                if existing is not None:
                    return existing[0]

                profile_id = uuid4()
                connection.execute(
                    """
                    INSERT INTO profiles (
                        id, clerk_issuer, clerk_subject, display_name
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (profile_id, issuer, subject, display_name),
                )
                return profile_id
        except errors.UniqueViolation:
            # A concurrent transaction won the actor identity.
            with psycopg.connect(self._settings.url) as connection:
                existing = connection.execute(
                    """
                    SELECT id
                    FROM profiles
                    WHERE clerk_issuer = %s AND clerk_subject = %s
                    """,
                    (issuer, subject),
                ).fetchone()
                if existing is None:
                    raise CaptureStoreError("profile binding failed")
                return existing[0]

    def get_profile(self, issuer: str, subject: str) -> _StoredProfile | None:
        issuer = _required_text(issuer, "issuer")
        subject = _required_text(subject, "subject")
        with psycopg.connect(self._settings.url) as connection:
            row = connection.execute(
                """
                SELECT id, display_name, revision
                FROM profiles
                WHERE clerk_issuer = %s AND clerk_subject = %s
                """,
                (issuer, subject),
            ).fetchone()
        if row is None:
            return None
        return _StoredProfile(row[0], row[1], row[2])

    def update_profile_display_name(
        self,
        issuer: str,
        subject: str,
        display_name: str | None,
        expected_revision: int,
    ) -> _StoredProfile:
        issuer = _required_text(issuer, "issuer")
        subject = _required_text(subject, "subject")
        display_name = _optional_text(display_name, "display_name")
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
        ):
            raise CaptureStoreError("expected_revision must be a non-negative integer")
        with psycopg.connect(self._settings.url) as connection:
            row = connection.execute(
                """
                UPDATE profiles
                SET display_name = %s,
                    revision = revision + 1
                WHERE clerk_issuer = %s
                  AND clerk_subject = %s
                  AND revision = %s
                RETURNING id, display_name, revision
                """,
                (display_name, issuer, subject, expected_revision),
            ).fetchone()
            if row is not None:
                return _StoredProfile(row[0], row[1], row[2])
            exists = connection.execute(
                """
                SELECT 1
                FROM profiles
                WHERE clerk_issuer = %s AND clerk_subject = %s
                """,
                (issuer, subject),
            ).fetchone()
            if exists is None:
                raise _ProfileNotFound("profile is not available")
            raise _StaleRevision("profile revision is stale")

    def ensure_source_connection_for_actor(
        self,
        issuer: str,
        subject: str,
        provider: str,
        connection_key: str,
    ) -> tuple[UUID, UUID]:
        profile = self.get_profile(issuer, subject)
        if profile is None:
            raise _ProfileNotFound("profile is not available")
        connection_id = self.ensure_source_connection(
            profile.id,
            provider,
            connection_key,
        )
        return profile.id, connection_id

    def collection_summary(
        self, issuer: str, subject: str
    ) -> tuple[UUID, _CollectionSummary] | None:
        issuer = _required_text(issuer, "issuer")
        subject = _required_text(subject, "subject")
        with psycopg.connect(self._settings.url) as connection:
            row = connection.execute(
                """
                SELECT
                    p.id,
                    count(DISTINCT r.id),
                    count(c.id),
                    max(c.collected_at)
                FROM profiles AS p
                LEFT JOIN collected_records AS r ON r.profile_id = p.id
                LEFT JOIN collected_record_captures AS c
                  ON c.profile_id = r.profile_id
                 AND c.collected_record_id = r.id
                WHERE p.clerk_issuer = %s AND p.clerk_subject = %s
                GROUP BY p.id
                """,
                (issuer, subject),
            ).fetchone()
        if row is None:
            return None
        return row[0], _CollectionSummary(row[1], row[2], row[3])

    def ensure_source_connection(
        self,
        profile_id: UUID,
        provider: str,
        connection_key: str,
        *,
        encrypted_credentials: EncryptedBlob | None = None,
        encrypted_tokens: EncryptedBlob | None = None,
    ) -> UUID:
        provider = _required_text(provider, "provider")
        connection_key = _required_text(connection_key, "connection_key")
        encrypted_credentials = _encrypted_blob(
            encrypted_credentials, "encrypted_credentials"
        )
        encrypted_tokens = _encrypted_blob(encrypted_tokens, "encrypted_tokens")
        connection_id = uuid4()
        with psycopg.connect(self._settings.url) as connection:
            inserted = connection.execute(
                """
                INSERT INTO source_connections (
                    profile_id,
                    id,
                    provider,
                    connection_key,
                    encrypted_credentials,
                    encrypted_tokens
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    profile_id,
                    provider,
                    connection_key
                ) DO NOTHING
                RETURNING id
                """,
                (
                    profile_id,
                    connection_id,
                    provider,
                    connection_key,
                    encrypted_credentials,
                    encrypted_tokens,
                ),
            ).fetchone()
            if inserted is not None:
                return inserted[0]
            existing = connection.execute(
                """
                SELECT id, encrypted_credentials, encrypted_tokens
                FROM source_connections
                WHERE profile_id = %s AND provider = %s AND connection_key = %s
                """,
                (profile_id, provider, connection_key),
            ).fetchone()
            if existing is None:
                raise CaptureStoreError("source connection binding failed")
            if (
                encrypted_credentials is not None
                and existing[1] != encrypted_credentials
            ) or (encrypted_tokens is not None and existing[2] != encrypted_tokens):
                raise CaptureStoreError(
                    "source connection already exists with different encrypted material"
                )
            return existing[0]

    def replace_source_credentials_for_actor(
        self,
        connection: psycopg.Connection,
        issuer: str,
        subject: str,
        profile_id: UUID,
        source_connection_id: UUID,
        encrypted_credentials: EncryptedBlob,
    ) -> None:
        issuer = _required_text(issuer, "issuer")
        subject = _required_text(subject, "subject")
        envelope = _encrypted_blob(
            encrypted_credentials, "encrypted_credentials"
        )
        assert envelope is not None
        with connection.transaction():
            updated = connection.execute(
                """
                UPDATE source_connections AS sc
                SET encrypted_credentials = %s,
                    encrypted_tokens = NULL,
                    state = 'disconnected',
                    state_revision = sc.state_revision + 1,
                    last_authenticated_at = NULL,
                    reconnect_safe_code = NULL,
                    reconnect_at = NULL,
                    updated_at = now()
                FROM profiles AS p
                WHERE p.id = sc.profile_id
                  AND p.clerk_issuer = %s AND p.clerk_subject = %s
                  AND sc.profile_id = %s AND sc.id = %s
                """,
                (
                    envelope,
                    issuer,
                    subject,
                    profile_id,
                    source_connection_id,
                ),
            ).rowcount
            if updated != 1:
                raise _AccessDenied("source connection is not available")

    def ingest_captures(
        self,
        profile_id: UUID,
        source_connection_id: UUID,
        captures: Sequence[CaptureInput],
    ) -> CaptureBatchResult:
        return self.ingest_graph(
            profile_id,
            source_connection_id,
            idempotency_key=None,
            captures=captures,
            sessions=(),
            observations=(),
        )

    def ingest_graph(
        self,
        profile_id: UUID,
        source_connection_id: UUID,
        *,
        idempotency_key: str | None,
        captures: Sequence[CaptureInput],
        sessions: Sequence[TrainingSessionInput],
        observations: Sequence[ObservationInput],
        connection: psycopg.Connection | None = None,
    ) -> CaptureBatchResult:
        if connection is not None:
            prepared = _prepare_graph(
                idempotency_key,
                captures,
                sessions,
                observations,
                self._observation_definitions(connection),
            )
            return self._ingest_prepared(
                connection, profile_id, source_connection_id, prepared
            )
        with psycopg.connect(self._settings.url) as owned:
            prepared = _prepare_graph(
                idempotency_key,
                captures,
                sessions,
                observations,
                self._observation_definitions(owned),
            )
            return self._ingest_prepared(
                owned, profile_id, source_connection_id, prepared
            )

    @staticmethod
    def _observation_definitions(
        connection: psycopg.Connection,
    ) -> dict[str, tuple[str, str, str, str, bool]]:
        return {
            row[0]: (row[1], row[2], row[3], row[4], row[5])
            for row in connection.execute(
                """
                SELECT key, value_type, unit, window_kind, method,
                       missing_allowed
                FROM observation_definitions
                """
            ).fetchall()
        }

    def _ingest_prepared(
        self,
        connection: psycopg.Connection,
        profile_id: UUID,
        source_connection_id: UUID,
        prepared: _PreparedGraph,
    ) -> CaptureBatchResult:
        owned_connection = connection.execute(
            """
            SELECT 1
            FROM source_connections
            WHERE profile_id = %s AND id = %s
            """,
            (profile_id, source_connection_id),
        ).fetchone()
        if owned_connection is None:
            raise _AccessDenied("source connection is not available")

        inserted_batch = connection.execute(
            """
            INSERT INTO ingest_batches (
                profile_id, id, source_connection_id,
                idempotency_key, semantics_hash
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (
                profile_id, source_connection_id, idempotency_key
            ) DO NOTHING
            RETURNING id
            """,
            (
                profile_id,
                uuid4(),
                source_connection_id,
                prepared.idempotency_key,
                prepared.semantics_hash,
            ),
        ).fetchone()
        if inserted_batch is None:
            existing_hash = connection.execute(
                """
                SELECT semantics_hash
                FROM ingest_batches
                WHERE profile_id = %s
                  AND source_connection_id = %s
                  AND idempotency_key = %s
                """,
                (profile_id, source_connection_id, prepared.idempotency_key),
            ).fetchone()
            if existing_hash is None:
                raise CaptureStoreError("ingest batch binding failed")
            if existing_hash[0] != prepared.semantics_hash:
                raise _IdempotencyConflict(
                    "idempotency key already represents another batch"
                )

        inserted_count = 0
        unchanged_count = 0
        identities: dict[tuple[str, str], tuple[UUID, UUID, bool]] = {}
        for capture in prepared.captures:
            identity = (capture.record_kind, capture.source_key)
            record_id = uuid4()
            inserted_record = connection.execute(
                """
                INSERT INTO collected_records (
                    profile_id, id, source_connection_id, record_kind, source_key
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (
                    profile_id, source_connection_id, record_kind, source_key
                ) DO NOTHING
                RETURNING id
                """,
                (
                    profile_id,
                    record_id,
                    source_connection_id,
                    capture.record_kind,
                    capture.source_key,
                ),
            ).fetchone()
            if inserted_record is None:
                existing_record = connection.execute(
                    """
                    SELECT id
                    FROM collected_records
                    WHERE profile_id = %s
                      AND source_connection_id = %s
                      AND record_kind = %s
                      AND source_key = %s
                    """,
                    (
                        profile_id,
                        source_connection_id,
                        capture.record_kind,
                        capture.source_key,
                    ),
                ).fetchone()
                if existing_record is None:
                    raise CaptureStoreError("collected record binding failed")
                record_id = existing_record[0]
            else:
                record_id = inserted_record[0]

            capture_id = uuid4()
            inserted_capture = connection.execute(
                """
                INSERT INTO collected_record_captures (
                    profile_id, id, collected_record_id, source_at,
                    content_hash, payload, provenance
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (
                    profile_id, collected_record_id, content_hash
                ) DO NOTHING
                RETURNING id
                """,
                (
                    profile_id,
                    capture_id,
                    record_id,
                    capture.source_at,
                    capture.content_hash,
                    Jsonb(capture.payload),
                    Jsonb(capture.provenance),
                ),
            ).fetchone()
            if inserted_capture is None:
                unchanged_count += 1
                existing_capture = connection.execute(
                    """
                    SELECT id
                    FROM collected_record_captures
                    WHERE profile_id = %s
                      AND collected_record_id = %s
                      AND content_hash = %s
                    """,
                    (profile_id, record_id, capture.content_hash),
                ).fetchone()
                if existing_capture is None:
                    raise CaptureStoreError("capture binding failed")
                capture_id = existing_capture[0]
            else:
                inserted_count += 1
                capture_id = inserted_capture[0]
            identities[identity] = (
                record_id,
                capture_id,
                inserted_capture is not None,
            )

        # Serialize every projection decision for one Collected Record. A
        # NO KEY UPDATE lock conflicts with another projector while remaining
        # compatible with the KEY SHARE locks taken by concurrent child FK
        # inserts. UUID order is stable across overlapping multi-record batches.
        for record_id in sorted(
            {identity[0] for identity in identities.values()},
            key=lambda value: value.int,
        ):
            locked = connection.execute(
                """
                SELECT 1
                FROM collected_records
                WHERE profile_id = %s AND id = %s
                FOR NO KEY UPDATE
                """,
                (profile_id, record_id),
            ).fetchone()
            if locked is None:
                raise CaptureStoreError("collected record binding failed")

        for session in prepared.sessions:
            record_id, capture_id, capture_is_new = identities[session.capture]
            current_session = connection.execute(
                """
                SELECT id, current_capture_id
                FROM training_sessions
                WHERE profile_id = %s AND collected_record_id = %s
                  AND ownership = 'collected'
                """,
                (profile_id, record_id),
            ).fetchone()
            # A delayed replay of an older immutable capture must never roll a
            # newer current projection backwards. Reprocessing the current
            # capture remains allowed because projections are regenerable.
            if (
                current_session is not None
                and current_session[1] != capture_id
                and not capture_is_new
            ):
                continue
            session_id = uuid4()
            row = connection.execute(
                """
                INSERT INTO training_sessions (
                    profile_id, id, ownership, collected_record_id,
                    current_capture_id, local_date, local_start,
                    timing_precision, time_zone, utc_offset, sport,
                    session_type, title, session_rpe, duration_value,
                    duration_unit, duration_basis, distance_value,
                    distance_unit, notes
                ) VALUES (
                    %s, %s, 'collected', %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (profile_id, collected_record_id)
                    WHERE collected_record_id IS NOT NULL
                DO UPDATE SET
                    current_capture_id = EXCLUDED.current_capture_id,
                    local_date = EXCLUDED.local_date,
                    local_start = EXCLUDED.local_start,
                    timing_precision = EXCLUDED.timing_precision,
                    time_zone = EXCLUDED.time_zone,
                    utc_offset = EXCLUDED.utc_offset,
                    sport = EXCLUDED.sport,
                    session_type = EXCLUDED.session_type,
                    title = EXCLUDED.title,
                    session_rpe = EXCLUDED.session_rpe,
                    duration_value = EXCLUDED.duration_value,
                    duration_unit = EXCLUDED.duration_unit,
                    duration_basis = EXCLUDED.duration_basis,
                    distance_value = EXCLUDED.distance_value,
                    distance_unit = EXCLUDED.distance_unit,
                    notes = EXCLUDED.notes,
                    updated_at = now()
                RETURNING id
                """,
                (
                    profile_id,
                    session_id,
                    record_id,
                    capture_id,
                    session.local_date,
                    session.local_start,
                    session.timing_precision,
                    session.time_zone,
                    session.utc_offset,
                    session.sport,
                    session.session_type,
                    session.title,
                    session.session_rpe,
                    session.duration_value,
                    session.duration_unit,
                    session.duration_basis,
                    session.distance_value,
                    session.distance_unit,
                    session.notes,
                ),
            ).fetchone()
            if row is None:
                raise CaptureStoreError("training session projection failed")
            session_id = row[0]
            connection.execute(
                """
                DELETE FROM session_loads
                WHERE profile_id = %s AND training_session_id = %s
                  AND ownership = 'collected'
                """,
                (profile_id, session_id),
            )
            for load in session.loads:
                connection.execute(
                    """
                    INSERT INTO session_loads (
                        profile_id, id, training_session_id, ownership,
                        collected_record_id, source_capture_id,
                        method, unit, value, source
                    ) VALUES (%s, %s, %s, 'collected', %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        profile_id,
                        uuid4(),
                        session_id,
                        record_id,
                        capture_id,
                        load.method,
                        load.unit,
                        load.value,
                        load.source,
                    ),
                )

        observation_groups: dict[
            tuple[str, str], list[_PreparedObservation]
        ] = {}
        for observation in prepared.observations:
            observation_groups.setdefault(observation.capture, []).append(observation)
        active_observations: list[_PreparedObservation] = []
        for capture_key in identities:
            grouped = observation_groups.get(capture_key, [])
            record_id, capture_id, capture_is_new = identities[capture_key]
            current = connection.execute(
                """
                SELECT current_capture_id
                FROM observation_projection_heads
                WHERE profile_id = %s AND collected_record_id = %s
                """,
                (profile_id, record_id),
            ).fetchone()
            if current is not None and current[0] != capture_id and not capture_is_new:
                continue
            connection.execute(
                """
                DELETE FROM controlled_observations
                WHERE profile_id = %s AND collected_record_id = %s
                """,
                (profile_id, record_id),
            )
            connection.execute(
                """
                INSERT INTO observation_projection_heads (
                    profile_id, collected_record_id, current_capture_id
                ) VALUES (%s, %s, %s)
                ON CONFLICT (profile_id, collected_record_id)
                DO UPDATE SET current_capture_id = EXCLUDED.current_capture_id,
                              updated_at = now()
                """,
                (profile_id, record_id, capture_id),
            )
            active_observations.extend(grouped)
        for observation in active_observations:
            record_id, capture_id, _ = identities[observation.capture]
            values = {
                "decimal": (observation.value, None, None, None),
                "integer": (None, observation.value, None, None),
                "text": (None, None, observation.value, None),
                "boolean": (None, None, None, observation.value),
            }
            decimal_value, integer_value, text_value, boolean_value = (
                (None, None, None, None)
                if observation.status == "missing"
                else values[observation.value_type]
            )
            connection.execute(
                """
                INSERT INTO controlled_observations (
                    profile_id, id, collected_record_id, current_capture_id,
                    definition_key, value_type, unit, window_kind, method,
                    status, local_date, observed_at, window_start, window_end,
                    decimal_value, integer_value, text_value, boolean_value,
                    provenance
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    profile_id,
                    uuid4(),
                    record_id,
                    capture_id,
                    observation.definition,
                    observation.value_type,
                    observation.unit,
                    observation.window_kind,
                    observation.method,
                    observation.status,
                    observation.local_date,
                    observation.observed_at,
                    observation.window_start,
                    observation.window_end,
                    decimal_value,
                    integer_value,
                    text_value,
                    boolean_value,
                    Jsonb(observation.provenance),
                ),
            )

        return CaptureBatchResult(
            inserted=inserted_count,
            unchanged=unchanged_count,
            sessions=len(prepared.sessions),
            observations=len(prepared.observations),
        )

    def collected_projection_for_actor(
        self,
        issuer: str,
        subject: str,
        source_connection_id: UUID,
        record_kind: str,
        source_key: str,
    ) -> _StoredProjection | None:
        issuer = _required_text(issuer, "issuer")
        subject = _required_text(subject, "subject")
        record_kind = _required_text(record_kind, "record_kind")
        source_key = _required_text(source_key, "source_key")
        with psycopg.connect(self._settings.url) as connection:
            record = connection.execute(
                """
                SELECT r.profile_id, r.id,
                       (SELECT count(*) FROM collected_record_captures AS c
                        WHERE c.profile_id = r.profile_id
                          AND c.collected_record_id = r.id)
                FROM profiles AS p
                JOIN collected_records AS r ON r.profile_id = p.id
                WHERE p.clerk_issuer = %s AND p.clerk_subject = %s
                  AND r.source_connection_id = %s
                  AND r.record_kind = %s AND r.source_key = %s
                """,
                (issuer, subject, source_connection_id, record_kind, source_key),
            ).fetchone()
            if record is None:
                return None
            profile_id, record_id, capture_count = record
            session_row = connection.execute(
                """
                SELECT id, local_date, local_start, timing_precision, time_zone,
                       utc_offset, sport, session_type, title, session_rpe,
                       duration_value, duration_unit, duration_basis,
                       distance_value, distance_unit, notes
                FROM training_sessions
                WHERE profile_id = %s AND collected_record_id = %s
                  AND ownership = 'collected'
                """,
                (profile_id, record_id),
            ).fetchone()
            session = None
            if session_row is not None:
                load_rows = connection.execute(
                    """
                    SELECT method, unit, value, source
                    FROM session_loads
                    WHERE profile_id = %s AND training_session_id = %s
                    ORDER BY method, unit, source
                    """,
                    (profile_id, session_row[0]),
                ).fetchall()
                session = _StoredSession(
                    id=session_row[0],
                    local_date=session_row[1],
                    local_start=session_row[2],
                    timing_precision=session_row[3],
                    time_zone=session_row[4],
                    utc_offset=session_row[5],
                    sport=session_row[6],
                    session_type=session_row[7],
                    title=session_row[8],
                    session_rpe=session_row[9],
                    duration_value=session_row[10],
                    duration_unit=session_row[11],
                    duration_basis=session_row[12],
                    distance_value=session_row[13],
                    distance_unit=session_row[14],
                    notes=session_row[15],
                    loads=tuple(_StoredLoad(*row) for row in load_rows),
                )
            observation_rows = connection.execute(
                """
                SELECT definition_key, status,
                       decimal_value, integer_value, text_value, boolean_value,
                       value_type, unit, window_kind, method, local_date,
                       observed_at, window_start, window_end, provenance
                FROM controlled_observations
                WHERE profile_id = %s AND collected_record_id = %s
                ORDER BY definition_key
                """,
                (profile_id, record_id),
            ).fetchall()
        observations = []
        for row in observation_rows:
            value_by_type = {
                "decimal": row[2],
                "integer": row[3],
                "text": row[4],
                "boolean": row[5],
            }
            observations.append(
                _StoredObservation(
                    definition=row[0],
                    status=row[1],
                    value=None if row[1] == "missing" else value_by_type[row[6]],
                    value_type=row[6],
                    unit=row[7],
                    window_kind=row[8],
                    method=row[9],
                    local_date=row[10],
                    observed_at=row[11],
                    window_start=row[12],
                    window_end=row[13],
                    provenance=row[14],
                )
            )
        return _StoredProjection(
            captures=capture_count,
            session=session,
            observations=tuple(observations),
        )


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CaptureStoreError(f"{field_name} is required")
    return value.strip()


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CaptureStoreError(f"{field_name} must be text or null")
    return value.strip() or None


def _encrypted_blob(
    value: EncryptedBlob | None, field_name: str
) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, EncryptedBlob):
        raise CaptureStoreError(f"{field_name} must be an EncryptedBlob")
    return value.envelope


def _normalize_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CaptureStoreError("capture must contain finite JSON numbers")
        if value == 0:
            return 0
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise CaptureStoreError("JSON object keys must be strings")
        return {key: _normalize_json(item) for key, item in value.items()}
    raise CaptureStoreError("capture must contain valid JSON values")


def _prepare_capture(capture: CaptureInput) -> _PreparedCapture:
    if not isinstance(capture, CaptureInput):
        raise CaptureStoreError("captures must use CaptureInput")
    record_kind = _required_text(capture.record_kind, "record_kind")
    source_key = _required_text(capture.source_key, "source_key")
    if capture.source_at is not None and (
        not isinstance(capture.source_at, datetime)
        or capture.source_at.utcoffset() is None
    ):
        raise CaptureStoreError("source_at must include a time zone")
    payload = _normalize_json(capture.payload)
    provenance = _normalize_json(dict(capture.provenance))
    payload_bytes = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _PreparedCapture(
        record_kind=record_kind,
        source_key=source_key,
        payload=payload,
        source_at=capture.source_at,
        provenance=provenance,
        content_hash=hashlib.sha256(payload_bytes).digest(),
    )


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise CaptureStoreError(f"{field_name} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CaptureStoreError(f"{field_name} must be a finite number") from None
    if not result.is_finite():
        raise CaptureStoreError(f"{field_name} must be a finite number")
    return result


def _decimal_semantics(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _optional_measurement(
    value: Any | None,
    unit: str | None,
    method: str | None,
    field_name: str,
) -> tuple[Decimal | None, str | None, str | None]:
    if value is None and unit is None and method is None:
        return None, None, None
    if value is None or unit is None or method is None:
        raise CaptureStoreError(
            f"{field_name} value, unit, and method must be supplied together"
        )
    measured = _decimal(value, f"{field_name}_value")
    if measured <= 0:
        raise CaptureStoreError(f"{field_name}_value must be positive")
    return (
        measured,
        _required_text(unit, f"{field_name}_unit"),
        _required_text(method, f"{field_name}_method"),
    )


def _aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise CaptureStoreError(f"{field_name} must include a time zone")
    return value


def _naive_local(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is not None:
        raise CaptureStoreError(f"{field_name} must be a naive local date and time")
    return value


def _prepare_session(
    session: TrainingSessionInput,
    capture_keys: set[tuple[str, str]],
) -> _PreparedSession:
    if not isinstance(session, TrainingSessionInput):
        raise CaptureStoreError("sessions must use TrainingSessionInput")
    capture = _prepare_pointer(session.capture)
    if capture not in capture_keys:
        raise CaptureStoreError("training session references an absent capture")
    if not isinstance(session.local_date, date) or isinstance(
        session.local_date, datetime
    ):
        raise CaptureStoreError("local_date must be a date")
    timing_precision = _required_text(
        session.timing_precision, "timing_precision"
    )
    if timing_precision not in {"date_only", "local_datetime"}:
        raise CaptureStoreError("timing_precision is unsupported")
    local_start = _naive_local(session.local_start, "local_start")
    if (timing_precision == "date_only") != (local_start is None):
        raise CaptureStoreError("local_start must match timing_precision")
    if local_start is not None and local_start.date() != session.local_date:
        raise CaptureStoreError("local_start date must match local_date")
    session_rpe = session.session_rpe
    if (
        session_rpe is not None
        and (
            isinstance(session_rpe, bool)
            or not isinstance(session_rpe, int)
            or not 1 <= session_rpe <= 10
        )
    ):
        raise CaptureStoreError("session_rpe must be an integer from 1 to 10")
    duration = _optional_measurement(
        session.duration_value,
        session.duration_unit,
        session.duration_basis,
        "duration",
    )
    if duration[2] is not None and duration[2] not in {
        "elapsed",
        "active",
        "source_reported",
    }:
        raise CaptureStoreError(
            "collected duration_basis must be elapsed, active, or source_reported"
        )
    if session.distance_value is None and session.distance_unit is None:
        distance = (None, None)
    elif session.distance_value is None or session.distance_unit is None:
        raise CaptureStoreError(
            "distance value and unit must be supplied together"
        )
    else:
        distance_value = _decimal(session.distance_value, "distance_value")
        if distance_value <= 0:
            raise CaptureStoreError("distance_value must be positive")
        distance = (
            distance_value,
            _required_text(session.distance_unit, "distance_unit"),
        )
    loads = []
    load_keys: set[tuple[str, str, str]] = set()
    for load in session.loads:
        if not isinstance(load, SessionLoadInput):
            raise CaptureStoreError("loads must use SessionLoadInput")
        method = _required_text(load.method, "load_method")
        unit = _required_text(load.unit, "load_unit")
        value = _decimal(load.value, "load_value")
        if value <= 0:
            raise CaptureStoreError("load_value must be positive")
        source = _required_text(load.source, "load_source")
        if (method, unit, source) in load_keys:
            raise CaptureStoreError("load method, unit, and source must be unique")
        load_keys.add((method, unit, source))
        loads.append(SessionLoadInput(method, unit, value, source))
    return _PreparedSession(
        capture=capture,
        local_date=session.local_date,
        local_start=local_start,
        timing_precision=timing_precision,
        time_zone=_optional_text(session.time_zone, "time_zone"),
        utc_offset=_optional_text(session.utc_offset, "utc_offset"),
        sport=_required_text(session.sport, "sport"),
        session_type=_optional_text(session.session_type, "session_type"),
        title=_optional_text(session.title, "title"),
        session_rpe=session_rpe,
        duration_value=duration[0],
        duration_unit=duration[1],
        duration_basis=duration[2],
        distance_value=distance[0],
        distance_unit=distance[1],
        notes=_optional_text(session.notes, "notes"),
        loads=tuple(
            sorted(loads, key=lambda item: (item.method, item.unit, item.source))
        ),
    )


def _prepare_pointer(pointer: RecordPointerInput) -> tuple[str, str]:
    if not isinstance(pointer, RecordPointerInput):
        raise CaptureStoreError("capture references must use RecordPointerInput")
    return (
        _required_text(pointer.record_kind, "record_kind"),
        _required_text(pointer.source_key, "source_key"),
    )


def _prepare_observation(
    observation: ObservationInput,
    capture_keys: set[tuple[str, str]],
    definitions: Mapping[str, tuple[str, str, str, str, bool]],
) -> _PreparedObservation:
    if not isinstance(observation, ObservationInput):
        raise CaptureStoreError("observations must use ObservationInput")
    capture = _prepare_pointer(observation.capture)
    if capture not in capture_keys:
        raise CaptureStoreError("observation references an absent capture")
    definition_key = _required_text(observation.definition, "definition")
    expected = definitions.get(definition_key)
    supplied = (
        observation.value_type,
        observation.unit,
        observation.window_kind,
        observation.method,
    )
    if expected is None or supplied != expected[:4]:
        raise CaptureStoreError(
            "observation type, unit, window, and method must match its definition"
        )
    status = _required_text(observation.status, "observation_status")
    if status not in {"observed", "missing"}:
        raise CaptureStoreError("observation status is unsupported")
    if status == "missing":
        if not expected[4] or observation.value is not None:
            raise CaptureStoreError("missing observations cannot carry a value")
        value = None
    elif observation.value_type == "decimal":
        value = _decimal(observation.value, "observation_value")
    elif observation.value_type == "integer":
        if isinstance(observation.value, bool) or not isinstance(
            observation.value, int
        ):
            raise CaptureStoreError("integer observation requires an integer")
        value = observation.value
    elif observation.value_type == "text":
        if not isinstance(observation.value, str):
            raise CaptureStoreError("text observation requires text")
        value = observation.value
    elif observation.value_type == "boolean":
        if not isinstance(observation.value, bool):
            raise CaptureStoreError("boolean observation requires a boolean")
        value = observation.value
    else:
        raise CaptureStoreError("observation value type is unsupported")

    local_date = observation.local_date
    observed_at = _aware(observation.observed_at, "observed_at")
    window_start = _aware(observation.window_start, "window_start")
    window_end = _aware(observation.window_end, "window_end")
    if observation.window_kind == "calendar_day":
        valid_window = (
            isinstance(local_date, date)
            and not isinstance(local_date, datetime)
            and observed_at is None
            and window_start is None
            and window_end is None
        )
    elif observation.window_kind == "instant":
        valid_window = (
            local_date is None
            and observed_at is not None
            and window_start is None
            and window_end is None
        )
    else:
        valid_window = (
            local_date is None
            and observed_at is None
            and window_start is not None
            and window_end is not None
            and window_start < window_end
        )
    if not valid_window:
        raise CaptureStoreError("observation window does not match its definition")
    return _PreparedObservation(
        capture=capture,
        definition=definition_key,
        value_type=observation.value_type,
        unit=observation.unit,
        window_kind=observation.window_kind,
        method=observation.method,
        status=status,
        value=value,
        local_date=local_date,
        observed_at=observed_at,
        window_start=window_start,
        window_end=window_end,
        provenance=_normalize_json(dict(observation.provenance)),
    )


def _prepare_graph(
    idempotency_key: str | None,
    captures: Sequence[CaptureInput],
    sessions: Sequence[TrainingSessionInput],
    observations: Sequence[ObservationInput],
    definitions: Mapping[str, tuple[str, str, str, str, bool]],
) -> _PreparedGraph:
    if not isinstance(captures, Sequence) or not isinstance(sessions, Sequence) or not isinstance(observations, Sequence):
        raise CaptureStoreError("ingest graph collections must be sequences")
    prepared_captures = tuple(
        sorted(
            (_prepare_capture(capture) for capture in captures),
            key=lambda item: (item.record_kind, item.source_key),
        )
    )
    capture_keys = {
        (capture.record_kind, capture.source_key) for capture in prepared_captures
    }
    if len(capture_keys) != len(prepared_captures):
        raise CaptureStoreError("a batch cannot repeat a capture source identity")
    prepared_sessions = tuple(
        sorted(
            (_prepare_session(session, capture_keys) for session in sessions),
            key=lambda item: item.capture,
        )
    )
    if len({session.capture for session in prepared_sessions}) != len(
        prepared_sessions
    ):
        raise CaptureStoreError("a capture can project at most one training session")
    prepared_observations = tuple(
        sorted(
            (
                _prepare_observation(observation, capture_keys, definitions)
                for observation in observations
            ),
            key=lambda item: (item.capture, item.definition),
        )
    )
    observation_keys = {
        (observation.capture, observation.definition)
        for observation in prepared_observations
    }
    if len(observation_keys) != len(prepared_observations):
        raise CaptureStoreError(
            "a controlled observation definition can occur once per capture"
        )

    semantics = {
        "captures": [
            {
                "record_kind": capture.record_kind,
                "source_key": capture.source_key,
                "source_at": capture.source_at.isoformat()
                if capture.source_at is not None
                else None,
                "content_hash": capture.content_hash.hex(),
                "provenance": capture.provenance,
            }
            for capture in prepared_captures
        ],
        "sessions": [
            {
                "capture": list(session.capture),
                "local_date": session.local_date.isoformat(),
                "local_start": session.local_start.isoformat()
                if session.local_start is not None
                else None,
                "timing_precision": session.timing_precision,
                "time_zone": session.time_zone,
                "utc_offset": session.utc_offset,
                "sport": session.sport,
                "session_type": session.session_type,
                "title": session.title,
                "session_rpe": session.session_rpe,
                "duration": [
                    _decimal_semantics(session.duration_value),
                    session.duration_unit,
                    session.duration_basis,
                ],
                "distance": [
                    _decimal_semantics(session.distance_value),
                    session.distance_unit,
                ],
                "notes": session.notes,
                "loads": [
                    [
                        load.method,
                        load.unit,
                        _decimal_semantics(load.value),
                        load.source,
                    ]
                    for load in session.loads
                ],
            }
            for session in prepared_sessions
        ],
        "observations": [
            {
                "capture": list(observation.capture),
                "definition": observation.definition,
                "type": observation.value_type,
                "unit": observation.unit,
                "window": observation.window_kind,
                "method": observation.method,
                "status": observation.status,
                "value": _decimal_semantics(observation.value)
                if isinstance(observation.value, Decimal)
                else observation.value,
                "local_date": observation.local_date.isoformat()
                if observation.local_date is not None
                else None,
                "observed_at": observation.observed_at.isoformat()
                if observation.observed_at is not None
                else None,
                "window_start": observation.window_start.isoformat()
                if observation.window_start is not None
                else None,
                "window_end": observation.window_end.isoformat()
                if observation.window_end is not None
                else None,
                "provenance": observation.provenance,
            }
            for observation in prepared_observations
        ],
    }
    encoded = json.dumps(
        semantics,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    semantics_hash = hashlib.sha256(encoded).digest()
    key = (
        f"implicit:{semantics_hash.hex()}"
        if idempotency_key is None
        else _required_text(idempotency_key, "idempotency_key")
    )
    return _PreparedGraph(
        idempotency_key=key,
        semantics_hash=semantics_hash,
        captures=prepared_captures,
        sessions=prepared_sessions,
        observations=prepared_observations,
    )
