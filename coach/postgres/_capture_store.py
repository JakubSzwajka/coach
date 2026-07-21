"""Private transactional primitives for the first PostgreSQL schema slice.

`CoachApplication` will own the public use-case boundary. This module exists so
schema behavior can be exercised without exposing tables to adapters.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb

from .config import DatabaseSettings
from .encryption import EncryptedBlob


class CaptureStoreError(RuntimeError):
    """A profile-scoped capture operation is invalid."""


@dataclass(frozen=True, slots=True)
class CaptureInput:
    record_kind: str
    source_key: str
    payload: Any
    source_at: datetime | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CaptureBatchResult:
    inserted: int
    unchanged: int


@dataclass(frozen=True, slots=True)
class _PreparedCapture:
    record_kind: str
    source_key: str
    payload: Any
    source_at: datetime | None
    provenance: Any
    content_hash: bytes


class CaptureStore:
    """Private PostgreSQL implementation for Profile and capture operations."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings

    def ensure_profile(
        self, issuer: str, subject: str, display_name: str | None = None
    ) -> UUID:
        issuer = _required_text(issuer, "issuer")
        subject = _required_text(subject, "subject")
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

    def ingest_captures(
        self,
        profile_id: UUID,
        source_connection_id: UUID,
        captures: Sequence[CaptureInput],
    ) -> CaptureBatchResult:
        prepared = tuple(
            sorted(
                (_prepare_capture(capture) for capture in captures),
                key=lambda capture: (
                    capture.record_kind,
                    capture.source_key,
                    capture.content_hash,
                ),
            )
        )
        inserted_count = 0
        unchanged_count = 0
        with psycopg.connect(self._settings.url) as connection:
            owned_connection = connection.execute(
                """
                SELECT 1
                FROM source_connections
                WHERE profile_id = %s AND id = %s
                """,
                (profile_id, source_connection_id),
            ).fetchone()
            if owned_connection is None:
                raise CaptureStoreError("source connection is not available")

            for capture in prepared:
                record_id = uuid4()
                inserted_record = connection.execute(
                    """
                    INSERT INTO collected_records (
                        profile_id,
                        id,
                        source_connection_id,
                        record_kind,
                        source_key
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (
                        profile_id,
                        source_connection_id,
                        record_kind,
                        source_key
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

                inserted_capture = connection.execute(
                    """
                    INSERT INTO collected_record_captures (
                        profile_id,
                        id,
                        collected_record_id,
                        source_at,
                        content_hash,
                        payload,
                        provenance
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (
                        profile_id,
                        collected_record_id,
                        content_hash
                    ) DO NOTHING
                    RETURNING id
                    """,
                    (
                        profile_id,
                        uuid4(),
                        record_id,
                        capture.source_at,
                        capture.content_hash,
                        Jsonb(capture.payload),
                        Jsonb(capture.provenance),
                    ),
                ).fetchone()
                if inserted_capture is None:
                    unchanged_count += 1
                else:
                    inserted_count += 1

        return CaptureBatchResult(
            inserted=inserted_count,
            unchanged=unchanged_count,
        )


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CaptureStoreError(f"{field_name} is required")
    return value.strip()


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
