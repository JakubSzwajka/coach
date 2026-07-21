"""Authoritative PostgreSQL application module for Garmin Coach.

Callers learn three operations only: ``read(actor, query)``,
``execute(actor, command)``, and ``ingest(profile, batch)``. Actor resolution,
tenancy, validation, transactions, persistence, and stable error mapping stay
behind this interface. Adapters never receive database connections or table
names, and this module never falls back to the pre-cutover file store.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Mapping, Sequence, Union, overload
from uuid import UUID

import psycopg

from .postgres._capture_store import (
    CaptureInput,
    CaptureStore,
    CaptureStoreError,
    _AccessDenied,
    _ProfileNotFound,
    _StaleRevision,
    _StoredProfile,
)
from .postgres.config import DatabaseSettings


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
class GetProfile:
    pass


@dataclass(frozen=True, slots=True)
class GetCollectionSummary:
    pass


Query = Union[GetProfile, GetCollectionSummary]
ReadResult = Union[ProfileView, CollectionSummaryView, None]


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


Command = Union[
    EnsureProfile,
    UpdateProfileDisplayName,
    EnsureSourceConnection,
]
ExecuteResult = Union[ProfileView, SourceConnectionRef]


@dataclass(frozen=True, slots=True)
class CollectedCapture:
    record_kind: str
    source_key: str = field(repr=False)
    payload: Any = field(repr=False)
    source_at: datetime | None = field(default=None, repr=False)
    provenance: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return f"CollectedCapture(record_kind={self.record_kind!r}, <redacted>)"


@dataclass(frozen=True, slots=True)
class CollectedBatch:
    source: SourceConnectionRef
    captures: Sequence[CollectedCapture] = field(repr=False)

    def __repr__(self) -> str:
        return f"CollectedBatch(source={self.source!r}, <redacted>)"


@dataclass(frozen=True, slots=True)
class IngestResult:
    inserted: int
    unchanged: int


class CoachApplication:
    """Deep application module backed only by PostgreSQL."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self.__store = CaptureStore(settings)

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
                profile_id, connection_id = (
                    self.__store.ensure_source_connection_for_actor(
                        actor.issuer,
                        actor.subject,
                        command.provider,
                        command.connection_key,
                    )
                )
                return SourceConnectionRef._from_uuids(
                    profile_id, connection_id
                )
            raise InvalidRequest("unsupported application command")

    def ingest(
        self, profile: ProfileRef, batch: CollectedBatch
    ) -> IngestResult:
        if (
            not isinstance(profile, ProfileRef)
            or not isinstance(batch, CollectedBatch)
            or not isinstance(batch.source, SourceConnectionRef)
            or not isinstance(batch.captures, Sequence)
            or any(
                not isinstance(capture, CollectedCapture)
                for capture in batch.captures
            )
        ):
            raise InvalidRequest("invalid ingest request")
        if profile._id != batch.source._profile_id:
            raise AccessDenied("source connection is not owned by the Profile")
        with _stable_errors():
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
            result = self.__store.ingest_captures(
                profile._id,
                batch.source._id,
                captures,
            )
            return IngestResult(result.inserted, result.unchanged)


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
