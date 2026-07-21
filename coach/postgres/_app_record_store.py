"""Private transactional store for typed App Records.

The public module remains :class:`coach.application.CoachApplication`. This
store owns actor-to-Profile resolution, validation, lock order, optimistic CAS,
full Plan Revision snapshots, and the current-match projection in one
PostgreSQL transaction.

Training Session public identities are centralized here. Manual App Records use
``app:<uuidhex>`` for compatibility. Collected sessions use the provider-neutral
``session:<uuidhex>`` form; it exposes neither a Profile id nor provider key and
remains stable when a later adapter changes its source projection.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg

from coach.data import (
    InvalidRecord as _LegacyInvalidRecord,
    _normalize_matches,
    _optional_text,
    _plan_snapshot,
    _planned_session,
    _validate_constraints,
    _validate_goal_event_content,
    _validate_goal_refs,
    _validate_name,
    _validate_plan_date,
    _validate_planned_session_prescription,
    _validate_reason,
    _validate_session_content,
)

from ._capture_store import _ProfileNotFound
from .config import DatabaseSettings

_APP_RE = re.compile(r"^app:([0-9a-f]{32})$")
_SESSION_RE = re.compile(r"^session:([0-9a-f]{32})$")


class AppRecordStoreError(RuntimeError):
    """Invalid App Record request at the private store boundary."""


class _AppRecordNotFound(AppRecordStoreError):
    pass


class _AppRecordStale(AppRecordStoreError):
    pass


class _AppRecordConflict(AppRecordStoreError):
    pass


class _AppRecordReadOnly(AppRecordStoreError):
    pass


class AppRecordStore:
    """One transaction-owning persistence module for every App Record family."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- Manual Training Sessions -------------------------------------

    def create_training_session(
        self, issuer: str, subject: str, content: Mapping[str, Any]
    ) -> dict[str, Any]:
        normalized = _validated(_validate_session_content, content)
        session_id = uuid4()
        now = self._now()
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            self._insert_training_session(
                connection, profile_id, session_id, normalized, now
            )
            return self._training_session(
                connection, profile_id, session_id, expected_ownership="app"
            )

    def get_training_session(
        self, issuer: str, subject: str, public_id: str
    ) -> dict[str, Any] | None:
        try:
            session_id, ownership = _decode_training_session(public_id)
        except _AppRecordNotFound:
            return None
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            try:
                return self._training_session(
                    connection, profile_id, session_id, expected_ownership=ownership
                )
            except _AppRecordNotFound:
                return None

    def list_training_sessions(
        self,
        issuer: str,
        subject: str,
        starts_on: date,
        ends_on: date,
        sport: str | None,
    ) -> list[dict[str, Any]]:
        if not isinstance(starts_on, date) or isinstance(starts_on, datetime):
            raise AppRecordStoreError("starts_on must be a date")
        if not isinstance(ends_on, date) or isinstance(ends_on, datetime):
            raise AppRecordStoreError("ends_on must be a date")
        if starts_on > ends_on or (ends_on - starts_on).days >= 90:
            raise AppRecordStoreError("session window must contain 1 to 90 days")
        if sport is not None and (not isinstance(sport, str) or not sport.strip()):
            raise AppRecordStoreError("sport must be nonblank text or null")
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            rows = connection.execute(
                """
                SELECT id, ownership FROM training_sessions
                WHERE profile_id = %s AND local_date BETWEEN %s AND %s
                  AND (%s::text IS NULL OR sport = %s)
                ORDER BY COALESCE(local_start, local_date::timestamp) DESC, id
                """,
                (profile_id, starts_on, ends_on, sport, sport),
            ).fetchall()
            return [
                self._training_session(connection, profile_id, row[0], row[1])
                for row in rows
            ]

    def replace_training_session(
        self,
        issuer: str,
        subject: str,
        public_id: str,
        expected_revision: int,
        content: Mapping[str, Any],
    ) -> dict[str, Any]:
        session_id = _decode_app_mutation_id(public_id, "training session")
        expected_revision = _revision(expected_revision)
        normalized = _validated(_validate_session_content, content)
        fields = _session_columns(normalized)
        now = self._now()
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            row = connection.execute(
                """
                UPDATE training_sessions SET
                    local_date = %s, local_start = %s, timing_precision = %s,
                    time_zone = %s, utc_offset = %s, sport = %s,
                    session_type = %s, title = %s, session_rpe = %s,
                    duration_value = %s, duration_unit = %s, duration_basis = %s,
                    distance_value = %s, distance_unit = %s, notes = %s,
                    app_revision = app_revision + 1, updated_at = %s
                WHERE profile_id = %s AND id = %s AND ownership = 'app'
                  AND app_revision = %s
                RETURNING id
                """,
                (*fields, now, profile_id, session_id, expected_revision),
            ).fetchone()
            if row is None:
                self._raise_missing_or_stale(
                    connection,
                    "training_sessions",
                    profile_id,
                    session_id,
                    expected_revision,
                    revision_column="app_revision",
                    required_ownership="app",
                )
            connection.execute(
                "DELETE FROM session_loads WHERE profile_id = %s AND training_session_id = %s",
                (profile_id, session_id),
            )
            self._insert_loads(
                connection, profile_id, session_id, normalized.get("loads") or []
            )
            return self._training_session(
                connection, profile_id, session_id, expected_ownership="app"
            )

    def delete_training_session(
        self,
        issuer: str,
        subject: str,
        public_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        session_id = _decode_app_mutation_id(public_id, "training session")
        expected_revision = _revision(expected_revision)
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            row = connection.execute(
                """
                DELETE FROM training_sessions
                WHERE profile_id = %s AND id = %s AND ownership = 'app'
                  AND app_revision = %s
                RETURNING app_revision
                """,
                (profile_id, session_id, expected_revision),
            ).fetchone()
            if row is None:
                self._raise_missing_or_stale(
                    connection,
                    "training_sessions",
                    profile_id,
                    session_id,
                    expected_revision,
                    revision_column="app_revision",
                    required_ownership="app",
                )
            return {"id": _app_id(session_id), "deleted": True, "revision": row[0]}

    # -- Session Annotations ------------------------------------------

    def create_session_annotation(
        self,
        issuer: str,
        subject: str,
        training_session_id: str,
        *,
        notes: str | None,
        reliability: str | None,
        duplicate: bool | None,
    ) -> dict[str, Any]:
        target_id, ownership = _decode_training_session(training_session_id)
        if ownership != "collected":
            raise _AppRecordConflict("session annotations require a collected Training Session")
        values = _annotation_values(notes, reliability, duplicate)
        annotation_id = uuid4()
        now = self._now()
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            target = connection.execute(
                """
                SELECT 1 FROM training_sessions
                WHERE profile_id = %s AND id = %s AND ownership = 'collected'
                FOR KEY SHARE
                """,
                (profile_id, target_id),
            ).fetchone()
            if target is None:
                raise _AppRecordNotFound("requested record is not available")
            try:
                connection.execute(
                    """
                    INSERT INTO session_annotations (
                        profile_id, id, training_session_id, revision,
                        notes, reliability, duplicate_flag, created_at, updated_at
                    ) VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s)
                    """,
                    (profile_id, annotation_id, target_id, *values, now, now),
                )
            except psycopg.errors.UniqueViolation:
                raise _AppRecordConflict(
                    "the collected Training Session already has an annotation"
                ) from None
            return self._session_annotation(connection, profile_id, annotation_id)

    def get_session_annotation(
        self, issuer: str, subject: str, annotation_id: str
    ) -> dict[str, Any] | None:
        try:
            identifier = _decode_app_id(annotation_id)
        except _AppRecordNotFound:
            return None
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            try:
                return self._session_annotation(connection, profile_id, identifier)
            except _AppRecordNotFound:
                return None

    def replace_session_annotation(
        self,
        issuer: str,
        subject: str,
        annotation_id: str,
        expected_revision: int,
        *,
        notes: str | None,
        reliability: str | None,
        duplicate: bool | None,
    ) -> dict[str, Any]:
        identifier = _decode_app_mutation_id(annotation_id, "session annotation")
        expected_revision = _revision(expected_revision)
        values = _annotation_values(notes, reliability, duplicate)
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            row = connection.execute(
                """
                UPDATE session_annotations
                SET notes = %s, reliability = %s, duplicate_flag = %s,
                    revision = revision + 1, updated_at = %s
                WHERE profile_id = %s AND id = %s AND revision = %s
                RETURNING id
                """,
                (*values, self._now(), profile_id, identifier, expected_revision),
            ).fetchone()
            if row is None:
                self._raise_missing_or_stale(
                    connection,
                    "session_annotations",
                    profile_id,
                    identifier,
                    expected_revision,
                )
            return self._session_annotation(connection, profile_id, identifier)

    def delete_session_annotation(
        self,
        issuer: str,
        subject: str,
        annotation_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        identifier = _decode_app_mutation_id(annotation_id, "session annotation")
        expected_revision = _revision(expected_revision)
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            row = connection.execute(
                """
                DELETE FROM session_annotations
                WHERE profile_id = %s AND id = %s AND revision = %s
                RETURNING revision
                """,
                (profile_id, identifier, expected_revision),
            ).fetchone()
            if row is None:
                self._raise_missing_or_stale(
                    connection,
                    "session_annotations",
                    profile_id,
                    identifier,
                    expected_revision,
                )
            return {"id": _app_id(identifier), "deleted": True, "revision": row[0]}

    # -- Goal Events ---------------------------------------------------

    def create_goal_event(
        self, issuer: str, subject: str, content: Mapping[str, Any]
    ) -> dict[str, Any]:
        normalized = _validated(_validate_goal_event_content, content)
        identifier = uuid4()
        now = self._now()
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            connection.execute(
                """
                INSERT INTO goal_events (
                    profile_id, id, revision, local_date, local_start,
                    timing_precision, time_zone, utc_offset, sport, name,
                    priority, status, distance_value, distance_unit,
                    goal_target_value, goal_target_unit, goal_statement,
                    outcome_actual_value, outcome_actual_unit, outcome_statement,
                    notes, created_at, updated_at
                ) VALUES (
                    %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (profile_id, identifier, *_goal_columns(normalized), now, now),
            )
            return self._goal_event(connection, profile_id, identifier)

    def get_goal_event(
        self, issuer: str, subject: str, goal_event_id: str
    ) -> dict[str, Any] | None:
        try:
            identifier = _decode_app_id(goal_event_id)
        except _AppRecordNotFound:
            return None
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            try:
                return self._goal_event(connection, profile_id, identifier)
            except _AppRecordNotFound:
                return None

    def list_goal_events(
        self,
        issuer: str,
        subject: str,
        sport: str | None,
        status: str | None,
    ) -> list[dict[str, Any]]:
        if sport is not None and (not isinstance(sport, str) or not sport.strip()):
            raise AppRecordStoreError("sport must be nonblank text or null")
        if status is not None and (
            not isinstance(status, str)
            or status not in {"scheduled", "completed", "cancelled"}
        ):
            raise AppRecordStoreError("status is unsupported")
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            rows = connection.execute(
                """
                SELECT id FROM goal_events
                WHERE profile_id = %s
                  AND (%s::text IS NULL OR sport = %s)
                  AND (%s::text IS NULL OR status = %s)
                ORDER BY COALESCE(local_start, local_date::timestamp), id
                """,
                (profile_id, sport, sport, status, status),
            ).fetchall()
            return [self._goal_event(connection, profile_id, row[0]) for row in rows]

    def replace_goal_event(
        self,
        issuer: str,
        subject: str,
        goal_event_id: str,
        expected_revision: int,
        content: Mapping[str, Any],
    ) -> dict[str, Any]:
        identifier = _decode_app_mutation_id(goal_event_id, "goal event")
        expected_revision = _revision(expected_revision)
        normalized = _validated(_validate_goal_event_content, content)
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            row = connection.execute(
                """
                UPDATE goal_events SET
                    local_date = %s, local_start = %s, timing_precision = %s,
                    time_zone = %s, utc_offset = %s, sport = %s, name = %s,
                    priority = %s, status = %s, distance_value = %s,
                    distance_unit = %s, goal_target_value = %s,
                    goal_target_unit = %s, goal_statement = %s,
                    outcome_actual_value = %s, outcome_actual_unit = %s,
                    outcome_statement = %s, notes = %s,
                    revision = revision + 1, updated_at = %s
                WHERE profile_id = %s AND id = %s AND revision = %s
                RETURNING id
                """,
                (
                    *_goal_columns(normalized),
                    self._now(),
                    profile_id,
                    identifier,
                    expected_revision,
                ),
            ).fetchone()
            if row is None:
                self._raise_missing_or_stale(
                    connection, "goal_events", profile_id, identifier, expected_revision
                )
            return self._goal_event(connection, profile_id, identifier)

    def delete_goal_event(
        self,
        issuer: str,
        subject: str,
        goal_event_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        identifier = _decode_app_mutation_id(goal_event_id, "goal event")
        expected_revision = _revision(expected_revision)
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            row = connection.execute(
                """
                DELETE FROM goal_events
                WHERE profile_id = %s AND id = %s AND revision = %s
                RETURNING revision
                """,
                (profile_id, identifier, expected_revision),
            ).fetchone()
            if row is None:
                self._raise_missing_or_stale(
                    connection, "goal_events", profile_id, identifier, expected_revision
                )
            return {"id": _app_id(identifier), "deleted": True, "revision": row[0]}

    # -- Training Plans ------------------------------------------------

    def create_training_plan(
        self,
        issuer: str,
        subject: str,
        name: str,
        starts_on: str,
        ends_on: str,
        reason: str,
        *,
        goal_events: Sequence[Mapping[str, Any]] | None,
        constraints: Sequence[str] | None,
        planned_sessions: Sequence[Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        plan_name = _validated(_validate_name, name)
        starts = _validated(_validate_plan_date, starts_on, "starts_on")
        ends = _validated(_validate_plan_date, ends_on, "ends_on")
        if starts > ends:
            raise AppRecordStoreError("starts_on must be on or before ends_on")
        normalized_reason = _validated(_validate_reason, reason)
        refs = _validated(_validate_goal_refs, _list_or_none(goal_events))
        constraint_values = _validated(_validate_constraints, _list_or_none(constraints))
        if planned_sessions is not None and not isinstance(planned_sessions, Sequence):
            raise AppRecordStoreError("planned_sessions must be a sequence")
        planned: list[dict[str, Any]] = []
        for index, item in enumerate(planned_sessions or ()):
            prescription = _validated(
                _validate_planned_session_prescription,
                dict(item) if isinstance(item, Mapping) else item,
                starts,
                ends,
                label=f"planned_sessions[{index}]",
            )
            planned.append(
                _planned_session(uuid4().hex, prescription, "scheduled", [], None)
            )
        snapshot = _plan_snapshot(
            plan_name, starts, ends, "draft", refs, constraint_values, planned
        )
        plan_id = uuid4()
        now = self._now()
        today = now.date().isoformat()
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            self._lock_current_goal_refs(connection, profile_id, refs)
            connection.execute(
                """
                INSERT INTO training_plans (
                    profile_id, id, current_revision, status, created_at, updated_at
                ) VALUES (%s, %s, 1, 'draft', %s, %s)
                """,
                (profile_id, plan_id, now, now),
            )
            self._insert_plan_revision(
                connection,
                profile_id,
                plan_id,
                1,
                "create",
                normalized_reason,
                None,
                snapshot,
                now,
                today,
            )
            connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
            return self._plan(connection, profile_id, plan_id)

    def get_training_plan(
        self, issuer: str, subject: str, plan_id: str
    ) -> dict[str, Any] | None:
        try:
            identifier = _decode_app_id(plan_id)
        except _AppRecordNotFound:
            return None
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            try:
                return self._plan(connection, profile_id, identifier)
            except _AppRecordNotFound:
                return None

    def list_training_plans(
        self, issuer: str, subject: str, status: str | None
    ) -> list[dict[str, Any]]:
        if status is not None and (
            not isinstance(status, str)
            or status not in {"draft", "active", "archived"}
        ):
            raise AppRecordStoreError("status is unsupported")
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            rows = connection.execute(
                """
                SELECT p.id FROM training_plans p
                JOIN plan_revisions r
                  ON r.profile_id = p.profile_id AND r.plan_id = p.id
                 AND r.revision = p.current_revision
                WHERE p.profile_id = %s AND (%s::text IS NULL OR p.status = %s)
                ORDER BY r.starts_on, p.id
                """,
                (profile_id, status, status),
            ).fetchall()
            return [self._plan(connection, profile_id, row[0]) for row in rows]

    def get_training_plan_history(
        self, issuer: str, subject: str, plan_id: str
    ) -> dict[str, Any] | None:
        try:
            identifier = _decode_app_id(plan_id)
        except _AppRecordNotFound:
            return None
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            exists = connection.execute(
                "SELECT 1 FROM training_plans WHERE profile_id = %s AND id = %s",
                (profile_id, identifier),
            ).fetchone()
            if exists is None:
                return None
            revisions = connection.execute(
                """
                SELECT revision, kind, recorded_at, reason, effective_from
                FROM plan_revisions
                WHERE profile_id = %s AND plan_id = %s ORDER BY revision
                """,
                (profile_id, identifier),
            ).fetchall()
            return {
                "id": _app_id(identifier),
                "revisions": [
                    {
                        "revision": row[0],
                        "kind": row[1],
                        "recorded_at": row[2],
                        "reason": row[3],
                        "effective_from": row[4].isoformat() if row[4] else None,
                        "plan": self._snapshot(
                            connection, profile_id, identifier, row[0]
                        ),
                    }
                    for row in revisions
                ],
            }

    def activate_training_plan(
        self,
        issuer: str,
        subject: str,
        plan_id: str,
        expected_revision: int,
        reason: str,
    ) -> dict[str, Any]:
        identifier = _decode_app_mutation_id(plan_id, "training plan")
        expected_revision = _revision(expected_revision)
        reason = _validated(_validate_reason, reason)
        now = self._now()
        with psycopg.connect(self._settings.url) as connection:
            profile_id, head = self._locked_plan(
                connection, issuer, subject, identifier, expected_revision
            )
            if head["status"] != "draft":
                raise AppRecordStoreError("only a draft plan can be activated")
            self._lock_current_goal_refs(connection, profile_id, head["goal_events"])
            today = now.date().isoformat()
            if any(
                item["disposition"] == "scheduled" and item["scheduled_date"] < today
                for item in head["planned_sessions"]
            ):
                raise AppRecordStoreError(
                    "activation requires resolving scheduled prescriptions dated before the activation date"
                )
            snapshot = _copy_snapshot(head)
            snapshot["status"] = "active"
            self._append_plan_revision(
                connection,
                profile_id,
                identifier,
                expected_revision,
                "lifecycle",
                reason,
                None,
                snapshot,
                now,
            )
            return self._plan(connection, profile_id, identifier)

    def archive_training_plan(
        self,
        issuer: str,
        subject: str,
        plan_id: str,
        expected_revision: int,
        reason: str,
    ) -> dict[str, Any]:
        identifier = _decode_app_mutation_id(plan_id, "training plan")
        expected_revision = _revision(expected_revision)
        reason = _validated(_validate_reason, reason)
        now = self._now()
        with psycopg.connect(self._settings.url) as connection:
            profile_id, head = self._locked_plan(
                connection, issuer, subject, identifier, expected_revision
            )
            if head["status"] == "archived":
                raise AppRecordStoreError("plan is already archived")
            snapshot = _copy_snapshot(head)
            snapshot["status"] = "archived"
            self._append_plan_revision(
                connection,
                profile_id,
                identifier,
                expected_revision,
                "lifecycle",
                reason,
                None,
                snapshot,
                now,
            )
            return self._plan(connection, profile_id, identifier)

    def delete_training_plan(
        self,
        issuer: str,
        subject: str,
        plan_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        identifier = _decode_app_mutation_id(plan_id, "training plan")
        expected_revision = _revision(expected_revision)
        with psycopg.connect(self._settings.url) as connection:
            profile_id, _ = self._locked_plan(
                connection, issuer, subject, identifier, expected_revision
            )
            row = connection.execute(
                """
                DELETE FROM training_plans
                WHERE profile_id = %s AND id = %s AND current_revision = %s
                RETURNING current_revision
                """,
                (profile_id, identifier, expected_revision),
            ).fetchone()
            if row is None:
                raise _AppRecordStale("revision does not match current state")
            return {"id": _app_id(identifier), "deleted": True, "revision": row[0]}

    def adjust_training_plan(
        self,
        issuer: str,
        subject: str,
        plan_id: str,
        expected_revision: int,
        reason: str,
        effective_from: str,
        operations: Sequence[Mapping[str, Any]],
        *,
        name: str | None,
        starts_on: str | None,
        ends_on: str | None,
        goal_events: Sequence[Mapping[str, Any]] | None,
        constraints: Sequence[str] | None,
    ) -> dict[str, Any]:
        identifier = _decode_app_mutation_id(plan_id, "training plan")
        expected_revision = _revision(expected_revision)
        reason = _validated(_validate_reason, reason)
        if not isinstance(operations, Sequence) or isinstance(operations, (str, bytes)):
            raise AppRecordStoreError("operations must be a sequence")
        now = self._now()
        with psycopg.connect(self._settings.url) as connection:
            profile_id, head = self._locked_plan(
                connection, issuer, subject, identifier, expected_revision
            )
            if head["status"] == "archived":
                raise AppRecordStoreError("archived plans accept only fulfilment corrections")
            new_name = head["name"] if name is None else _validated(_validate_name, name)
            new_starts = (
                head["starts_on"]
                if starts_on is None
                else _validated(_validate_plan_date, starts_on, "starts_on")
            )
            new_ends = (
                head["ends_on"]
                if ends_on is None
                else _validated(_validate_plan_date, ends_on, "ends_on")
            )
            if new_starts > new_ends:
                raise AppRecordStoreError("starts_on must be on or before ends_on")
            effective = _validated(
                _validate_plan_date, effective_from, "effective_from"
            )
            today = now.date().isoformat()
            if effective < today:
                raise AppRecordStoreError("effective_from must not be earlier than today")
            prior = connection.execute(
                """
                SELECT max(effective_from) FROM plan_revisions
                WHERE profile_id = %s AND plan_id = %s
                """,
                (profile_id, identifier),
            ).fetchone()[0]
            if prior is not None and effective < prior.isoformat():
                raise AppRecordStoreError(
                    "effective_from must not move earlier than a prior adjustment"
                )
            if not new_starts <= effective <= new_ends:
                raise AppRecordStoreError("effective_from must fall within the plan range")

            sessions = {item["id"]: dict(item) for item in head["planned_sessions"]}
            order = [item["id"] for item in head["planned_sessions"]]
            for index, operation in enumerate(operations):
                self._apply_adjustment(
                    operation,
                    index,
                    sessions,
                    order,
                    effective,
                    new_starts,
                    new_ends,
                )
            planned = [sessions[item] for item in order]
            if any(
                not new_starts <= item["scheduled_date"] <= new_ends
                for item in planned
            ):
                raise AppRecordStoreError(
                    "a retained planned session would fall outside the plan range"
                )
            old_before = [
                item for item in head["planned_sessions"]
                if item["scheduled_date"] < effective
            ]
            new_before = [
                item for item in planned if item["scheduled_date"] < effective
            ]
            if old_before != new_before:
                raise AppRecordStoreError("prescriptions before effective_from are frozen")
            refs = (
                [dict(item) for item in head["goal_events"]]
                if goal_events is None
                else _validated(_validate_goal_refs, _list_or_none(goal_events))
            )
            self._lock_current_goal_refs(connection, profile_id, refs)
            constraint_values = (
                list(head["constraints"])
                if constraints is None
                else _validated(_validate_constraints, _list_or_none(constraints))
            )
            snapshot = _plan_snapshot(
                new_name,
                new_starts,
                new_ends,
                head["status"],
                refs,
                constraint_values,
                planned,
            )
            self._append_plan_revision(
                connection,
                profile_id,
                identifier,
                expected_revision,
                "adjustment",
                reason,
                effective,
                snapshot,
                now,
            )
            return self._plan(connection, profile_id, identifier)

    def set_planned_session_fulfilment(
        self,
        issuer: str,
        subject: str,
        plan_id: str,
        expected_revision: int,
        planned_session_id: str,
        disposition: str,
        reason: str,
        matches: Sequence[str] | None,
        fulfilment_note: str | None,
    ) -> dict[str, Any]:
        identifier = _decode_app_mutation_id(plan_id, "training plan")
        expected_revision = _revision(expected_revision)
        reason = _validated(_validate_reason, reason)
        if not isinstance(disposition, str) or disposition not in {
            "scheduled",
            "fulfilled",
            "skipped",
        }:
            raise AppRecordStoreError(
                "disposition must be scheduled, fulfilled, or skipped"
            )
        normalized_matches = _validated(
            _normalize_matches,
            list(matches) if matches is not None else None,
            disposition,
        )
        note = _validated(_optional_text, fulfilment_note, "fulfilment_note")
        try:
            planned_uuid = UUID(hex=planned_session_id)
        except (TypeError, ValueError):
            raise _AppRecordNotFound("requested record is not available") from None
        decoded_matches = [_decode_training_session(item) for item in normalized_matches]
        match_ids = [item[0] for item in decoded_matches]
        if len(set(match_ids)) != len(match_ids):
            raise AppRecordStoreError("matches contains duplicate training session ids")
        now = self._now()
        with psycopg.connect(self._settings.url) as connection:
            profile_id = self._profile(connection, issuer, subject)
            self._assert_available_matches(connection, profile_id, decoded_matches)
            profile_id, head = self._locked_plan(
                connection, issuer, subject, identifier, expected_revision
            )
            target = next(
                (item for item in head["planned_sessions"] if item["id"] == planned_uuid.hex),
                None,
            )
            if target is None:
                raise _AppRecordNotFound("requested record is not available")
            if target["disposition"] == "cancelled":
                raise AppRecordStoreError(
                    "a cancelled planned session must be reinstated by adjustment"
                )
            updated: list[dict[str, Any]] = []
            for item in head["planned_sessions"]:
                copy = dict(item)
                if item["id"] == planned_uuid.hex:
                    copy["disposition"] = disposition
                    copy["matches"] = [
                        _training_session_id(match_id, ownership)
                        for match_id, ownership in decoded_matches
                    ]
                    copy["fulfilment_note"] = note
                updated.append(copy)
            snapshot = _plan_snapshot(
                head["name"],
                head["starts_on"],
                head["ends_on"],
                head["status"],
                head["goal_events"],
                head["constraints"],
                updated,
            )
            self._append_plan_revision(
                connection,
                profile_id,
                identifier,
                expected_revision,
                "fulfilment_correction",
                reason,
                None,
                snapshot,
                now,
            )
            return self._plan(connection, profile_id, identifier)

    # -- Persistence internals ----------------------------------------

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise AppRecordStoreError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _profile(
        connection: psycopg.Connection, issuer: str, subject: str
    ) -> UUID:
        if not isinstance(issuer, str) or not issuer.strip():
            raise AppRecordStoreError("actor issuer is required")
        if not isinstance(subject, str) or not subject.strip():
            raise AppRecordStoreError("actor subject is required")
        row = connection.execute(
            """
            SELECT id FROM profiles
            WHERE clerk_issuer = %s AND clerk_subject = %s
            """,
            (issuer.strip(), subject.strip()),
        ).fetchone()
        if row is None:
            raise _ProfileNotFound("profile is not available")
        return row[0]

    @staticmethod
    def _raise_missing_or_stale(
        connection: psycopg.Connection,
        table: str,
        profile_id: UUID,
        identifier: UUID,
        expected_revision: int,
        *,
        revision_column: str = "revision",
        required_ownership: str | None = None,
    ) -> None:
        # Table and column names are private constants selected by callers.
        ownership_filter = " AND ownership = %s" if required_ownership else ""
        parameters: tuple[Any, ...] = (profile_id, identifier)
        if required_ownership:
            parameters += (required_ownership,)
        row = connection.execute(
            f"SELECT {revision_column} FROM {table} "
            f"WHERE profile_id = %s AND id = %s{ownership_filter}",
            parameters,
        ).fetchone()
        if row is None:
            raise _AppRecordNotFound("requested record is not available")
        if row[0] != expected_revision:
            raise _AppRecordStale("revision does not match current state")
        raise _AppRecordConflict("operation conflicts with current state")

    def _insert_training_session(
        self,
        connection: psycopg.Connection,
        profile_id: UUID,
        session_id: UUID,
        normalized: Mapping[str, Any],
        now: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO training_sessions (
                profile_id, id, ownership, app_revision,
                local_date, local_start, timing_precision, time_zone, utc_offset,
                sport, session_type, title, session_rpe, duration_value,
                duration_unit, duration_basis, distance_value, distance_unit,
                notes, created_at, updated_at
            ) VALUES (
                %s, %s, 'app', 1, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (profile_id, session_id, *_session_columns(normalized), now, now),
        )
        self._insert_loads(
            connection, profile_id, session_id, normalized.get("loads") or []
        )

    @staticmethod
    def _insert_loads(
        connection: psycopg.Connection,
        profile_id: UUID,
        session_id: UUID,
        loads: Sequence[Mapping[str, Any]],
    ) -> None:
        for load in loads:
            connection.execute(
                """
                INSERT INTO session_loads (
                    profile_id, id, training_session_id, ownership,
                    method, unit, value, source
                ) VALUES (%s, %s, %s, 'app', %s, %s, %s, %s)
                """,
                (
                    profile_id,
                    uuid4(),
                    session_id,
                    load["method"],
                    load["unit"],
                    load["value"],
                    load["source"],
                ),
            )

    @staticmethod
    def _training_session(
        connection: psycopg.Connection,
        profile_id: UUID,
        session_id: UUID,
        expected_ownership: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT id, ownership, app_revision, local_date, local_start,
                   timing_precision, time_zone, utc_offset, sport, session_type,
                   title, session_rpe, duration_value, duration_unit,
                   duration_basis, distance_value, distance_unit, notes,
                   created_at, updated_at
            FROM training_sessions
            WHERE profile_id = %s AND id = %s AND ownership = %s
            """,
            (profile_id, session_id, expected_ownership),
        ).fetchone()
        if row is None:
            raise _AppRecordNotFound("requested record is not available")
        loads = connection.execute(
            """
            SELECT value, unit, method, source FROM session_loads
            WHERE profile_id = %s AND training_session_id = %s
            ORDER BY method, unit, source
            """,
            (profile_id, session_id),
        ).fetchall()
        duration = (
            {
                "value": _number(row[12]),
                "unit": row[13],
                "basis": row[14],
            }
            if row[12] is not None
            else None
        )
        distance = (
            {"value": _number(row[15]), "unit": row[16]}
            if row[15] is not None
            else None
        )
        return {
            "id": _training_session_id(row[0], row[1]),
            "origin": "app_record" if row[1] == "app" else "collected_record",
            "ownership": row[1],
            "local_date": row[3].isoformat(),
            "local_start": _local_start(row[4]),
            "timing_precision": row[5],
            "time_zone": row[6],
            "utc_offset": row[7],
            "sport": row[8],
            "session_type": row[9],
            "title": row[10],
            "session_rpe": row[11],
            "duration": duration,
            "distance": distance,
            "notes": row[17],
            "loads": (
                [
                    {
                        "value": _number(item[0]),
                        "unit": item[1],
                        "method": item[2],
                        "source": item[3],
                    }
                    for item in loads
                ]
                or None
            ),
            "provenance": {"source": "manual"} if row[1] == "app" else {"source": "collected"},
            "created_at": row[18],
            "updated_at": row[19],
            "revision": row[2],
            "source_detail": None,
        }

    @staticmethod
    def _session_annotation(
        connection: psycopg.Connection, profile_id: UUID, annotation_id: UUID
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT id, training_session_id, revision, notes, reliability,
                   duplicate_flag, created_at, updated_at
            FROM session_annotations WHERE profile_id = %s AND id = %s
            """,
            (profile_id, annotation_id),
        ).fetchone()
        if row is None:
            raise _AppRecordNotFound("requested record is not available")
        return {
            "id": _app_id(row[0]),
            "origin": "app_record",
            "training_session_id": _training_session_id(row[1], "collected"),
            "notes": row[3],
            "reliability": row[4],
            "duplicate": row[5],
            "provenance": {"source": "manual"},
            "created_at": row[6],
            "updated_at": row[7],
            "revision": row[2],
        }

    @staticmethod
    def _goal_event(
        connection: psycopg.Connection, profile_id: UUID, identifier: UUID
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT id, revision, local_date, local_start, timing_precision,
                   time_zone, utc_offset, sport, name, priority, status,
                   distance_value, distance_unit, goal_target_value,
                   goal_target_unit, goal_statement, outcome_actual_value,
                   outcome_actual_unit, outcome_statement, notes,
                   created_at, updated_at
            FROM goal_events WHERE profile_id = %s AND id = %s
            """,
            (profile_id, identifier),
        ).fetchone()
        if row is None:
            raise _AppRecordNotFound("requested record is not available")
        distance = (
            {"value": _number(row[11]), "unit": row[12]}
            if row[11] is not None
            else None
        )
        goal = (
            {
                "target_duration": (
                    {"value": _number(row[13]), "unit": row[14]}
                    if row[13] is not None
                    else None
                ),
                "statement": row[15],
            }
            if row[13] is not None or row[15] is not None
            else None
        )
        outcome = (
            {
                "actual_duration": (
                    {"value": _number(row[16]), "unit": row[17]}
                    if row[16] is not None
                    else None
                ),
                "statement": row[18],
            }
            if row[16] is not None or row[18] is not None
            else None
        )
        return {
            "id": _app_id(row[0]),
            "origin": "app_record",
            "local_date": row[2].isoformat(),
            "local_start": _local_start(row[3]),
            "timing_precision": row[4],
            "time_zone": row[5],
            "utc_offset": row[6],
            "sport": row[7],
            "name": row[8],
            "priority": row[9],
            "status": row[10],
            "distance": distance,
            "goal": goal,
            "outcome": outcome,
            "notes": row[19],
            "provenance": {"source": "manual"},
            "created_at": row[20],
            "updated_at": row[21],
            "revision": row[1],
        }

    def _locked_plan(
        self,
        connection: psycopg.Connection,
        issuer: str,
        subject: str,
        plan_id: UUID,
        expected_revision: int,
    ) -> tuple[UUID, dict[str, Any]]:
        profile_id = self._profile(connection, issuer, subject)
        row = connection.execute(
            """
            SELECT current_revision FROM training_plans
            WHERE profile_id = %s AND id = %s FOR UPDATE
            """,
            (profile_id, plan_id),
        ).fetchone()
        if row is None:
            raise _AppRecordNotFound("requested record is not available")
        if row[0] != expected_revision:
            raise _AppRecordStale("revision does not match current state")
        return profile_id, self._snapshot(connection, profile_id, plan_id, row[0])

    def _plan(
        self, connection: psycopg.Connection, profile_id: UUID, plan_id: UUID
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT current_revision, created_at, updated_at
            FROM training_plans WHERE profile_id = %s AND id = %s
            """,
            (profile_id, plan_id),
        ).fetchone()
        if row is None:
            raise _AppRecordNotFound("requested record is not available")
        snapshot = self._snapshot(connection, profile_id, plan_id, row[0])
        stale = self._stale_goal_refs(connection, profile_id, snapshot["goal_events"])
        return {
            "id": _app_id(plan_id),
            "origin": "app_record",
            "name": snapshot["name"],
            "starts_on": snapshot["starts_on"],
            "ends_on": snapshot["ends_on"],
            "status": snapshot["status"],
            "goal_events": stale,
            "constraints": snapshot["constraints"],
            "planned_sessions": sorted(
                snapshot["planned_sessions"],
                key=lambda item: (item["scheduled_date"], item["id"]),
            ),
            "requires_review": any(item["stale"] for item in stale),
            "provenance": {"source": "manual"},
            "created_at": row[1],
            "updated_at": row[2],
            "revision": row[0],
        }

    @staticmethod
    def _snapshot(
        connection: psycopg.Connection,
        profile_id: UUID,
        plan_id: UUID,
        revision: int,
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT name, starts_on, ends_on, status FROM plan_revisions
            WHERE profile_id = %s AND plan_id = %s AND revision = %s
            """,
            (profile_id, plan_id, revision),
        ).fetchone()
        if row is None:
            raise _AppRecordConflict("training plan head is unavailable")
        goal_rows = connection.execute(
            """
            SELECT goal_event_id, goal_event_revision
            FROM plan_revision_goal_events
            WHERE profile_id = %s AND plan_id = %s AND revision = %s
            ORDER BY position
            """,
            (profile_id, plan_id, revision),
        ).fetchall()
        constraints = connection.execute(
            """
            SELECT statement FROM plan_revision_constraints
            WHERE profile_id = %s AND plan_id = %s AND revision = %s
            ORDER BY position
            """,
            (profile_id, plan_id, revision),
        ).fetchall()
        planned_rows = connection.execute(
            """
            SELECT planned_session_id, scheduled_date, sport, session_type,
                   prescription, target_duration_seconds,
                   target_distance_meters, effort_guidance, disposition,
                   fulfilment_note
            FROM plan_revision_planned_sessions
            WHERE profile_id = %s AND plan_id = %s AND revision = %s
            ORDER BY position
            """,
            (profile_id, plan_id, revision),
        ).fetchall()
        planned = []
        for item in planned_rows:
            matches = connection.execute(
                """
                SELECT m.training_session_id, s.ownership
                FROM plan_revision_matches m
                JOIN training_sessions s
                  ON s.profile_id = m.profile_id AND s.id = m.training_session_id
                WHERE m.profile_id = %s AND m.plan_id = %s
                  AND m.revision = %s AND m.planned_session_id = %s
                ORDER BY m.position
                """,
                (profile_id, plan_id, revision, item[0]),
            ).fetchall()
            planned.append(
                {
                    "id": item[0].hex,
                    "scheduled_date": item[1].isoformat(),
                    "sport": item[2],
                    "session_type": item[3],
                    "prescription": item[4],
                    "target_duration_seconds": _number(item[5]),
                    "target_distance_meters": _number(item[6]),
                    "effort_guidance": item[7],
                    "disposition": item[8],
                    "matches": [
                        _training_session_id(match[0], match[1]) for match in matches
                    ],
                    "fulfilment_note": item[9],
                }
            )
        return _plan_snapshot(
            row[0],
            row[1].isoformat(),
            row[2].isoformat(),
            row[3],
            [
                {
                    "goal_event_id": _app_id(goal[0]),
                    "goal_event_revision": goal[1],
                }
                for goal in goal_rows
            ],
            [item[0] for item in constraints],
            planned,
        )

    def _insert_plan_revision(
        self,
        connection: psycopg.Connection,
        profile_id: UUID,
        plan_id: UUID,
        revision: int,
        kind: str,
        reason: str,
        effective_from: str | None,
        snapshot: Mapping[str, Any],
        recorded_at: datetime,
        recorded_date: str,
    ) -> None:
        self._lock_current_match_sessions(
            connection, profile_id, plan_id, snapshot["planned_sessions"]
        )
        connection.execute(
            """
            INSERT INTO plan_revisions (
                profile_id, plan_id, revision, kind, recorded_at,
                recorded_date, reason, effective_from, name, starts_on,
                ends_on, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                profile_id,
                plan_id,
                revision,
                kind,
                recorded_at,
                recorded_date,
                reason,
                effective_from,
                snapshot["name"],
                snapshot["starts_on"],
                snapshot["ends_on"],
                snapshot["status"],
            ),
        )
        for position, ref in enumerate(snapshot["goal_events"]):
            connection.execute(
                """
                INSERT INTO plan_revision_goal_events (
                    profile_id, plan_id, revision, position,
                    goal_event_id, goal_event_revision
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    profile_id,
                    plan_id,
                    revision,
                    position,
                    _decode_app_id(ref["goal_event_id"]),
                    ref["goal_event_revision"],
                ),
            )
        for position, statement in enumerate(snapshot["constraints"]):
            connection.execute(
                """
                INSERT INTO plan_revision_constraints (
                    profile_id, plan_id, revision, position, statement
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (profile_id, plan_id, revision, position, statement),
            )
        for position, item in enumerate(snapshot["planned_sessions"]):
            planned_id = UUID(hex=item["id"])
            connection.execute(
                """
                INSERT INTO planned_sessions (
                    profile_id, plan_id, id, created_revision
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (profile_id, plan_id, id) DO NOTHING
                """,
                (profile_id, plan_id, planned_id, revision),
            )
            connection.execute(
                """
                INSERT INTO plan_revision_planned_sessions (
                    profile_id, plan_id, revision, planned_session_id,
                    position, scheduled_date, sport, session_type, prescription,
                    target_duration_seconds, target_distance_meters,
                    effort_guidance, disposition, fulfilment_note
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                """,
                (
                    profile_id,
                    plan_id,
                    revision,
                    planned_id,
                    position,
                    item["scheduled_date"],
                    item["sport"],
                    item["session_type"],
                    item["prescription"],
                    item["target_duration_seconds"],
                    item["target_distance_meters"],
                    item["effort_guidance"],
                    item["disposition"],
                    item["fulfilment_note"],
                ),
            )
            for match_position, public_id in enumerate(item["matches"]):
                match_id, _ = _decode_training_session(public_id)
                connection.execute(
                    """
                    INSERT INTO plan_revision_matches (
                        profile_id, plan_id, revision, planned_session_id,
                        position, training_session_id
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        profile_id,
                        plan_id,
                        revision,
                        planned_id,
                        match_position,
                        match_id,
                    ),
                )

        connection.execute(
            """
            DELETE FROM planned_session_current_matches
            WHERE profile_id = %s AND plan_id = %s
            """,
            (profile_id, plan_id),
        )
        for item in snapshot["planned_sessions"]:
            planned_id = UUID(hex=item["id"])
            for position, public_id in enumerate(item["matches"]):
                match_id, _ = _decode_training_session(public_id)
                connection.execute(
                    """
                    INSERT INTO planned_session_current_matches (
                        profile_id, plan_id, planned_session_id,
                        training_session_id, position
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    (profile_id, plan_id, planned_id, match_id, position),
                )

    def _append_plan_revision(
        self,
        connection: psycopg.Connection,
        profile_id: UUID,
        plan_id: UUID,
        expected_revision: int,
        kind: str,
        reason: str,
        effective_from: str | None,
        snapshot: Mapping[str, Any],
        now: datetime,
    ) -> None:
        revision = expected_revision + 1
        self._insert_plan_revision(
            connection,
            profile_id,
            plan_id,
            revision,
            kind,
            reason,
            effective_from,
            snapshot,
            now,
            now.date().isoformat(),
        )
        updated = connection.execute(
            """
            UPDATE training_plans
            SET current_revision = %s, status = %s, updated_at = %s
            WHERE profile_id = %s AND id = %s AND current_revision = %s
            """,
            (
                revision,
                snapshot["status"],
                now,
                profile_id,
                plan_id,
                expected_revision,
            ),
        ).rowcount
        if updated != 1:
            raise _AppRecordStale("revision does not match current state")
        connection.execute("SET CONSTRAINTS ALL IMMEDIATE")

    @staticmethod
    def _assert_available_matches(
        connection: psycopg.Connection,
        profile_id: UUID,
        decoded_matches: Sequence[tuple[UUID, str]],
    ) -> None:
        if not decoded_matches:
            return
        expected = dict(decoded_matches)
        rows = connection.execute(
            """
            SELECT id, ownership FROM training_sessions
            WHERE profile_id = %s AND id = ANY(%s)
            """,
            (profile_id, list(expected)),
        ).fetchall()
        actual = {row[0]: row[1] for row in rows}
        if any(
            actual.get(identifier) != ownership
            for identifier, ownership in expected.items()
        ):
            raise _AppRecordConflict(
                "match references an unavailable Training Session"
            )

    @staticmethod
    def _lock_current_match_sessions(
        connection: psycopg.Connection,
        profile_id: UUID,
        plan_id: UUID,
        planned_sessions: Sequence[Mapping[str, Any]],
    ) -> None:
        outgoing = {
            row[0]
            for row in connection.execute(
                """
                SELECT training_session_id
                FROM planned_session_current_matches
                WHERE profile_id = %s AND plan_id = %s
                """,
                (profile_id, plan_id),
            ).fetchall()
        }
        incoming = dict(
            _decode_training_session(public_id)
            for item in planned_sessions
            for public_id in item["matches"]
        )
        # A cross-plan reassignment can otherwise have each transaction delete
        # one unique-index entry and wait while inserting the other's entry.
        # NO KEY UPDATE conflicts with the same lock without conflicting with
        # FK KEY SHARE locks taken for Training Session references.
        for training_session_id in sorted(
            outgoing | set(incoming), key=lambda item: item.int
        ):
            expected_ownership = incoming.get(training_session_id)
            row = connection.execute(
                """
                SELECT 1 FROM training_sessions
                WHERE profile_id = %s AND id = %s
                  AND (%s::text IS NULL OR ownership = %s)
                FOR NO KEY UPDATE
                """,
                (
                    profile_id,
                    training_session_id,
                    expected_ownership,
                    expected_ownership,
                ),
            ).fetchone()
            if row is None:
                raise _AppRecordConflict(
                    "match references an unavailable Training Session"
                )

    @staticmethod
    def _lock_current_goal_refs(
        connection: psycopg.Connection,
        profile_id: UUID,
        refs: Sequence[Mapping[str, Any]],
    ) -> None:
        decoded = sorted(
            [(_decode_app_id(ref["goal_event_id"]), ref["goal_event_revision"]) for ref in refs],
            key=lambda item: item[0].int,
        )
        for identifier, expected_revision in decoded:
            row = connection.execute(
                """
                SELECT revision FROM goal_events
                WHERE profile_id = %s AND id = %s FOR UPDATE
                """,
                (profile_id, identifier),
            ).fetchone()
            if row is None or row[0] != expected_revision:
                raise _AppRecordConflict(
                    "goal event reference is missing or stale and requires review"
                )

    @staticmethod
    def _stale_goal_refs(
        connection: psycopg.Connection,
        profile_id: UUID,
        refs: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        result = []
        for ref in refs:
            identifier = _decode_app_id(ref["goal_event_id"])
            row = connection.execute(
                "SELECT revision FROM goal_events WHERE profile_id = %s AND id = %s",
                (profile_id, identifier),
            ).fetchone()
            stale = row is None or row[0] != ref["goal_event_revision"]
            result.append(
                {
                    **ref,
                    "stale": stale,
                    "stale_reason": (
                        "missing" if row is None else "revision_advanced" if stale else None
                    ),
                }
            )
        return result

    @staticmethod
    def _apply_adjustment(
        operation: Mapping[str, Any],
        index: int,
        sessions: dict[str, dict[str, Any]],
        order: list[str],
        effective: str,
        starts_on: str,
        ends_on: str,
    ) -> None:
        if not isinstance(operation, Mapping):
            raise AppRecordStoreError(f"operations[{index}] must be an object")
        op = dict(operation)
        kind = op.get("op")
        if not isinstance(kind, str) or kind not in {"add", "update", "cancel"}:
            raise AppRecordStoreError(
                f"operations[{index}].op must be add, update, or cancel"
            )
        if kind == "add":
            body = {key: value for key, value in op.items() if key != "op"}
            prescription = _validated(
                _validate_planned_session_prescription,
                body,
                starts_on,
                ends_on,
                label=f"operations[{index}]",
            )
            if prescription["scheduled_date"] < effective:
                raise AppRecordStoreError(
                    f"operations[{index}] adds a session before effective_from"
                )
            item = _planned_session(uuid4().hex, prescription, "scheduled", [], None)
            sessions[item["id"]] = item
            order.append(item["id"])
            return
        target_id = op.get("planned_session_id")
        if not isinstance(target_id, str) or target_id not in sessions:
            raise _AppRecordNotFound("requested record is not available")
        current = sessions[target_id]
        if current["scheduled_date"] < effective:
            raise AppRecordStoreError(
                f"operations[{index}] cannot change a session before effective_from"
            )
        if kind == "update":
            body = {
                key: value
                for key, value in op.items()
                if key not in {"op", "planned_session_id"}
            }
            prescription = _validated(
                _validate_planned_session_prescription,
                body,
                starts_on,
                ends_on,
                label=f"operations[{index}]",
            )
            if prescription["scheduled_date"] < effective:
                raise AppRecordStoreError(
                    f"operations[{index}] reschedules a session before effective_from"
                )
            sessions[target_id] = _planned_session(
                target_id,
                prescription,
                current["disposition"],
                current["matches"],
                current["fulfilment_note"],
            )
            return
        unknown = set(op) - {"op", "planned_session_id"}
        if unknown:
            raise AppRecordStoreError(
                f"operations[{index}] cancel has unknown fields: "
                + ", ".join(sorted(unknown))
            )
        if current["disposition"] != "scheduled":
            raise AppRecordStoreError(
                f"operations[{index}] can only cancel a scheduled session"
            )
        sessions[target_id] = {**current, "disposition": "cancelled"}


def _validated(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except _LegacyInvalidRecord as exc:
        raise AppRecordStoreError(str(exc)) from None
    except (TypeError, ValueError):
        raise AppRecordStoreError("request contains an invalid value") from None


def _list_or_none(value: Sequence[Any] | None) -> list[Any] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AppRecordStoreError("value must be a sequence")
    return list(value)


def _revision(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AppRecordStoreError("expected_revision must be a positive integer")
    return value


def _decode_app_id(value: str) -> UUID:
    match = _APP_RE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise _AppRecordNotFound("requested record is not available")
    return UUID(hex=match.group(1))


def _decode_app_mutation_id(value: str, noun: str) -> UUID:
    if isinstance(value, str) and (
        value.startswith("session:") or value.startswith("garmin:")
    ):
        raise _AppRecordReadOnly(f"collected {noun} is read-only")
    return _decode_app_id(value)


def _decode_training_session(value: str) -> tuple[UUID, str]:
    if not isinstance(value, str):
        raise _AppRecordNotFound("requested record is not available")
    app = _APP_RE.fullmatch(value)
    if app:
        return UUID(hex=app.group(1)), "app"
    collected = _SESSION_RE.fullmatch(value)
    if collected:
        return UUID(hex=collected.group(1)), "collected"
    raise _AppRecordNotFound("requested record is not available")


def _app_id(identifier: UUID) -> str:
    return f"app:{identifier.hex}"


def _training_session_id(identifier: UUID, ownership: str) -> str:
    return _app_id(identifier) if ownership == "app" else f"session:{identifier.hex}"


def _local_start(value: datetime | None) -> str | None:
    return value.isoformat(sep=" ") if value is not None else None


def _number(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _naive_local(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=None)


def _session_columns(content: Mapping[str, Any]) -> tuple[Any, ...]:
    duration = content.get("duration")
    distance = content.get("distance")
    return (
        content["local_date"],
        _naive_local(content.get("local_start")),
        content["timing_precision"],
        content.get("time_zone"),
        content.get("utc_offset"),
        content["sport"],
        content.get("session_type"),
        content.get("title"),
        content.get("session_rpe"),
        duration.get("value") if duration else None,
        duration.get("unit") if duration else None,
        duration.get("basis") if duration else None,
        distance.get("value") if distance else None,
        distance.get("unit") if distance else None,
        content.get("notes"),
    )


def _goal_columns(content: Mapping[str, Any]) -> tuple[Any, ...]:
    distance = content.get("distance")
    goal = content.get("goal")
    target = goal.get("target_duration") if goal else None
    outcome = content.get("outcome")
    actual = outcome.get("actual_duration") if outcome else None
    return (
        content["local_date"],
        _naive_local(content.get("local_start")),
        content["timing_precision"],
        content.get("time_zone"),
        content.get("utc_offset"),
        content["sport"],
        content["name"],
        content["priority"],
        content["status"],
        distance.get("value") if distance else None,
        distance.get("unit") if distance else None,
        target.get("value") if target else None,
        target.get("unit") if target else None,
        goal.get("statement") if goal else None,
        actual.get("value") if actual else None,
        actual.get("unit") if actual else None,
        outcome.get("statement") if outcome else None,
        content.get("notes"),
    )


def _annotation_values(
    notes: str | None, reliability: str | None, duplicate: bool | None
) -> tuple[str | None, str | None, bool | None]:
    normalized_notes = _validated(_optional_text, notes, "notes")
    if reliability is not None and (
        not isinstance(reliability, str)
        or reliability not in {"reliable", "unreliable"}
    ):
        raise AppRecordStoreError("reliability must be reliable, unreliable, or null")
    if duplicate is not None and not isinstance(duplicate, bool):
        raise AppRecordStoreError("duplicate must be an explicit boolean or null")
    if normalized_notes is None and reliability is None and duplicate is None:
        raise AppRecordStoreError("an annotation must record at least one value")
    return normalized_notes, reliability, duplicate


def _copy_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return _plan_snapshot(
        snapshot["name"],
        snapshot["starts_on"],
        snapshot["ends_on"],
        snapshot["status"],
        [dict(item) for item in snapshot["goal_events"]],
        list(snapshot["constraints"]),
        [dict(item) for item in snapshot["planned_sessions"]],
    )
