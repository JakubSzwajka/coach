"""Private PostgreSQL collection lifecycle and token-lease primitives."""

from __future__ import annotations

import hashlib
import math
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator, Mapping
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from ._capture_store import (
    CaptureStoreError,
    _AccessDenied,
    _IdempotencyConflict,
    _ProfileNotFound,
)
from .config import DatabaseSettings
from .encryption import EncryptedBlob

_SAFE_CODES = {
    "authentication_required",
    "mfa_required",
    "credentials_rejected",
    "rate_limited",
    "provider_unavailable",
    "external_failure",
    "invalid_response",
    "worker_lost",
}


class RepairableAuthenticationError(RuntimeError):
    """Typed external auth failure that requires operator repair.

    The exception deliberately accepts only an allowlisted code and never an
    upstream message, identity, or payload.
    """

    def __init__(self, code: str = "authentication_required") -> None:
        if code not in {
            "authentication_required",
            "mfa_required",
            "credentials_rejected",
        }:
            raise ValueError("unsupported repairable authentication code")
        self.code = code
        super().__init__(code)


class TransientCollectionError(RuntimeError):
    """Typed retryable external failure with a privacy-safe code."""

    def __init__(self, code: str = "provider_unavailable") -> None:
        if code not in {"rate_limited", "provider_unavailable"}:
            raise ValueError("unsupported transient collection code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class _StoredJob:
    profile_id: UUID
    id: UUID
    source_connection_id: UUID
    kind: str
    state: str
    safe_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    def __repr__(self) -> str:
        return f"_StoredJob(state={self.state!r}, safe_code={self.safe_code!r}, <redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _StoredStatus:
    state: str
    outcome: str | None
    safe_code: str | None
    attempted_at: datetime | None
    finished_at: datetime | None
    last_success_at: datetime | None
    checkpoint_revision: int | None

    def __repr__(self) -> str:
        return (
            "_StoredStatus("
            f"state={self.state!r}, outcome={self.outcome!r}, "
            f"safe_code={self.safe_code!r}, <redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _StoredSourceOverview:
    profile_id: UUID
    source_connection_id: UUID
    provider: str
    state: str
    credentials_stored: bool
    last_authenticated_at: datetime | None
    reconnect_safe_code: str | None
    latest_job: _StoredJob | None
    initial_sync: _StoredStatus
    incremental: _StoredStatus

    def __repr__(self) -> str:
        return (
            "_StoredSourceOverview("
            f"provider={self.provider!r}, state={self.state!r}, <redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _PendingWork:
    issuer: str
    subject: str
    job: _StoredJob

    def __repr__(self) -> str:
        return "_PendingWork(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class _RunLease:
    job: _StoredJob
    run_id: UUID
    attempt_id: UUID
    encrypted_credentials: bytes | None
    encrypted_tokens: bytes | None
    source_state: str
    reconnect_safe_code: str | None

    def __repr__(self) -> str:
        return "_RunLease(<redacted>)"


class CollectionStore:
    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings

    def request_for_actor(
        self,
        issuer: str,
        subject: str,
        source_connection_id: UUID,
        request_key: str,
        kind: str,
    ) -> _StoredJob:
        request_key = _required_text(request_key, "request_key")
        if kind not in {"initial_sync", "incremental"}:
            raise CaptureStoreError("collection kind is unsupported")
        with psycopg.connect(self._settings.url) as connection:
            return self.request_for_actor_on_connection(
                connection,
                issuer,
                subject,
                source_connection_id,
                request_key,
                kind,
            )

    def request_for_actor_on_connection(
        self,
        connection: psycopg.Connection,
        issuer: str,
        subject: str,
        source_connection_id: UUID,
        request_key: str,
        kind: str,
    ) -> _StoredJob:
        request_key = _required_text(request_key, "request_key")
        if kind not in {"initial_sync", "incremental"}:
            raise CaptureStoreError("collection kind is unsupported")
        source = connection.execute(
            """
            SELECT sc.profile_id
            FROM source_connections AS sc
            JOIN profiles AS p ON p.id = sc.profile_id
            WHERE p.clerk_issuer = %s AND p.clerk_subject = %s
              AND sc.id = %s
            """,
            (issuer, subject, source_connection_id),
        ).fetchone()
        if source is None:
            raise _AccessDenied("source connection is not available")
        profile_id = source[0]
        job_id = uuid4()
        inserted = connection.execute(
            """
            INSERT INTO collection_jobs (
                profile_id, id, source_connection_id, request_key, kind
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (profile_id, job_id, source_connection_id, request_key, kind),
        ).fetchone()
        if inserted is not None:
            return self._job(connection, profile_id, inserted[0])

        existing = connection.execute(
            """
            SELECT id, kind
            FROM collection_jobs
            WHERE profile_id = %s AND source_connection_id = %s
              AND request_key = %s
            """,
            (profile_id, source_connection_id, request_key),
        ).fetchone()
        if existing is not None:
            if existing[1] != kind:
                raise _IdempotencyConflict(
                    "request key already represents another collection kind"
                )
            return self._job(connection, profile_id, existing[0])

        active = connection.execute(
            """
            SELECT 1
            FROM collection_jobs
            WHERE profile_id = %s AND source_connection_id = %s
              AND state IN ('requested', 'running')
            """,
            (profile_id, source_connection_id),
        ).fetchone()
        if active is not None:
            raise _IdempotencyConflict(
                "a collection job is already active for this source"
            )
        raise CaptureStoreError("collection request binding failed")

    @contextmanager
    def worker_connection(self) -> Iterator[psycopg.Connection | None]:
        """Hold the process-independent singleton worker lease.

        A second adapter never mistakes work actively owned by the lease holder
        for a stranded job. Losing the PostgreSQL connection releases the lease,
        so a restarted adapter can recover durable requested/running work.
        """
        lock_key = int.from_bytes(
            hashlib.sha256(b"garmin-coach-persistent-collection-worker").digest()[:8],
            byteorder="big",
            signed=True,
        )
        connection = psycopg.connect(self._settings.url, autocommit=True)
        try:
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (lock_key,)
            ).fetchone()
            if acquired != (True,):
                yield None
                return
            yield connection
        finally:
            connection.close()

    @staticmethod
    def next_pending_work(
        connection: psycopg.Connection,
    ) -> _PendingWork | None:
        row = connection.execute(
            """
            SELECT p.clerk_issuer, p.clerk_subject,
                   j.profile_id, j.id, j.source_connection_id, j.kind,
                   j.state, j.safe_code, j.created_at, j.started_at,
                   j.finished_at
            FROM collection_jobs AS j
            JOIN profiles AS p ON p.id = j.profile_id
            WHERE j.state IN ('requested', 'running')
            ORDER BY CASE WHEN j.state = 'running' THEN 0 ELSE 1 END,
                     j.created_at, j.id
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return _PendingWork(row[0], row[1], _StoredJob(*row[2:]))

    def job_for_actor(
        self, issuer: str, subject: str, profile_id: UUID, job_id: UUID
    ) -> _StoredJob | None:
        with psycopg.connect(self._settings.url) as connection:
            owned = connection.execute(
                """
                SELECT 1 FROM profiles
                WHERE id = %s AND clerk_issuer = %s AND clerk_subject = %s
                """,
                (profile_id, issuer, subject),
            ).fetchone()
            if owned is None:
                return None
            row = connection.execute(
                """
                SELECT profile_id, id, source_connection_id, kind, state,
                       safe_code, created_at, started_at, finished_at
                FROM collection_jobs
                WHERE profile_id = %s AND id = %s
                """,
                (profile_id, job_id),
            ).fetchone()
        return _StoredJob(*row) if row is not None else None

    def status_for_actor(
        self,
        issuer: str,
        subject: str,
        profile_id: UUID,
        source_connection_id: UUID,
        domain: str,
    ) -> _StoredStatus | None:
        if domain not in {"initial_sync", "incremental"}:
            raise CaptureStoreError("collection domain is unsupported")
        with psycopg.connect(self._settings.url) as connection:
            row = connection.execute(
                """
                SELECT sc.state, h.outcome, h.safe_code, h.attempted_at,
                       h.finished_at, h.last_success_at, cp.revision
                FROM source_connections AS sc
                JOIN profiles AS p ON p.id = sc.profile_id
                LEFT JOIN collection_health AS h
                  ON h.profile_id = sc.profile_id
                 AND h.source_connection_id = sc.id
                 AND h.domain = %s
                LEFT JOIN collection_checkpoints AS cp
                  ON cp.profile_id = sc.profile_id
                 AND cp.source_connection_id = sc.id
                 AND cp.domain = %s
                WHERE p.id = %s AND p.clerk_issuer = %s
                  AND p.clerk_subject = %s AND sc.id = %s
                """,
                (
                    domain,
                    domain,
                    profile_id,
                    issuer,
                    subject,
                    source_connection_id,
                ),
            ).fetchone()
        return _StoredStatus(*row) if row is not None else None

    def source_overview_for_actor(
        self, issuer: str, subject: str, provider: str
    ) -> _StoredSourceOverview | None:
        provider = _required_text(provider, "provider")
        with psycopg.connect(self._settings.url) as connection:
            source = connection.execute(
                """
                SELECT sc.profile_id, sc.id, sc.provider, sc.state,
                       sc.encrypted_credentials IS NOT NULL,
                       sc.last_authenticated_at, sc.reconnect_safe_code
                FROM source_connections AS sc
                JOIN profiles AS p ON p.id = sc.profile_id
                WHERE p.clerk_issuer = %s AND p.clerk_subject = %s
                  AND sc.provider = %s
                ORDER BY sc.created_at, sc.id
                LIMIT 1
                """,
                (issuer, subject, provider),
            ).fetchone()
            if source is None:
                return None
            latest = connection.execute(
                """
                SELECT profile_id, id, source_connection_id, kind, state,
                       safe_code, created_at, started_at, finished_at
                FROM collection_jobs
                WHERE profile_id = %s AND source_connection_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (source[0], source[1]),
            ).fetchone()
            statuses: dict[str, _StoredStatus] = {}
            for domain in ("initial_sync", "incremental"):
                row = connection.execute(
                    """
                    SELECT sc.state, h.outcome, h.safe_code, h.attempted_at,
                           h.finished_at, h.last_success_at, cp.revision
                    FROM source_connections AS sc
                    LEFT JOIN collection_health AS h
                      ON h.profile_id = sc.profile_id
                     AND h.source_connection_id = sc.id
                     AND h.domain = %s
                    LEFT JOIN collection_checkpoints AS cp
                      ON cp.profile_id = sc.profile_id
                     AND cp.source_connection_id = sc.id
                     AND cp.domain = %s
                    WHERE sc.profile_id = %s AND sc.id = %s
                    """,
                    (domain, domain, source[0], source[1]),
                ).fetchone()
                assert row is not None
                statuses[domain] = _StoredStatus(*row)
        return _StoredSourceOverview(
            profile_id=source[0],
            source_connection_id=source[1],
            provider=source[2],
            state=source[3],
            credentials_stored=source[4],
            last_authenticated_at=source[5],
            reconnect_safe_code=source[6],
            latest_job=_StoredJob(*latest) if latest is not None else None,
            initial_sync=statuses["initial_sync"],
            incremental=statuses["incremental"],
        )

    @contextmanager
    def locked_profile(self, profile_id: UUID) -> Iterator[psycopg.Connection]:
        lock_key = int.from_bytes(
            hashlib.sha256(profile_id.bytes).digest()[:8],
            byteorder="big",
            signed=True,
        )
        connection = psycopg.connect(self._settings.url, autocommit=True)
        try:
            connection.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
            yield connection
        finally:
            try:
                connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
            finally:
                connection.close()

    def start_job(
        self,
        connection: psycopg.Connection,
        profile_id: UUID,
        job_id: UUID,
    ) -> _RunLease | _StoredJob:
        recovered = False
        with connection.transaction():
            row = connection.execute(
                """
                SELECT j.profile_id, j.id, j.source_connection_id, j.kind,
                       j.state, j.safe_code, j.created_at, j.started_at,
                       j.finished_at, sc.encrypted_credentials,
                       sc.encrypted_tokens, sc.state, sc.reconnect_safe_code
                FROM collection_jobs AS j
                JOIN source_connections AS sc
                  ON sc.profile_id = j.profile_id
                 AND sc.id = j.source_connection_id
                WHERE j.profile_id = %s AND j.id = %s
                FOR UPDATE OF j
                """,
                (profile_id, job_id),
            ).fetchone()
            if row is None:
                raise _ProfileNotFound("collection job is not available")
            job = _StoredJob(*row[:9])
            if job.state == "running":
                active = connection.execute(
                    """
                    SELECT r.id, a.id
                    FROM collection_runs AS r
                    JOIN collection_attempts AS a
                      ON a.profile_id = r.profile_id AND a.run_id = r.id
                    WHERE r.profile_id = %s AND r.job_id = %s
                      AND r.state = 'running' AND a.state = 'running'
                    """,
                    (profile_id, job_id),
                ).fetchone()
                if active is None:
                    raise CaptureStoreError("running collection evidence is incomplete")
                lease = _RunLease(
                    job=job,
                    run_id=active[0],
                    attempt_id=active[1],
                    encrypted_credentials=row[9],
                    encrypted_tokens=row[10],
                    source_state=row[11],
                    reconnect_safe_code=row[12],
                )
                diagnostics = {
                    "attempt": 1,
                    "cached_tokens": row[10] is not None,
                }
                self._finish_rows(
                    connection, lease, "failed", "worker_lost", diagnostics
                )
                # Health is aggregate source/domain state, not job evidence.
                # Recover it only when it still represents this job's start;
                # a newer job may already have replaced it and succeeded.
                connection.execute(
                    """
                    UPDATE collection_health
                    SET outcome = 'failed', finished_at = now(),
                        safe_code = 'worker_lost', diagnostics = %s
                    WHERE profile_id = %s AND source_connection_id = %s
                      AND domain = %s AND outcome = 'running'
                      AND collection_job_id = %s
                    """,
                    (
                        Jsonb(diagnostics),
                        profile_id,
                        job.source_connection_id,
                        job.kind,
                        job.id,
                    ),
                )
                recovered = True
            elif job.state != "requested":
                return job
            else:
                updated = connection.execute(
                    """
                    UPDATE collection_jobs
                    SET state = 'running', revision = revision + 1,
                        started_at = now(), safe_code = NULL,
                        diagnostics = '{}'::jsonb
                    WHERE profile_id = %s AND id = %s
                      AND state = 'requested'
                    RETURNING started_at
                    """,
                    (profile_id, job_id),
                ).fetchone()
                if updated is None:
                    raise CaptureStoreError("collection job compare-and-set failed")
                run_id = uuid4()
                attempt_id = uuid4()
                cached = row[10] is not None
                diagnostics = Jsonb({"attempt": 1, "cached_tokens": cached})
                connection.execute(
                    """
                    INSERT INTO collection_runs (
                        profile_id, id, job_id, run_number, state, diagnostics
                    ) VALUES (%s, %s, %s, 1, 'running', %s)
                    """,
                    (profile_id, run_id, job_id, diagnostics),
                )
                connection.execute(
                    """
                    INSERT INTO collection_attempts (
                        profile_id, id, run_id, attempt_number, state, diagnostics
                    ) VALUES (%s, %s, %s, 1, 'running', %s)
                    """,
                    (profile_id, attempt_id, run_id, diagnostics),
                )
                connection.execute(
                    """
                    INSERT INTO collection_health (
                        profile_id, source_connection_id, domain,
                        collection_job_id, outcome, attempted_at,
                        finished_at, safe_code, diagnostics
                    ) VALUES (
                        %s, %s, %s, %s, 'running', now(), NULL, NULL, %s
                    )
                    ON CONFLICT (profile_id, source_connection_id, domain)
                    DO UPDATE SET collection_job_id = EXCLUDED.collection_job_id,
                                  outcome = 'running', attempted_at = now(),
                                  finished_at = NULL, safe_code = NULL,
                                  diagnostics = EXCLUDED.diagnostics
                    """,
                    (
                        profile_id,
                        job.source_connection_id,
                        job.kind,
                        job.id,
                        diagnostics,
                    ),
                )
        if recovered:
            return self._job(connection, profile_id, job_id)
        running = self._job(connection, profile_id, job_id)
        return _RunLease(
            job=running,
            run_id=run_id,
            attempt_id=attempt_id,
            encrypted_credentials=row[9],
            encrypted_tokens=row[10],
            source_state=row[11],
            reconnect_safe_code=row[12],
        )

    def checkpoint_cursor(
        self,
        connection: psycopg.Connection,
        profile_id: UUID,
        source_connection_id: UUID,
        domain: str,
    ) -> Mapping[str, Any] | None:
        if domain not in {"initial_sync", "incremental"}:
            raise CaptureStoreError("collection checkpoint domain is invalid")
        domains = (domain,) if domain == "initial_sync" else (domain, "initial_sync")
        row = connection.execute(
            """
            SELECT cursor
            FROM collection_checkpoints
            WHERE profile_id = %s AND source_connection_id = %s
              AND domain = ANY(%s)
            ORDER BY CASE WHEN domain = %s THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (profile_id, source_connection_id, list(domains), domain),
        ).fetchone()
        if row is None:
            return None
        return _safe_json_object(row[0], "cursor")

    def persist_rotated_tokens(
        self,
        connection: psycopg.Connection,
        profile_id: UUID,
        source_connection_id: UUID,
        encrypted_tokens: EncryptedBlob,
    ) -> None:
        if not isinstance(encrypted_tokens, EncryptedBlob):
            raise CaptureStoreError("rotated tokens must be encrypted")
        with connection.transaction():
            updated = connection.execute(
                """
                UPDATE source_connections
                SET encrypted_tokens = %s, state = 'connected',
                    state_revision = state_revision + 1,
                    last_authenticated_at = now(), reconnect_safe_code = NULL,
                    reconnect_at = NULL, updated_at = now()
                WHERE profile_id = %s AND id = %s
                """,
                (encrypted_tokens.envelope, profile_id, source_connection_id),
            ).rowcount
            if updated != 1:
                raise _AccessDenied("source connection is not available")

    def complete_job(
        self,
        connection: psycopg.Connection,
        lease: _RunLease,
        cursor: Mapping[str, Any],
        diagnostics: Mapping[str, Any],
    ) -> _StoredJob:
        cursor = _safe_json_object(cursor, "cursor")
        diagnostics = _safe_diagnostics(diagnostics)
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO collection_checkpoints (
                    profile_id, source_connection_id, domain, cursor,
                    revision, updated_at, last_success_at
                ) VALUES (%s, %s, %s, %s, 0, now(), now())
                ON CONFLICT (profile_id, source_connection_id, domain)
                DO UPDATE SET cursor = EXCLUDED.cursor,
                              revision = collection_checkpoints.revision + 1,
                              updated_at = now(), last_success_at = now()
                """,
                (
                    lease.job.profile_id,
                    lease.job.source_connection_id,
                    lease.job.kind,
                    Jsonb(cursor),
                ),
            )
            self._finish_rows(
                connection, lease, "succeeded", None, diagnostics
            )
            connection.execute(
                """
                UPDATE collection_health
                SET outcome = 'successful', finished_at = now(),
                    last_success_at = now(), safe_code = NULL,
                    diagnostics = %s
                WHERE profile_id = %s AND source_connection_id = %s
                  AND domain = %s AND outcome = 'running'
                  AND collection_job_id = %s
                """,
                (
                    Jsonb(diagnostics),
                    lease.job.profile_id,
                    lease.job.source_connection_id,
                    lease.job.kind,
                    lease.job.id,
                ),
            )
        return self._job(connection, lease.job.profile_id, lease.job.id)

    def fail_job(
        self,
        connection: psycopg.Connection,
        lease: _RunLease,
        code: str,
        *,
        needs_reconnect: bool,
    ) -> _StoredJob:
        if code not in _SAFE_CODES:
            code = "external_failure"
        diagnostics = {"attempt": 1, "cached_tokens": lease.encrypted_tokens is not None}
        with connection.transaction():
            if needs_reconnect:
                connection.execute(
                    """
                    UPDATE source_connections
                    SET state = 'needs_reconnect',
                        state_revision = state_revision + 1,
                        reconnect_safe_code = %s, reconnect_at = now(),
                        updated_at = now()
                    WHERE profile_id = %s AND id = %s
                    """,
                    (
                        code,
                        lease.job.profile_id,
                        lease.job.source_connection_id,
                    ),
                )
            self._finish_rows(connection, lease, "failed", code, diagnostics)
            connection.execute(
                """
                UPDATE collection_health
                SET outcome = 'failed', finished_at = now(), safe_code = %s,
                    diagnostics = %s
                WHERE profile_id = %s AND source_connection_id = %s
                  AND domain = %s AND outcome = 'running'
                  AND collection_job_id = %s
                """,
                (
                    code,
                    Jsonb(diagnostics),
                    lease.job.profile_id,
                    lease.job.source_connection_id,
                    lease.job.kind,
                    lease.job.id,
                ),
            )
        return self._job(connection, lease.job.profile_id, lease.job.id)

    def _finish_rows(
        self,
        connection: psycopg.Connection,
        lease: _RunLease,
        state: str,
        code: str | None,
        diagnostics: Mapping[str, Any],
    ) -> None:
        parameters = (state, code, Jsonb(dict(diagnostics)))
        attempt_count = connection.execute(
            """
            UPDATE collection_attempts
            SET state = %s, safe_code = %s, diagnostics = %s,
                finished_at = now()
            WHERE profile_id = %s AND id = %s AND state = 'running'
            """,
            (*parameters, lease.job.profile_id, lease.attempt_id),
        ).rowcount
        run_count = connection.execute(
            """
            UPDATE collection_runs
            SET state = %s, safe_code = %s, diagnostics = %s,
                finished_at = now()
            WHERE profile_id = %s AND id = %s AND state = 'running'
            """,
            (*parameters, lease.job.profile_id, lease.run_id),
        ).rowcount
        job_count = connection.execute(
            """
            UPDATE collection_jobs
            SET state = %s, safe_code = %s, diagnostics = %s,
                revision = revision + 1, finished_at = now()
            WHERE profile_id = %s AND id = %s AND state = 'running'
            """,
            (*parameters, lease.job.profile_id, lease.job.id),
        ).rowcount
        if (attempt_count, run_count, job_count) != (1, 1, 1):
            raise CaptureStoreError("collection state compare-and-set failed")

    @staticmethod
    def _job(
        connection: psycopg.Connection, profile_id: UUID, job_id: UUID
    ) -> _StoredJob:
        row = connection.execute(
            """
            SELECT profile_id, id, source_connection_id, kind, state,
                   safe_code, created_at, started_at, finished_at
            FROM collection_jobs
            WHERE profile_id = %s AND id = %s
            """,
            (profile_id, job_id),
        ).fetchone()
        if row is None:
            raise _ProfileNotFound("collection job is not available")
        return _StoredJob(*row)


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CaptureStoreError(f"{name} is required")
    return value.strip()


def _safe_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CaptureStoreError("cursor must contain finite JSON values")
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise CaptureStoreError("cursor keys must be text")
        return {key: _safe_json(item) for key, item in value.items()}
    raise CaptureStoreError("cursor must contain JSON values")


def _safe_json_object(
    value: Mapping[str, Any], name: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CaptureStoreError(f"{name} must be an object")
    normalized = _safe_json(value)
    assert isinstance(normalized, dict)
    return normalized


def _safe_diagnostics(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _safe_json_object(value, "diagnostics")
    if not set(normalized).issubset(
        {"attempt", "cached_tokens", "captures", "sessions", "observations"}
    ):
        raise CaptureStoreError("diagnostics contain unsupported fields")
    for key in ("attempt", "captures", "sessions", "observations"):
        if key in normalized and (
            isinstance(normalized[key], bool)
            or not isinstance(normalized[key], int)
            or normalized[key] < 0
        ):
            raise CaptureStoreError("diagnostics counts must be non-negative integers")
    if "cached_tokens" in normalized and not isinstance(
        normalized["cached_tokens"], bool
    ):
        raise CaptureStoreError("cached_tokens diagnostic must be boolean")
    return normalized
