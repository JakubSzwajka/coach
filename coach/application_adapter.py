"""In-process consumer adapter over the authoritative CoachApplication seam.

The adapter preserves the established MCP tool DTOs while translating them to
explicit application queries and commands.  It has no filesystem configuration
or fallback path.
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from .application import (
    ActivateTrainingPlan,
    AdjustTrainingPlan,
    ArchiveTrainingPlan,
    ClerkActor,
    CoachApplication,
    CollectionHealthView,
    Conflict,
    CreateGoalEvent,
    CreateTrainingPlan,
    CreateTrainingSession,
    DeleteGoalEvent,
    DeleteTrainingPlan,
    DeleteTrainingSession,
    EnsureProfile,
    GetCoachingContext,
    GetGoalEvent,
    GetProfile,
    GetTrainingPlan,
    GetTrainingPlanHistory,
    GetTrainingSession,
    GoalEventView,
    ListGoalEvents,
    ListTrainingPlans,
    ListTrainingSessions,
    NotFound,
    PlanRevisionView,
    ReplaceGoalEvent,
    ReplaceTrainingSession,
    SetPlannedSessionFulfilment,
    TrainingPlanView,
    TrainingSessionRecordView,
)
from .postgres import DatabaseSettings

_CURRENT_ACTOR: ContextVar[ClerkActor | None] = ContextVar(
    "garmin_coach_application_actor", default=None
)


@dataclass(frozen=True, slots=True)
class ContextWindow:
    days: int = 14
    end_date: date | None = None

    def bounds(self) -> tuple[date, date]:
        if isinstance(self.days, bool) or not isinstance(self.days, int) or not 1 <= self.days <= 90:
            raise ValueError("days must be between 1 and 90")
        end = self.end_date or date.today()
        return end - timedelta(days=self.days - 1), end


def set_current_actor(actor: ClerkActor) -> Token[ClerkActor | None]:
    if not isinstance(actor, ClerkActor):
        raise TypeError("a verified Clerk actor is required")
    return _CURRENT_ACTOR.set(actor)


def reset_current_actor(token: Token[ClerkActor | None]) -> None:
    _CURRENT_ACTOR.reset(token)


def local_actor_from_env() -> ClerkActor:
    issuer = os.environ.get("GARMIN_COACH_LOCAL_ACTOR_ISSUER", "").strip()
    subject = os.environ.get("GARMIN_COACH_LOCAL_ACTOR_SUBJECT", "").strip()
    if not issuer or not subject:
        raise RuntimeError(
            "GARMIN_COACH_LOCAL_ACTOR_ISSUER and "
            "GARMIN_COACH_LOCAL_ACTOR_SUBJECT are required"
        )
    return ClerkActor(issuer, subject)


def current_application_adapter() -> "ApplicationCoachAdapter":
    actor = _CURRENT_ACTOR.get() or local_actor_from_env()
    application = CoachApplication(DatabaseSettings.from_env())
    # The local MCP historically represented its configured owner immediately.
    # Remote/web actors are provisioned by Connect, not by read adapters.
    if _CURRENT_ACTOR.get() is None:
        application.execute(actor, EnsureProfile())
    return ApplicationCoachAdapter(application, actor)


class ApplicationCoachAdapter:
    """CoachData-shaped translation whose only authority is CoachApplication."""

    def __init__(self, application: CoachApplication, actor: ClerkActor) -> None:
        self._application = application
        self._actor = actor

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

    def read_context(self, window: ContextWindow) -> dict[str, Any]:
        starts_on, ends_on = window.bounds()
        context = self._application.read(
            self._actor, GetCoachingContext(starts_on, ends_on)
        )
        profile = self._application.read(self._actor, GetProfile())
        if context is None:
            observations = ()
            sessions = ()
            health = None
        else:
            observations = context.observations
            sessions = context.sessions
            health = context.collection_health

        by_date: dict[str, dict[str, Any]] = {}
        for observation in observations:
            if observation.local_date is None:
                continue
            iso = observation.local_date.isoformat()
            record = by_date.setdefault(iso, {"date": iso})
            field = _WELLNESS_FIELDS.get(observation.definition)
            if field is not None:
                record[field] = observation.value
        wellness = [by_date[key] for key in sorted(by_date)]
        missing_dates = []
        current = starts_on
        while current <= ends_on:
            if current.isoformat() not in by_date:
                missing_dates.append(current.isoformat())
            current += timedelta(days=1)
        history = [self._session(item) for item in sessions]
        latest_session = history[0].get("local_date") if history else None
        return {
            "window": {
                "start_date": starts_on.isoformat(),
                "end_date": ends_on.isoformat(),
                "days": window.days,
            },
            "athlete": (
                {"full_name": profile.display_name, "snapshot_date": None}
                if profile is not None
                else None
            ),
            "wellness": {"records": wellness, "missing_dates": missing_dates},
            "collection_health": _health_contract(health),
            "freshness": {
                "athlete_snapshot_date": None,
                "wellness_latest_date": wellness[-1]["date"] if wellness else None,
                "training_history_latest_date": latest_session,
            },
            "training_history": history,
        }

    def create_session(self, content: Mapping[str, Any]) -> dict[str, Any]:
        return self._session(
            self._application.execute(self._actor, CreateTrainingSession(content))
        )

    def get_session(self, session_id: str) -> dict[str, Any]:
        view = self._application.read(self._actor, GetTrainingSession(session_id))
        if view is None:
            raise NotFound("requested record is not available")
        return self._session(view)

    def list_sessions(
        self, window: ContextWindow, sport: str | None = None
    ) -> list[dict[str, Any]]:
        starts_on, ends_on = window.bounds()
        view = self._application.read(
            self._actor, ListTrainingSessions(starts_on, ends_on, sport)
        )
        return [self._session(item) for item in view.sessions]

    def replace_session(
        self, session_id: str, expected_revision: int, content: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._session(
            self._application.execute(
                self._actor,
                ReplaceTrainingSession(session_id, expected_revision, content),
            )
        )

    def delete_session(self, session_id: str, expected_revision: int) -> dict[str, Any]:
        result = self._application.execute(
            self._actor, DeleteTrainingSession(session_id, expected_revision)
        )
        return {"id": result.id, "deleted": result.deleted, "revision": result.revision}

    def create_goal_event(self, content: Mapping[str, Any]) -> dict[str, Any]:
        return self._goal(
            self._application.execute(self._actor, CreateGoalEvent(content))
        )

    def get_goal_event(self, goal_event_id: str) -> dict[str, Any]:
        view = self._application.read(self._actor, GetGoalEvent(goal_event_id))
        if view is None:
            raise NotFound("requested record is not available")
        return self._goal(view)

    def list_goal_events(
        self, *, sport: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        view = self._application.read(
            self._actor, ListGoalEvents(sport=sport, status=status)
        )
        return [self._goal(item) for item in view.events]

    def replace_goal_event(
        self, goal_event_id: str, expected_revision: int, content: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._goal(
            self._application.execute(
                self._actor,
                ReplaceGoalEvent(goal_event_id, expected_revision, content),
            )
        )

    def delete_goal_event(self, goal_event_id: str, expected_revision: int) -> dict[str, Any]:
        result = self._application.execute(
            self._actor, DeleteGoalEvent(goal_event_id, expected_revision)
        )
        return {"id": result.id, "deleted": result.deleted, "revision": result.revision}

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
            self._application.execute(
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

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        view = self._application.read(self._actor, GetTrainingPlan(plan_id))
        if view is None:
            raise NotFound("requested record is not available")
        return self._plan(view)

    def list_plans(self, status: str | None = None) -> list[dict[str, Any]]:
        view = self._application.read(self._actor, ListTrainingPlans(status=status))
        return [self._plan(item) for item in view.plans]

    def get_plan_history(self, plan_id: str) -> dict[str, Any]:
        view = self._application.read(self._actor, GetTrainingPlanHistory(plan_id))
        if view is None:
            raise NotFound("requested record is not available")
        return {"id": view.id, "revisions": [self._revision(item) for item in view.revisions]}

    def activate_plan(self, plan_id: str, expected_revision: int, reason: str) -> dict[str, Any]:
        return self._plan(
            self._application.execute(
                self._actor,
                ActivateTrainingPlan(plan_id, expected_revision, reason),
            )
        )

    def archive_plan(self, plan_id: str, expected_revision: int, reason: str) -> dict[str, Any]:
        return self._plan(
            self._application.execute(
                self._actor,
                ArchiveTrainingPlan(plan_id, expected_revision, reason),
            )
        )

    def delete_plan(self, plan_id: str, expected_revision: int) -> dict[str, Any]:
        result = self._application.execute(
            self._actor, DeleteTrainingPlan(plan_id, expected_revision)
        )
        return {"id": result.id, "deleted": result.deleted, "revision": result.revision}

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
            self._application.execute(
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
            self._application.execute(
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


_WELLNESS_FIELDS = {
    "daily_steps": "steps",
    "daily_distance": "distance_m",
    "daily_active_energy": "active_kcal",
    "daily_resting_heart_rate": "resting_hr",
    "daily_minimum_heart_rate": "min_hr",
    "daily_maximum_heart_rate": "max_hr",
    "daily_average_stress": "avg_stress",
    "daily_moderate_intensity_minutes": "intensity_min_moderate",
    "daily_vigorous_intensity_minutes": "intensity_min_vigorous",
    "daily_floors_ascended": "floors_ascended",
    "daily_sleep_duration": "sleep_seconds",
    "daily_sleep_score": "sleep_score",
    "nightly_hrv_average": "hrv_avg",
    "daily_hrv_status": "hrv_status",
    "daily_training_readiness": "training_readiness",
    "daily_training_readiness_level": "training_readiness_level",
    "daily_training_status": "training_status",
    "daily_vo2max_running": "vo2max_running",
    "daily_body_battery_charged": "body_battery_charged",
    "daily_body_battery_drained": "body_battery_drained",
}


def _health_contract(health: CollectionHealthView | None) -> dict[str, Any] | None:
    if health is None:
        return None
    domains: dict[str, dict[str, Any]] = {}
    for source in health.sources:
        for domain in source.domains:
            existing = domains.get(domain.domain)
            candidate = {
                "outcome": domain.outcome,
                "safe_code": domain.safe_code,
                "attempted_at": domain.attempted_at.isoformat() if domain.attempted_at else None,
                "finished_at": domain.finished_at.isoformat() if domain.finished_at else None,
                "last_success_at": (
                    domain.last_success_at.isoformat() if domain.last_success_at else None
                ),
            }
            if existing is None or (candidate["attempted_at"] or "") > (existing["attempted_at"] or ""):
                domains[domain.domain] = candidate
    attempted = [item for item in domains.values() if item["outcome"] != "not_attempted"]
    if not attempted:
        outcome = "not_attempted"
    elif all(item["outcome"] == "successful" for item in attempted):
        outcome = "successful"
    elif all(item["outcome"] == "failed" for item in attempted):
        outcome = "failed"
    else:
        outcome = "degraded"
    return {
        "schema_version": 1,
        "run": {
            "outcome": outcome,
            "attempted_at": max((item["attempted_at"] or "" for item in attempted), default=None),
            "finished_at": max((item["finished_at"] or "" for item in attempted), default=None),
            "last_success_at": max((item["last_success_at"] or "" for item in attempted), default=None),
        },
        "domains": domains,
    }
