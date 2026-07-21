"""Legacy App Record contract adapter over the public CoachApplication seam.

This adapter exists only so the portable file-authority behavior suite can run
unchanged against disposable PostgreSQL. It translates DTOs and stable public
application errors; it never reaches into AppRecordStore or relational tables.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeVar
from uuid import uuid4

from coach.application import (
    ActivateTrainingPlan,
    AdjustTrainingPlan,
    ArchiveTrainingPlan,
    CapturePointer,
    ClerkActor,
    CoachApplication,
    CollectedBatch,
    CollectedCapture,
    CollectedTrainingSession,
    Conflict,
    CreateGoalEvent,
    CreateTrainingPlan,
    CreateTrainingSession,
    DeleteGoalEvent,
    DeleteTrainingPlan,
    DeleteTrainingSession,
    EnsureProfile,
    EnsureSourceConnection,
    GetCollectedRecord,
    GetGoalEvent,
    GetProfile,
    GetTrainingPlan,
    GetTrainingPlanHistory,
    GetTrainingSession,
    GoalEventView,
    InvalidRequest,
    ListGoalEvents,
    ListTrainingPlans,
    ListTrainingSessions,
    NotFound as ApplicationNotFound,
    PlanRevisionView,
    ReplaceGoalEvent,
    ReplaceTrainingSession,
    SetPlannedSessionFulfilment,
    StaleRevision,
    TrainingPlanView,
    TrainingSessionRecordView,
)
from coach.data import (
    ContextWindow,
    InvalidRecord,
    NotFound,
    ReadOnlyRecord,
    ReferencedRecord,
    RevisionConflict,
)
from coach.postgres import DatabaseSettings

_T = TypeVar("_T")


class CoachApplicationCompat:
    """CoachData-shaped test façade whose only authority is CoachApplication."""

    _settings: DatabaseSettings | None = None

    @classmethod
    def configure(cls, application_url: str) -> None:
        cls._settings = DatabaseSettings.from_url(application_url)

    def __init__(self, data_root: Path | str) -> None:
        if self._settings is None:
            raise RuntimeError("CoachApplicationCompat is not configured")
        self._root = Path(data_root)
        self._actor = ClerkActor(
            "https://identity.example.test", f"portable-{uuid4().hex}"
        )
        self._application = CoachApplication(
            self._settings,
            clock=lambda: datetime.now(timezone.utc),
        )
        self._application.execute(self._actor, EnsureProfile("Portable contract"))

    def _call(
        self,
        operation: Callable[[], _T],
        *,
        referenced_on_conflict: bool = False,
    ) -> _T:
        try:
            return operation()
        except StaleRevision as exc:
            raise RevisionConflict(str(exc)) from None
        except ApplicationNotFound as exc:
            raise NotFound(str(exc)) from None
        except InvalidRequest as exc:
            raise InvalidRecord(str(exc)) from None
        except Conflict as exc:
            if str(exc) == "requested record is read-only":
                raise ReadOnlyRecord(str(exc)) from None
            if referenced_on_conflict:
                raise ReferencedRecord(str(exc)) from None
            raise InvalidRecord(str(exc)) from None

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.isoformat()

    @classmethod
    def _session(cls, view: TrainingSessionRecordView) -> dict[str, Any]:
        return {
            "id": view.id,
            "origin": view.origin,
            **dict(view.content),
            "provenance": dict(view.provenance),
            "created_at": cls._timestamp(view.created_at),
            "updated_at": cls._timestamp(view.updated_at),
            "revision": view.revision,
        }

    @classmethod
    def _goal(cls, view: GoalEventView) -> dict[str, Any]:
        return {
            "id": view.id,
            "origin": view.origin,
            **dict(view.content),
            "provenance": dict(view.provenance),
            "created_at": cls._timestamp(view.created_at),
            "updated_at": cls._timestamp(view.updated_at),
            "revision": view.revision,
        }

    @classmethod
    def _plan(cls, view: TrainingPlanView) -> dict[str, Any]:
        return {
            "id": view.id,
            "origin": view.origin,
            **dict(view.content),
            "provenance": dict(view.provenance),
            "created_at": cls._timestamp(view.created_at),
            "updated_at": cls._timestamp(view.updated_at),
            "revision": view.revision,
        }

    @classmethod
    def _revision(cls, view: PlanRevisionView) -> dict[str, Any]:
        return {
            "revision": view.revision,
            "kind": view.kind,
            "recorded_at": cls._timestamp(view.recorded_at),
            "reason": view.reason,
            "effective_from": view.effective_from,
            "plan": dict(view.plan),
        }

    def create_session(self, content: Mapping[str, Any]) -> dict[str, Any]:
        return self._session(
            self._call(
                lambda: self._application.execute(
                    self._actor, CreateTrainingSession(content)
                )
            )
        )

    def get_session(self, session_id: str) -> dict[str, Any]:
        view = self._call(
            lambda: self._application.read(
                self._actor, GetTrainingSession(session_id)
            )
        )
        if view is None:
            raise NotFound("requested record is not available")
        return self._session(view)

    def list_sessions(
        self, window: ContextWindow, sport: str | None = None
    ) -> list[dict[str, Any]]:
        starts_on, ends_on = window.bounds()
        view = self._call(
            lambda: self._application.read(
                self._actor,
                ListTrainingSessions(starts_on, ends_on, sport),
            )
        )
        return [self._session(item) for item in view.sessions]

    def replace_session(
        self, session_id: str, expected_revision: int, content: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._session(
            self._call(
                lambda: self._application.execute(
                    self._actor,
                    ReplaceTrainingSession(session_id, expected_revision, content),
                )
            )
        )

    def delete_session(
        self, session_id: str, expected_revision: int
    ) -> dict[str, Any]:
        result = self._call(
            lambda: self._application.execute(
                self._actor, DeleteTrainingSession(session_id, expected_revision)
            ),
            referenced_on_conflict=True,
        )
        return {
            "id": result.id,
            "deleted": result.deleted,
            "revision": result.revision,
        }

    def create_goal_event(self, content: Mapping[str, Any]) -> dict[str, Any]:
        return self._goal(
            self._call(
                lambda: self._application.execute(
                    self._actor, CreateGoalEvent(content)
                )
            )
        )

    def get_goal_event(self, goal_event_id: str) -> dict[str, Any]:
        view = self._call(
            lambda: self._application.read(self._actor, GetGoalEvent(goal_event_id))
        )
        if view is None:
            raise NotFound("requested record is not available")
        return self._goal(view)

    def list_goal_events(
        self, *, sport: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        view = self._call(
            lambda: self._application.read(
                self._actor, ListGoalEvents(sport=sport, status=status)
            )
        )
        return [self._goal(item) for item in view.events]

    def replace_goal_event(
        self, goal_event_id: str, expected_revision: int, content: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._goal(
            self._call(
                lambda: self._application.execute(
                    self._actor,
                    ReplaceGoalEvent(goal_event_id, expected_revision, content),
                )
            )
        )

    def delete_goal_event(
        self, goal_event_id: str, expected_revision: int
    ) -> dict[str, Any]:
        result = self._call(
            lambda: self._application.execute(
                self._actor, DeleteGoalEvent(goal_event_id, expected_revision)
            ),
            referenced_on_conflict=True,
        )
        return {
            "id": result.id,
            "deleted": result.deleted,
            "revision": result.revision,
        }

    def create_plan(
        self,
        name: str,
        starts_on: str,
        ends_on: str,
        reason: str,
        *,
        goal_events: Sequence[Mapping[str, Any]] | None = None,
        constraints: Sequence[str] | None = None,
        planned_sessions: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self._plan(
            self._call(
                lambda: self._application.execute(
                    self._actor,
                    CreateTrainingPlan(
                        name,
                        starts_on,
                        ends_on,
                        reason,
                        goal_events,
                        constraints,
                        planned_sessions,
                    ),
                )
            )
        )

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        view = self._call(
            lambda: self._application.read(self._actor, GetTrainingPlan(plan_id))
        )
        if view is None:
            raise NotFound("requested record is not available")
        return self._plan(view)

    def list_plans(self, status: str | None = None) -> list[dict[str, Any]]:
        view = self._call(
            lambda: self._application.read(
                self._actor, ListTrainingPlans(status=status)
            )
        )
        return [self._plan(item) for item in view.plans]

    def get_plan_history(self, plan_id: str) -> dict[str, Any]:
        view = self._call(
            lambda: self._application.read(
                self._actor, GetTrainingPlanHistory(plan_id)
            )
        )
        if view is None:
            raise NotFound("requested record is not available")
        return {
            "id": view.id,
            "revisions": [self._revision(item) for item in view.revisions],
        }

    def activate_plan(
        self, plan_id: str, expected_revision: int, reason: str
    ) -> dict[str, Any]:
        return self._plan(
            self._call(
                lambda: self._application.execute(
                    self._actor,
                    ActivateTrainingPlan(plan_id, expected_revision, reason),
                )
            )
        )

    def archive_plan(
        self, plan_id: str, expected_revision: int, reason: str
    ) -> dict[str, Any]:
        return self._plan(
            self._call(
                lambda: self._application.execute(
                    self._actor,
                    ArchiveTrainingPlan(plan_id, expected_revision, reason),
                )
            )
        )

    def delete_plan(self, plan_id: str, expected_revision: int) -> dict[str, Any]:
        result = self._call(
            lambda: self._application.execute(
                self._actor, DeleteTrainingPlan(plan_id, expected_revision)
            )
        )
        return {
            "id": result.id,
            "deleted": result.deleted,
            "revision": result.revision,
        }

    def adjust_plan(
        self,
        plan_id: str,
        expected_revision: int,
        reason: str,
        effective_from: str,
        operations: Sequence[Mapping[str, Any]],
        *,
        name: str | None = None,
        starts_on: str | None = None,
        ends_on: str | None = None,
        goal_events: Sequence[Mapping[str, Any]] | None = None,
        constraints: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        return self._plan(
            self._call(
                lambda: self._application.execute(
                    self._actor,
                    AdjustTrainingPlan(
                        plan_id,
                        expected_revision,
                        reason,
                        effective_from,
                        operations,
                        name,
                        starts_on,
                        ends_on,
                        goal_events,
                        constraints,
                    ),
                )
            )
        )

    def set_planned_session_fulfilment(
        self,
        plan_id: str,
        expected_revision: int,
        planned_session_id: str,
        disposition: str,
        reason: str,
        matches: Sequence[str] | None = None,
        fulfilment_note: str | None = None,
    ) -> dict[str, Any]:
        return self._plan(
            self._call(
                lambda: self._application.execute(
                    self._actor,
                    SetPlannedSessionFulfilment(
                        plan_id,
                        expected_revision,
                        planned_session_id,
                        disposition,
                        reason,
                        matches,
                        fulfilment_note,
                    ),
                )
            )
        )

    def ingest_collected_session(
        self,
        *,
        local_date: date,
        sport: str,
        source_key: str,
    ) -> dict[str, Any]:
        source = self._application.execute(
            self._actor,
            EnsureSourceConnection("synthetic-provider", f"source-{source_key}"),
        )
        profile = self._application.read(self._actor, GetProfile()).profile
        self._application.ingest(
            profile,
            CollectedBatch(
                source=source,
                captures=(
                    CollectedCapture("activity", source_key, {"synthetic": True}),
                ),
                idempotency_key=f"batch-{source_key}",
                sessions=(
                    CollectedTrainingSession(
                        capture=CapturePointer("activity", source_key),
                        local_date=local_date,
                        sport=sport,
                    ),
                ),
            ),
        )
        projection = self._application.read(
            self._actor, GetCollectedRecord(source, "activity", source_key)
        )
        if projection is None or projection.session is None:
            raise AssertionError("synthetic collected session projection is missing")
        return self.get_session(projection.session.id)
