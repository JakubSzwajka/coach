"""Authoritative PostgreSQL application module for Garmin Coach.

Callers learn three operations only: ``read(actor, query)``,
``execute(actor, command)``, and ``ingest(profile, batch)``. Actor resolution,
tenancy, validation, transactions, persistence, and stable error mapping stay
behind this interface. Adapters never receive database connections or table
names, and this module never falls back to the pre-cutover file store.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence, Union, overload
from uuid import UUID

import psycopg

from .postgres._capture_store import (
    CaptureInput,
    CaptureStore,
    CaptureStoreError,
    ObservationInput,
    RecordPointerInput,
    SessionLoadInput,
    TrainingSessionInput,
    _AccessDenied,
    _IdempotencyConflict,
    _ProfileNotFound,
    _StaleRevision,
    _StoredProfile,
)
from .postgres._collection_store import (
    CollectionStore,
    RepairableAuthenticationError,
    TransientCollectionError,
    _RunLease,
    _StoredJob,
)
from .postgres._app_record_store import (
    AppRecordStore,
    AppRecordStoreError,
    _AppRecordConflict,
    _AppRecordNotFound,
    _AppRecordReadOnly,
    _AppRecordStale,
    _training_session_id,
)
from .postgres.config import DatabaseSettings
from .postgres.encryption import EncryptedBlob


class CoachApplicationError(RuntimeError):
    """Stable, payload-free error exposed at the application seam."""


class InvalidRequest(CoachApplicationError):
    pass


class NotFound(CoachApplicationError):
    pass


class AccessDenied(CoachApplicationError):
    pass


class StaleRevision(CoachApplicationError):
    pass


class Conflict(CoachApplicationError):
    pass


class ApplicationUnavailable(CoachApplicationError):
    pass


class CollectionFailed(CoachApplicationError):
    pass


@dataclass(frozen=True, slots=True)
class ClerkActor:
    issuer: str = field(repr=False)
    subject: str = field(repr=False)

    def __repr__(self) -> str:
        return "ClerkActor(<redacted>)"


@dataclass(frozen=True, slots=True, init=False)
class ProfileRef:
    """Opaque Profile capability returned by this module for internal ingest."""

    _id: UUID

    def __init__(self, profile_id: UUID) -> None:
        raise TypeError("ProfileRef values are issued by CoachApplication")

    @classmethod
    def _from_uuid(cls, profile_id: UUID) -> "ProfileRef":
        instance = object.__new__(cls)
        object.__setattr__(instance, "_id", profile_id)
        return instance

    def __repr__(self) -> str:
        return "ProfileRef(<opaque>)"


@dataclass(frozen=True, slots=True, init=False)
class SourceConnectionRef:
    """Opaque, profile-bound source capability issued by this module."""

    _profile_id: UUID
    _id: UUID

    def __init__(self, profile_id: UUID, connection_id: UUID) -> None:
        raise TypeError(
            "SourceConnectionRef values are issued by CoachApplication"
        )

    @classmethod
    def _from_uuids(
        cls, profile_id: UUID, connection_id: UUID
    ) -> "SourceConnectionRef":
        instance = object.__new__(cls)
        object.__setattr__(instance, "_profile_id", profile_id)
        object.__setattr__(instance, "_id", connection_id)
        return instance

    def __repr__(self) -> str:
        return "SourceConnectionRef(<opaque>)"


@dataclass(frozen=True, slots=True, init=False)
class CollectionJobRef:
    """Opaque actor-scoped collection job capability."""

    _profile_id: UUID
    _id: UUID

    def __init__(self, profile_id: UUID, job_id: UUID) -> None:
        raise TypeError("CollectionJobRef values are issued by CoachApplication")

    @classmethod
    def _from_uuids(cls, profile_id: UUID, job_id: UUID) -> "CollectionJobRef":
        instance = object.__new__(cls)
        object.__setattr__(instance, "_profile_id", profile_id)
        object.__setattr__(instance, "_id", job_id)
        return instance

    def __repr__(self) -> str:
        return "CollectionJobRef(<opaque>)"


@dataclass(frozen=True, slots=True)
class ProfileView:
    profile: ProfileRef
    display_name: str | None = field(repr=False)
    revision: int

    def __repr__(self) -> str:
        return f"ProfileView(profile={self.profile!r}, revision={self.revision})"


@dataclass(frozen=True, slots=True)
class CollectionSummaryView:
    profile: ProfileRef
    records: int
    captures: int
    latest_collected_at: datetime | None = field(repr=False)

    def __repr__(self) -> str:
        return (
            "CollectionSummaryView("
            f"profile={self.profile!r}, records={self.records}, "
            f"captures={self.captures})"
        )


@dataclass(frozen=True, slots=True)
class SessionLoadView:
    method: str = field(repr=False)
    unit: str = field(repr=False)
    value: Any = field(repr=False)
    source: str = field(repr=False)

    def __repr__(self) -> str:
        return "SessionLoadView(<redacted>)"


@dataclass(frozen=True, slots=True)
class TrainingSessionView:
    id: str
    local_date: date = field(repr=False)
    sport: str = field(repr=False)
    timing_precision: str = field(repr=False)
    local_start: datetime | None = field(repr=False)
    time_zone: str | None = field(repr=False)
    utc_offset: str | None = field(repr=False)
    session_type: str | None = field(repr=False)
    title: str | None = field(repr=False)
    session_rpe: int | None = field(repr=False)
    duration_value: Any | None = field(repr=False)
    duration_unit: str | None = field(repr=False)
    duration_basis: str | None = field(repr=False)
    distance_value: Any | None = field(repr=False)
    distance_unit: str | None = field(repr=False)
    notes: str | None = field(repr=False)
    loads: tuple[SessionLoadView, ...] = field(repr=False)

    def __repr__(self) -> str:
        return "TrainingSessionView(<redacted>)"


@dataclass(frozen=True, slots=True)
class ControlledObservationView:
    definition: str = field(repr=False)
    status: str = field(repr=False)
    value: Any | None = field(repr=False)
    value_type: str = field(repr=False)
    unit: str = field(repr=False)
    window_kind: str = field(repr=False)
    method: str = field(repr=False)
    local_date: date | None = field(repr=False)
    observed_at: datetime | None = field(repr=False)
    window_start: datetime | None = field(repr=False)
    window_end: datetime | None = field(repr=False)
    provenance: Mapping[str, Any] = field(repr=False)

    def __repr__(self) -> str:
        return "ControlledObservationView(<redacted>)"


@dataclass(frozen=True, slots=True)
class CollectedRecordView:
    captures: int
    session: TrainingSessionView | None = field(repr=False)
    observations: tuple[ControlledObservationView, ...] = field(repr=False)

    def __repr__(self) -> str:
        return f"CollectedRecordView(captures={self.captures}, <redacted>)"


@dataclass(frozen=True, slots=True)
class CollectionJobView:
    job: CollectionJobRef = field(repr=False)
    kind: str = field(repr=False)
    state: str
    safe_code: str | None
    created_at: datetime = field(repr=False)
    started_at: datetime | None = field(default=None, repr=False)
    finished_at: datetime | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return (
            "CollectionJobView("
            f"state={self.state!r}, safe_code={self.safe_code!r}, <redacted>)"
        )


@dataclass(frozen=True, slots=True)
class SourceStatusView:
    state: str
    outcome: str | None
    safe_code: str | None
    attempted_at: datetime | None = field(repr=False)
    finished_at: datetime | None = field(repr=False)
    last_success_at: datetime | None = field(repr=False)
    checkpoint_revision: int | None = field(repr=False)

    def __repr__(self) -> str:
        return (
            "SourceStatusView("
            f"state={self.state!r}, outcome={self.outcome!r}, "
            f"safe_code={self.safe_code!r}, <redacted>)"
        )


@dataclass(frozen=True, slots=True)
class GetProfile:
    pass


@dataclass(frozen=True, slots=True)
class GetCollectionSummary:
    pass


@dataclass(frozen=True, slots=True)
class GetCollectedRecord:
    source: SourceConnectionRef = field(repr=False)
    record_kind: str = field(repr=False)
    source_key: str = field(repr=False)

    def __repr__(self) -> str:
        return "GetCollectedRecord(<redacted>)"


@dataclass(frozen=True, slots=True)
class GetCollectionJob:
    job: CollectionJobRef


@dataclass(frozen=True, slots=True)
class GetSourceStatus:
    source: SourceConnectionRef
    domain: str = "initial_sync"


@dataclass(frozen=True, slots=True)
class TrainingSessionRecordView:
    id: str
    origin: str
    ownership: str
    content: Mapping[str, Any] = field(repr=False)
    provenance: Mapping[str, Any] = field(repr=False)
    created_at: datetime = field(repr=False)
    updated_at: datetime = field(repr=False)
    revision: int | None

    def __repr__(self) -> str:
        return f"TrainingSessionRecordView(id={self.id!r}, revision={self.revision}, <redacted>)"


@dataclass(frozen=True, slots=True)
class TrainingSessionRecordsView:
    sessions: tuple[TrainingSessionRecordView, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class SessionAnnotationView:
    id: str
    origin: str
    training_session_id: str
    notes: str | None = field(repr=False)
    reliability: str | None = field(repr=False)
    duplicate: bool | None = field(repr=False)
    provenance: Mapping[str, Any] = field(repr=False)
    created_at: datetime = field(repr=False)
    updated_at: datetime = field(repr=False)
    revision: int

    def __repr__(self) -> str:
        return f"SessionAnnotationView(id={self.id!r}, revision={self.revision}, <redacted>)"


@dataclass(frozen=True, slots=True)
class GoalEventView:
    id: str
    origin: str
    content: Mapping[str, Any] = field(repr=False)
    provenance: Mapping[str, Any] = field(repr=False)
    created_at: datetime = field(repr=False)
    updated_at: datetime = field(repr=False)
    revision: int

    def __repr__(self) -> str:
        return f"GoalEventView(id={self.id!r}, revision={self.revision}, <redacted>)"


@dataclass(frozen=True, slots=True)
class GoalEventsView:
    events: tuple[GoalEventView, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class TrainingPlanView:
    id: str
    origin: str
    content: Mapping[str, Any] = field(repr=False)
    provenance: Mapping[str, Any] = field(repr=False)
    created_at: datetime = field(repr=False)
    updated_at: datetime = field(repr=False)
    revision: int

    def __repr__(self) -> str:
        return f"TrainingPlanView(id={self.id!r}, revision={self.revision}, <redacted>)"


@dataclass(frozen=True, slots=True)
class TrainingPlansView:
    plans: tuple[TrainingPlanView, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class PlanRevisionView:
    revision: int
    kind: str
    recorded_at: datetime = field(repr=False)
    reason: str = field(repr=False)
    effective_from: str | None = field(repr=False)
    plan: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class TrainingPlanHistoryView:
    id: str
    revisions: tuple[PlanRevisionView, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class DeletedAppRecordView:
    id: str
    revision: int
    deleted: bool = True


@dataclass(frozen=True, slots=True)
class GetTrainingSession:
    id: str


@dataclass(frozen=True, slots=True)
class ListTrainingSessions:
    starts_on: date
    ends_on: date
    sport: str | None = None


@dataclass(frozen=True, slots=True)
class GetSessionAnnotation:
    id: str


@dataclass(frozen=True, slots=True)
class GetGoalEvent:
    id: str


@dataclass(frozen=True, slots=True)
class ListGoalEvents:
    sport: str | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class GetTrainingPlan:
    id: str


@dataclass(frozen=True, slots=True)
class ListTrainingPlans:
    status: str | None = None


@dataclass(frozen=True, slots=True)
class GetTrainingPlanHistory:
    id: str


Query = Union[
    GetProfile,
    GetCollectionSummary,
    GetCollectedRecord,
    GetCollectionJob,
    GetSourceStatus,
    GetTrainingSession,
    ListTrainingSessions,
    GetSessionAnnotation,
    GetGoalEvent,
    ListGoalEvents,
    GetTrainingPlan,
    ListTrainingPlans,
    GetTrainingPlanHistory,
]
ReadResult = Union[
    ProfileView,
    CollectionSummaryView,
    CollectedRecordView,
    CollectionJobView,
    SourceStatusView,
    TrainingSessionRecordView,
    TrainingSessionRecordsView,
    SessionAnnotationView,
    GoalEventView,
    GoalEventsView,
    TrainingPlanView,
    TrainingPlansView,
    TrainingPlanHistoryView,
    None,
]


@dataclass(frozen=True, slots=True)
class EnsureProfile:
    display_name: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class UpdateProfileDisplayName:
    expected_revision: int
    display_name: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class EnsureSourceConnection:
    provider: str
    connection_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SecretBundle:
    """Opaque plaintext accepted only at the application encryption seam."""

    _plaintext: bytes = field(repr=False)

    def __init__(self, plaintext: bytes) -> None:
        if not isinstance(plaintext, bytes) or not plaintext:
            raise TypeError("secret bundle must contain bytes")
        object.__setattr__(self, "_plaintext", plaintext)

    def __repr__(self) -> str:
        return "SecretBundle(<redacted>)"


@dataclass(frozen=True, slots=True)
class ConfigureSourceCredentials:
    source: SourceConnectionRef = field(repr=False)
    credentials: SecretBundle = field(repr=False)

    def __repr__(self) -> str:
        return "ConfigureSourceCredentials(<redacted>)"


@dataclass(frozen=True, slots=True)
class RequestCollection:
    source: SourceConnectionRef
    request_key: str = field(repr=False)
    kind: str = "initial_sync"


@dataclass(frozen=True, slots=True)
class RunCollectionJob:
    job: CollectionJobRef


@dataclass(frozen=True, slots=True)
class CreateTrainingSession:
    content: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReplaceTrainingSession:
    id: str
    expected_revision: int
    content: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class DeleteTrainingSession:
    id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class CreateSessionAnnotation:
    training_session_id: str
    notes: str | None = field(default=None, repr=False)
    reliability: str | None = field(default=None, repr=False)
    duplicate: bool | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ReplaceSessionAnnotation:
    id: str
    expected_revision: int
    notes: str | None = field(default=None, repr=False)
    reliability: str | None = field(default=None, repr=False)
    duplicate: bool | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DeleteSessionAnnotation:
    id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class CreateGoalEvent:
    content: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReplaceGoalEvent:
    id: str
    expected_revision: int
    content: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class DeleteGoalEvent:
    id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class CreateTrainingPlan:
    name: str = field(repr=False)
    starts_on: str = field(repr=False)
    ends_on: str = field(repr=False)
    reason: str = field(repr=False)
    goal_events: Sequence[Mapping[str, Any]] | None = field(default=None, repr=False)
    constraints: Sequence[str] | None = field(default=None, repr=False)
    planned_sessions: Sequence[Mapping[str, Any]] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ActivateTrainingPlan:
    id: str
    expected_revision: int
    reason: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ArchiveTrainingPlan:
    id: str
    expected_revision: int
    reason: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class DeleteTrainingPlan:
    id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class AdjustTrainingPlan:
    id: str
    expected_revision: int
    reason: str = field(repr=False)
    effective_from: str = field(repr=False)
    operations: Sequence[Mapping[str, Any]] = field(repr=False)
    name: str | None = field(default=None, repr=False)
    starts_on: str | None = field(default=None, repr=False)
    ends_on: str | None = field(default=None, repr=False)
    goal_events: Sequence[Mapping[str, Any]] | None = field(default=None, repr=False)
    constraints: Sequence[str] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SetPlannedSessionFulfilment:
    id: str
    expected_revision: int
    planned_session_id: str
    disposition: str
    reason: str = field(repr=False)
    matches: Sequence[str] | None = field(default=None, repr=False)
    fulfilment_note: str | None = field(default=None, repr=False)


Command = Union[
    EnsureProfile,
    UpdateProfileDisplayName,
    EnsureSourceConnection,
    ConfigureSourceCredentials,
    RequestCollection,
    RunCollectionJob,
    CreateTrainingSession,
    ReplaceTrainingSession,
    DeleteTrainingSession,
    CreateSessionAnnotation,
    ReplaceSessionAnnotation,
    DeleteSessionAnnotation,
    CreateGoalEvent,
    ReplaceGoalEvent,
    DeleteGoalEvent,
    CreateTrainingPlan,
    ActivateTrainingPlan,
    ArchiveTrainingPlan,
    DeleteTrainingPlan,
    AdjustTrainingPlan,
    SetPlannedSessionFulfilment,
]
ExecuteResult = Union[
    ProfileView,
    SourceConnectionRef,
    CollectionJobView,
    TrainingSessionRecordView,
    SessionAnnotationView,
    GoalEventView,
    TrainingPlanView,
    DeletedAppRecordView,
]


@dataclass(frozen=True, slots=True)
class CollectedCapture:
    record_kind: str
    source_key: str = field(repr=False)
    payload: Any = field(repr=False)
    source_at: datetime | None = field(default=None, repr=False)
    provenance: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return "CollectedCapture(<redacted>)"


@dataclass(frozen=True, slots=True)
class CapturePointer:
    record_kind: str = field(repr=False)
    source_key: str = field(repr=False)

    def __repr__(self) -> str:
        return "CapturePointer(<redacted>)"


@dataclass(frozen=True, slots=True)
class SessionLoad:
    method: str = field(repr=False)
    unit: str = field(repr=False)
    value: Any = field(repr=False)
    source: str = field(repr=False)

    def __repr__(self) -> str:
        return "SessionLoad(<redacted>)"


@dataclass(frozen=True, slots=True)
class CollectedTrainingSession:
    capture: CapturePointer = field(repr=False)
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
    loads: Sequence[SessionLoad] = field(default_factory=tuple, repr=False)

    def __repr__(self) -> str:
        return "CollectedTrainingSession(<redacted>)"


@dataclass(frozen=True, slots=True)
class ControlledObservation:
    capture: CapturePointer
    definition: str
    value_type: str
    unit: str
    window_kind: str
    method: str
    status: str
    value: Any | None = None
    local_date: date | None = None
    observed_at: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return "ControlledObservation(<redacted>)"


@dataclass(frozen=True, slots=True)
class CollectedBatch:
    source: SourceConnectionRef
    captures: Sequence[CollectedCapture] = field(repr=False)
    idempotency_key: str | None = field(default=None, repr=False)
    sessions: Sequence[CollectedTrainingSession] = field(
        default_factory=tuple, repr=False
    )
    observations: Sequence[ControlledObservation] = field(
        default_factory=tuple, repr=False
    )

    def __repr__(self) -> str:
        return f"CollectedBatch(source={self.source!r}, <redacted>)"


@dataclass(frozen=True, slots=True)
class IngestResult:
    inserted: int
    unchanged: int
    sessions: int = 0
    observations: int = 0


@dataclass(frozen=True, slots=True)
class CollectionCheckpoint:
    domain: str
    cursor: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _AuthenticatedTokenSession:
    handle: Any = field(repr=False)
    rotated_tokens: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class _ExternalCollection:
    batch: CollectedBatch = field(repr=False)
    checkpoint: CollectionCheckpoint = field(repr=False)


class _CollectionAdapter(Protocol):
    def authenticate(
        self,
        token_directory: Path,
        credentials: bytes | None,
        cached_tokens: bool,
    ) -> _AuthenticatedTokenSession: ...

    def collect(
        self, session: Any, kind: str
    ) -> _ExternalCollection: ...


class CoachApplication:
    """Deep application module backed only by PostgreSQL."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        collection_adapter: _CollectionAdapter | None = None,
        encryption_key: bytes | None = None,
        temporary_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.__store = CaptureStore(settings)
        self.__collections = CollectionStore(settings)
        self.__app_records = AppRecordStore(settings, clock=clock)
        self.__adapter = collection_adapter
        self.__encryption_key = encryption_key
        self.__temporary_root = temporary_root

    @overload
    def read(self, actor: ClerkActor, query: GetProfile) -> ProfileView | None: ...

    @overload
    def read(
        self, actor: ClerkActor, query: GetCollectionSummary
    ) -> CollectionSummaryView | None: ...

    def read(self, actor: ClerkActor, query: Query) -> ReadResult:
        actor = _actor(actor)
        with _stable_errors():
            if isinstance(query, GetProfile):
                stored = self.__store.get_profile(actor.issuer, actor.subject)
                return _profile_view(stored) if stored is not None else None
            if isinstance(query, GetCollectionSummary):
                result = self.__store.collection_summary(
                    actor.issuer, actor.subject
                )
                if result is None:
                    return None
                profile_id, summary = result
                return CollectionSummaryView(
                    profile=ProfileRef._from_uuid(profile_id),
                    records=summary.records,
                    captures=summary.captures,
                    latest_collected_at=summary.latest_collected_at,
                )
            if isinstance(query, GetCollectedRecord):
                if not isinstance(query.source, SourceConnectionRef):
                    raise InvalidRequest("a source capability is required")
                projection = self.__store.collected_projection_for_actor(
                    actor.issuer,
                    actor.subject,
                    query.source._id,
                    query.record_kind,
                    query.source_key,
                )
                if projection is None:
                    return None
                session = None
                if projection.session is not None:
                    stored = projection.session
                    session = TrainingSessionView(
                        id=_training_session_id(stored.id, "collected"),
                        local_date=stored.local_date,
                        sport=stored.sport,
                        timing_precision=stored.timing_precision,
                        local_start=stored.local_start,
                        time_zone=stored.time_zone,
                        utc_offset=stored.utc_offset,
                        session_type=stored.session_type,
                        title=stored.title,
                        session_rpe=stored.session_rpe,
                        duration_value=stored.duration_value,
                        duration_unit=stored.duration_unit,
                        duration_basis=stored.duration_basis,
                        distance_value=stored.distance_value,
                        distance_unit=stored.distance_unit,
                        notes=stored.notes,
                        loads=tuple(
                            SessionLoadView(
                                load.method, load.unit, load.value, load.source
                            )
                            for load in stored.loads
                        ),
                    )
                return CollectedRecordView(
                    captures=projection.captures,
                    session=session,
                    observations=tuple(
                        ControlledObservationView(
                            definition=item.definition,
                            status=item.status,
                            value=item.value,
                            value_type=item.value_type,
                            unit=item.unit,
                            window_kind=item.window_kind,
                            method=item.method,
                            local_date=item.local_date,
                            observed_at=item.observed_at,
                            window_start=item.window_start,
                            window_end=item.window_end,
                            provenance=item.provenance,
                        )
                        for item in projection.observations
                    ),
                )
            if isinstance(query, GetCollectionJob):
                if not isinstance(query.job, CollectionJobRef):
                    raise InvalidRequest("a collection job capability is required")
                job = self.__collections.job_for_actor(
                    actor.issuer,
                    actor.subject,
                    query.job._profile_id,
                    query.job._id,
                )
                return _job_view(job) if job is not None else None
            if isinstance(query, GetSourceStatus):
                if not isinstance(query.source, SourceConnectionRef):
                    raise InvalidRequest("a source capability is required")
                status = self.__collections.status_for_actor(
                    actor.issuer,
                    actor.subject,
                    query.source._profile_id,
                    query.source._id,
                    query.domain,
                )
                if status is None:
                    return None
                return SourceStatusView(
                    state=status.state,
                    outcome=status.outcome,
                    safe_code=status.safe_code,
                    attempted_at=status.attempted_at,
                    finished_at=status.finished_at,
                    last_success_at=status.last_success_at,
                    checkpoint_revision=status.checkpoint_revision,
                )
            if isinstance(query, GetTrainingSession):
                record = self.__app_records.get_training_session(
                    actor.issuer, actor.subject, query.id
                )
                return _training_session_record_view(record) if record else None
            if isinstance(query, ListTrainingSessions):
                records = self.__app_records.list_training_sessions(
                    actor.issuer,
                    actor.subject,
                    query.starts_on,
                    query.ends_on,
                    query.sport,
                )
                return TrainingSessionRecordsView(
                    tuple(_training_session_record_view(item) for item in records)
                )
            if isinstance(query, GetSessionAnnotation):
                record = self.__app_records.get_session_annotation(
                    actor.issuer, actor.subject, query.id
                )
                return _session_annotation_view(record) if record else None
            if isinstance(query, GetGoalEvent):
                record = self.__app_records.get_goal_event(
                    actor.issuer, actor.subject, query.id
                )
                return _goal_event_view(record) if record else None
            if isinstance(query, ListGoalEvents):
                records = self.__app_records.list_goal_events(
                    actor.issuer, actor.subject, query.sport, query.status
                )
                return GoalEventsView(tuple(_goal_event_view(item) for item in records))
            if isinstance(query, GetTrainingPlan):
                record = self.__app_records.get_training_plan(
                    actor.issuer, actor.subject, query.id
                )
                return _training_plan_view(record) if record else None
            if isinstance(query, ListTrainingPlans):
                records = self.__app_records.list_training_plans(
                    actor.issuer, actor.subject, query.status
                )
                return TrainingPlansView(
                    tuple(_training_plan_view(item) for item in records)
                )
            if isinstance(query, GetTrainingPlanHistory):
                record = self.__app_records.get_training_plan_history(
                    actor.issuer, actor.subject, query.id
                )
                return _training_plan_history_view(record) if record else None
            raise InvalidRequest("unsupported application query")

    @overload
    def execute(
        self, actor: ClerkActor, command: EnsureProfile
    ) -> ProfileView: ...

    @overload
    def execute(
        self, actor: ClerkActor, command: UpdateProfileDisplayName
    ) -> ProfileView: ...

    @overload
    def execute(
        self, actor: ClerkActor, command: EnsureSourceConnection
    ) -> SourceConnectionRef: ...

    @overload
    def execute(
        self, actor: ClerkActor, command: ConfigureSourceCredentials
    ) -> SourceConnectionRef: ...

    def execute(self, actor: ClerkActor, command: Command) -> ExecuteResult:
        actor = _actor(actor)
        with _stable_errors():
            if isinstance(command, EnsureProfile):
                self.__store.ensure_profile(
                    actor.issuer,
                    actor.subject,
                    command.display_name,
                )
                stored = self.__store.get_profile(actor.issuer, actor.subject)
                if stored is None:
                    raise ApplicationUnavailable("profile persistence is unavailable")
                return _profile_view(stored)
            if isinstance(command, UpdateProfileDisplayName):
                stored = self.__store.update_profile_display_name(
                    actor.issuer,
                    actor.subject,
                    command.display_name,
                    command.expected_revision,
                )
                return _profile_view(stored)
            if isinstance(command, EnsureSourceConnection):
                profile = self.__store.get_profile(actor.issuer, actor.subject)
                if profile is None:
                    raise _ProfileNotFound("profile is not available")
                connection_id = self.__store.ensure_source_connection(
                    profile.id,
                    command.provider,
                    command.connection_key,
                )
                return SourceConnectionRef._from_uuids(
                    profile.id, connection_id
                )
            if isinstance(command, ConfigureSourceCredentials):
                if not isinstance(command.source, SourceConnectionRef):
                    raise InvalidRequest("a source capability is required")
                if not isinstance(command.credentials, SecretBundle):
                    raise InvalidRequest("a secret bundle is required")
                if self.__encryption_key is None:
                    raise InvalidRequest("source credential encryption is not configured")
                encrypted = EncryptedBlob.encrypt(
                    command.credentials._plaintext, self.__encryption_key
                )
                with self.__collections.locked_profile(
                    command.source._profile_id
                ) as connection:
                    self.__store.replace_source_credentials_for_actor(
                        connection,
                        actor.issuer,
                        actor.subject,
                        command.source._profile_id,
                        command.source._id,
                        encrypted,
                    )
                return command.source
            if isinstance(command, RequestCollection):
                if not isinstance(command.source, SourceConnectionRef):
                    raise InvalidRequest("a source capability is required")
                job = self.__collections.request_for_actor(
                    actor.issuer,
                    actor.subject,
                    command.source._id,
                    command.request_key,
                    command.kind,
                )
                return _job_view(job)
            if isinstance(command, RunCollectionJob):
                if not isinstance(command.job, CollectionJobRef):
                    raise InvalidRequest("a collection job capability is required")
                return self.__run_collection_job(actor, command.job)
            if isinstance(command, CreateTrainingSession):
                return _training_session_record_view(
                    self.__app_records.create_training_session(
                        actor.issuer, actor.subject, command.content
                    )
                )
            if isinstance(command, ReplaceTrainingSession):
                return _training_session_record_view(
                    self.__app_records.replace_training_session(
                        actor.issuer,
                        actor.subject,
                        command.id,
                        command.expected_revision,
                        command.content,
                    )
                )
            if isinstance(command, DeleteTrainingSession):
                return _deleted_view(
                    self.__app_records.delete_training_session(
                        actor.issuer,
                        actor.subject,
                        command.id,
                        command.expected_revision,
                    )
                )
            if isinstance(command, CreateSessionAnnotation):
                return _session_annotation_view(
                    self.__app_records.create_session_annotation(
                        actor.issuer,
                        actor.subject,
                        command.training_session_id,
                        notes=command.notes,
                        reliability=command.reliability,
                        duplicate=command.duplicate,
                    )
                )
            if isinstance(command, ReplaceSessionAnnotation):
                return _session_annotation_view(
                    self.__app_records.replace_session_annotation(
                        actor.issuer,
                        actor.subject,
                        command.id,
                        command.expected_revision,
                        notes=command.notes,
                        reliability=command.reliability,
                        duplicate=command.duplicate,
                    )
                )
            if isinstance(command, DeleteSessionAnnotation):
                return _deleted_view(
                    self.__app_records.delete_session_annotation(
                        actor.issuer,
                        actor.subject,
                        command.id,
                        command.expected_revision,
                    )
                )
            if isinstance(command, CreateGoalEvent):
                return _goal_event_view(
                    self.__app_records.create_goal_event(
                        actor.issuer, actor.subject, command.content
                    )
                )
            if isinstance(command, ReplaceGoalEvent):
                return _goal_event_view(
                    self.__app_records.replace_goal_event(
                        actor.issuer,
                        actor.subject,
                        command.id,
                        command.expected_revision,
                        command.content,
                    )
                )
            if isinstance(command, DeleteGoalEvent):
                return _deleted_view(
                    self.__app_records.delete_goal_event(
                        actor.issuer,
                        actor.subject,
                        command.id,
                        command.expected_revision,
                    )
                )
            if isinstance(command, CreateTrainingPlan):
                return _training_plan_view(
                    self.__app_records.create_training_plan(
                        actor.issuer,
                        actor.subject,
                        command.name,
                        command.starts_on,
                        command.ends_on,
                        command.reason,
                        goal_events=command.goal_events,
                        constraints=command.constraints,
                        planned_sessions=command.planned_sessions,
                    )
                )
            if isinstance(command, ActivateTrainingPlan):
                return _training_plan_view(
                    self.__app_records.activate_training_plan(
                        actor.issuer,
                        actor.subject,
                        command.id,
                        command.expected_revision,
                        command.reason,
                    )
                )
            if isinstance(command, ArchiveTrainingPlan):
                return _training_plan_view(
                    self.__app_records.archive_training_plan(
                        actor.issuer,
                        actor.subject,
                        command.id,
                        command.expected_revision,
                        command.reason,
                    )
                )
            if isinstance(command, DeleteTrainingPlan):
                return _deleted_view(
                    self.__app_records.delete_training_plan(
                        actor.issuer,
                        actor.subject,
                        command.id,
                        command.expected_revision,
                    )
                )
            if isinstance(command, AdjustTrainingPlan):
                return _training_plan_view(
                    self.__app_records.adjust_training_plan(
                        actor.issuer,
                        actor.subject,
                        command.id,
                        command.expected_revision,
                        command.reason,
                        command.effective_from,
                        command.operations,
                        name=command.name,
                        starts_on=command.starts_on,
                        ends_on=command.ends_on,
                        goal_events=command.goal_events,
                        constraints=command.constraints,
                    )
                )
            if isinstance(command, SetPlannedSessionFulfilment):
                return _training_plan_view(
                    self.__app_records.set_planned_session_fulfilment(
                        actor.issuer,
                        actor.subject,
                        command.id,
                        command.expected_revision,
                        command.planned_session_id,
                        command.disposition,
                        command.reason,
                        command.matches,
                        command.fulfilment_note,
                    )
                )
            raise InvalidRequest("unsupported application command")

    def ingest(
        self, profile: ProfileRef, batch: CollectedBatch
    ) -> IngestResult:
        _validate_ingest_capabilities(profile, batch)
        with _stable_errors():
            result = self.__ingest_batch(profile, batch)
            return IngestResult(
                result.inserted,
                result.unchanged,
                result.sessions,
                result.observations,
            )

    def __ingest_batch(
        self,
        profile: ProfileRef,
        batch: CollectedBatch,
        *,
        connection: psycopg.Connection | None = None,
    ):
        captures, sessions, observations = _store_graph(batch)
        return self.__store.ingest_graph(
            profile._id,
            batch.source._id,
            idempotency_key=batch.idempotency_key,
            captures=captures,
            sessions=sessions,
            observations=observations,
            connection=connection,
        )

    def __run_collection_job(
        self, actor: ClerkActor, job_ref: CollectionJobRef
    ) -> CollectionJobView:
        if self.__adapter is None or self.__encryption_key is None:
            raise InvalidRequest("collection execution is not configured")
        job = self.__collections.job_for_actor(
            actor.issuer,
            actor.subject,
            job_ref._profile_id,
            job_ref._id,
        )
        if job is None:
            raise NotFound("requested record is not available")
        with self.__collections.locked_profile(job.profile_id) as connection:
            started = self.__collections.start_job(
                connection, job.profile_id, job.id
            )
            if isinstance(started, _StoredJob):
                return _job_view(started)
            lease = started
            if lease.source_state == "needs_reconnect":
                self.__collections.fail_job(
                    connection,
                    lease,
                    lease.reconnect_safe_code or "authentication_required",
                    needs_reconnect=True,
                )
                raise CollectionFailed("source authentication requires repair")
            try:
                return self.__collect_under_lease(connection, lease)
            except RepairableAuthenticationError as exc:
                self.__collections.fail_job(
                    connection, lease, exc.code, needs_reconnect=True
                )
                raise CollectionFailed("source authentication requires repair") from None
            except TransientCollectionError as exc:
                self.__collections.fail_job(
                    connection, lease, exc.code, needs_reconnect=False
                )
                raise CollectionFailed("source collection failed") from None
            except (CaptureStoreError, TypeError, ValueError):
                self.__collections.fail_job(
                    connection, lease, "invalid_response", needs_reconnect=False
                )
                raise CollectionFailed("source returned an invalid collection") from None
            except psycopg.Error:
                raise
            except Exception:
                self.__collections.fail_job(
                    connection, lease, "external_failure", needs_reconnect=False
                )
                raise CollectionFailed("source collection failed") from None

    def __collect_under_lease(
        self, connection: psycopg.Connection, lease: _RunLease
    ) -> CollectionJobView:
        assert self.__adapter is not None
        assert self.__encryption_key is not None
        root = str(self.__temporary_root) if self.__temporary_root else None
        with tempfile.TemporaryDirectory(
            prefix="garmin-coach-token-", dir=root
        ) as directory:
            token_directory = Path(directory)
            os.chmod(token_directory, 0o700)
            cached_tokens = lease.encrypted_tokens is not None
            token_file = token_directory / "tokens.bundle"
            if lease.encrypted_tokens is not None:
                token_bytes = EncryptedBlob.from_envelope(
                    lease.encrypted_tokens, self.__encryption_key
                ).decrypt(self.__encryption_key)
                token_file.write_bytes(token_bytes)
                os.chmod(token_file, 0o600)
                try:
                    authenticated = self.__adapter.authenticate(
                        token_directory, None, True
                    )
                except RepairableAuthenticationError:
                    if lease.encrypted_credentials is None:
                        raise
                    credentials = EncryptedBlob.from_envelope(
                        lease.encrypted_credentials, self.__encryption_key
                    ).decrypt(self.__encryption_key)
                    token_file.unlink(missing_ok=True)
                    authenticated = self.__adapter.authenticate(
                        token_directory, credentials, False
                    )
            elif lease.encrypted_credentials is not None:
                credentials = EncryptedBlob.from_envelope(
                    lease.encrypted_credentials, self.__encryption_key
                ).decrypt(self.__encryption_key)
                authenticated = self.__adapter.authenticate(
                    token_directory, credentials, False
                )
            else:
                raise RepairableAuthenticationError("authentication_required")
            if (
                not isinstance(authenticated, _AuthenticatedTokenSession)
                or not isinstance(authenticated.rotated_tokens, bytes)
                or not authenticated.rotated_tokens
            ):
                raise ValueError("invalid authenticated token session")
            rotated = EncryptedBlob.encrypt(
                authenticated.rotated_tokens, self.__encryption_key
            )
            # Deliberately commits before collection while the session-level
            # Profile advisory lock remains held.
            self.__collections.persist_rotated_tokens(
                connection,
                lease.job.profile_id,
                lease.job.source_connection_id,
                rotated,
            )
            collected = self.__adapter.collect(
                authenticated.handle, lease.job.kind
            )
            if (
                not isinstance(collected, _ExternalCollection)
                or not isinstance(collected.checkpoint, CollectionCheckpoint)
                or collected.checkpoint.domain != lease.job.kind
            ):
                raise ValueError("invalid external collection")
            profile = ProfileRef._from_uuid(lease.job.profile_id)
            _validate_ingest_capabilities(profile, collected.batch)
            if collected.batch.source._id != lease.job.source_connection_id:
                raise AccessDenied("source collection capability does not match the job")
            with connection.transaction():
                result = self.__ingest_batch(
                    profile, collected.batch, connection=connection
                )
                completed = self.__collections.complete_job(
                    connection,
                    lease,
                    collected.checkpoint.cursor,
                    {
                        "attempt": 1,
                        "cached_tokens": cached_tokens,
                        "captures": result.inserted + result.unchanged,
                        "sessions": result.sessions,
                        "observations": result.observations,
                    },
                )
            return _job_view(completed)


def _validate_ingest_capabilities(
    profile: ProfileRef, batch: CollectedBatch
) -> None:
    if (
        not isinstance(profile, ProfileRef)
        or not isinstance(batch, CollectedBatch)
        or not isinstance(batch.source, SourceConnectionRef)
        or not isinstance(batch.captures, Sequence)
        or not isinstance(batch.sessions, Sequence)
        or not isinstance(batch.observations, Sequence)
        or any(
            not isinstance(capture, CollectedCapture)
            for capture in batch.captures
        )
        or any(
            not isinstance(session, CollectedTrainingSession)
            or not isinstance(session.capture, CapturePointer)
            or not isinstance(session.loads, Sequence)
            or any(not isinstance(load, SessionLoad) for load in session.loads)
            for session in batch.sessions
        )
        or any(
            not isinstance(observation, ControlledObservation)
            or not isinstance(observation.capture, CapturePointer)
            for observation in batch.observations
        )
    ):
        raise InvalidRequest("invalid ingest request")
    if profile._id != batch.source._profile_id:
        raise AccessDenied("source connection is not owned by the Profile")


def _store_graph(batch: CollectedBatch):
    captures = tuple(
        CaptureInput(
            record_kind=capture.record_kind,
            source_key=capture.source_key,
            payload=capture.payload,
            source_at=capture.source_at,
            provenance=capture.provenance,
        )
        for capture in batch.captures
    )
    sessions = tuple(
        TrainingSessionInput(
            capture=RecordPointerInput(
                session.capture.record_kind, session.capture.source_key
            ),
            local_date=session.local_date,
            sport=session.sport,
            timing_precision=session.timing_precision,
            local_start=session.local_start,
            time_zone=session.time_zone,
            utc_offset=session.utc_offset,
            session_type=session.session_type,
            title=session.title,
            session_rpe=session.session_rpe,
            duration_value=session.duration_value,
            duration_unit=session.duration_unit,
            duration_basis=session.duration_basis,
            distance_value=session.distance_value,
            distance_unit=session.distance_unit,
            notes=session.notes,
            loads=tuple(
                SessionLoadInput(
                    load.method, load.unit, load.value, load.source
                )
                for load in session.loads
            ),
        )
        for session in batch.sessions
    )
    observations = tuple(
        ObservationInput(
            capture=RecordPointerInput(
                observation.capture.record_kind,
                observation.capture.source_key,
            ),
            definition=observation.definition,
            value_type=observation.value_type,
            unit=observation.unit,
            window_kind=observation.window_kind,
            method=observation.method,
            status=observation.status,
            value=observation.value,
            local_date=observation.local_date,
            observed_at=observation.observed_at,
            window_start=observation.window_start,
            window_end=observation.window_end,
            provenance=observation.provenance,
        )
        for observation in batch.observations
    )
    return captures, sessions, observations


def _job_view(job: _StoredJob) -> CollectionJobView:
    return CollectionJobView(
        job=CollectionJobRef._from_uuids(job.profile_id, job.id),
        kind=job.kind,
        state=job.state,
        safe_code=job.safe_code,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


@contextmanager
def _stable_errors() -> Iterator[None]:
    try:
        yield
    except CoachApplicationError:
        raise
    except _StaleRevision:
        raise StaleRevision("revision does not match current state") from None
    except _ProfileNotFound:
        raise NotFound("requested record is not available") from None
    except _AccessDenied:
        raise AccessDenied("requested record is not available") from None
    except _IdempotencyConflict:
        raise Conflict("idempotency key conflicts with existing state") from None
    except _AppRecordStale:
        raise StaleRevision("revision does not match current state") from None
    except _AppRecordNotFound:
        raise NotFound("requested record is not available") from None
    except _AppRecordReadOnly:
        raise Conflict("requested record is read-only") from None
    except _AppRecordConflict:
        raise Conflict("operation conflicts with current state") from None
    except AppRecordStoreError as exc:
        raise InvalidRequest(str(exc)) from None
    except CaptureStoreError as exc:
        raise InvalidRequest(str(exc)) from None
    except psycopg.DataError:
        raise InvalidRequest("request contains an invalid value") from None
    except psycopg.IntegrityError:
        raise Conflict("operation conflicts with current state") from None
    except psycopg.Error:
        raise ApplicationUnavailable("PostgreSQL is unavailable") from None


def _actor(actor: ClerkActor) -> ClerkActor:
    if not isinstance(actor, ClerkActor):
        raise InvalidRequest("a verified Clerk actor is required")
    return actor


def _profile_view(stored: _StoredProfile) -> ProfileView:
    return ProfileView(
        profile=ProfileRef._from_uuid(stored.id),
        display_name=stored.display_name,
        revision=stored.revision,
    )


def _training_session_record_view(
    record: Mapping[str, Any],
) -> TrainingSessionRecordView:
    envelope = {
        "id", "origin", "ownership", "provenance", "created_at",
        "updated_at", "revision",
    }
    return TrainingSessionRecordView(
        id=record["id"],
        origin=record["origin"],
        ownership=record["ownership"],
        content={key: value for key, value in record.items() if key not in envelope},
        provenance=record["provenance"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
        revision=record["revision"],
    )


def _session_annotation_view(record: Mapping[str, Any]) -> SessionAnnotationView:
    return SessionAnnotationView(
        id=record["id"],
        origin=record["origin"],
        training_session_id=record["training_session_id"],
        notes=record["notes"],
        reliability=record["reliability"],
        duplicate=record["duplicate"],
        provenance=record["provenance"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
        revision=record["revision"],
    )


def _goal_event_view(record: Mapping[str, Any]) -> GoalEventView:
    envelope = {"id", "origin", "provenance", "created_at", "updated_at", "revision"}
    return GoalEventView(
        id=record["id"],
        origin=record["origin"],
        content={key: value for key, value in record.items() if key not in envelope},
        provenance=record["provenance"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
        revision=record["revision"],
    )


def _training_plan_view(record: Mapping[str, Any]) -> TrainingPlanView:
    envelope = {"id", "origin", "provenance", "created_at", "updated_at", "revision"}
    return TrainingPlanView(
        id=record["id"],
        origin=record["origin"],
        content={key: value for key, value in record.items() if key not in envelope},
        provenance=record["provenance"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
        revision=record["revision"],
    )


def _training_plan_history_view(
    record: Mapping[str, Any],
) -> TrainingPlanHistoryView:
    return TrainingPlanHistoryView(
        id=record["id"],
        revisions=tuple(
            PlanRevisionView(
                revision=item["revision"],
                kind=item["kind"],
                recorded_at=item["recorded_at"],
                reason=item["reason"],
                effective_from=item["effective_from"],
                plan=item["plan"],
            )
            for item in record["revisions"]
        ),
    )


def _deleted_view(record: Mapping[str, Any]) -> DeletedAppRecordView:
    return DeletedAppRecordView(
        id=record["id"], revision=record["revision"], deleted=record["deleted"]
    )
