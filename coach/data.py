"""File-backed adapter for coherent coaching context and app-owned records."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

GARMIN_SPORTS = {
    "running": "running",
    "trail_running": "running",
    "treadmill_running": "running",
    "track_running": "running",
    "virtual_running": "running",
}


APP_ID_PREFIX = "app:"
GARMIN_ID_PREFIX = "garmin:"
_APP_ID_RE = re.compile(r"^app:[0-9a-f]{32}$")
_SESSION_CONTENT_FIELDS = {
    "sport",
    "session_type",
    "local_date",
    "local_start",
    "time_zone",
    "utc_offset",
    "title",
    "notes",
    "duration",
    "distance",
    "session_rpe",
    "loads",
}
_DURATION_UNITS = {"seconds"}
_DURATION_BASES = {"elapsed", "active"}
_LOAD_FIELDS = {"value", "unit", "method", "source"}
_TIMING_PRECISIONS = {"date_only", "local_datetime"}
_GOAL_EVENT_CONTENT_FIELDS = {
    "name",
    "sport",
    "local_date",
    "local_start",
    "time_zone",
    "utc_offset",
    "priority",
    "status",
    "distance",
    "goal",
    "outcome",
    "notes",
}
_GOAL_EVENT_PRIORITIES = {"primary", "secondary", "practice"}
_GOAL_EVENT_STATUSES = {"scheduled", "completed", "cancelled"}
_GOAL_FIELDS = {"target_duration", "statement"}
_OUTCOME_FIELDS = {"actual_duration", "statement"}
_PLAN_STATUSES = {"draft", "active", "archived"}
_PLAN_REVISION_KINDS = {"create", "adjustment", "lifecycle", "fulfilment_correction"}
_PLANNED_SESSION_INPUT_FIELDS = {
    "scheduled_date",
    "sport",
    "session_type",
    "prescription",
    "target_duration_seconds",
    "target_distance_meters",
    "effort_guidance",
}
_PLANNED_SESSION_DISPOSITIONS = {"scheduled", "fulfilled", "skipped", "cancelled"}
_FULFILMENT_DISPOSITIONS = {"scheduled", "fulfilled", "skipped"}
_GOAL_REF_FIELDS = {"goal_event_id", "goal_event_revision"}
_ADJUST_OPS = {"add", "update", "cancel"}


class CoachError(RuntimeError):
    """Base for structured errors the adapter exposes to callers."""


class NotFound(CoachError):
    """No record exists for the requested identity."""


class AlreadyExists(CoachError):
    """A record already exists for the requested identity."""


class RevisionConflict(CoachError):
    """The stored revision does not match the caller's expected revision."""


class InvalidRecord(CoachError):
    """A submitted record fails validation."""


class ReadOnlyRecord(CoachError):
    """The requested record is source-owned and cannot be mutated."""


class DataIntegrityError(CoachError):
    """A stored record does not satisfy the adapter's stable input contract."""


class StorageError(CoachError):
    """The adapter could not read or write its configured store."""


class ReferencedRecord(CoachError):
    """A record cannot be deleted while another record still references it."""


@dataclass(frozen=True)
class ContextWindow:
    """Inclusive bounded period requested by a coaching client."""

    days: int = 14
    end_date: date | None = None

    def bounds(self) -> tuple[date, date]:
        if not 1 <= self.days <= 90:
            raise ValueError("days must be between 1 and 90")
        end = self.end_date or date.today()
        return end - timedelta(days=self.days - 1), end


class CoachData:
    """Read coherent coaching context without exposing filesystem primitives."""

    def __init__(self, data_root: Path | str) -> None:
        self._root = Path(data_root)

    def read_context(self, window: ContextWindow) -> dict[str, Any]:
        """Read one bounded athlete, wellness, health, and session projection."""
        start, end = window.bounds()
        athlete = self._read_athlete()
        wellness, missing_dates = self._read_wellness(start, end)
        training_history = self._read_training_history(start, end)
        collection_health = self._read_collection_health()

        return {
            "window": {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "days": window.days,
            },
            "athlete": athlete,
            "wellness": {
                "records": wellness,
                "missing_dates": missing_dates,
            },
            "collection_health": collection_health,
            "freshness": {
                "athlete_snapshot_date": (
                    athlete.get("snapshot_date") if athlete is not None else None
                ),
                "wellness_latest_date": (
                    wellness[-1].get("date") if wellness else None
                ),
                "training_history_latest_date": (
                    training_history[0].get("local_date")
                    if training_history
                    else None
                ),
            },
            "training_history": training_history,
        }

    def _read_collection_health(self) -> dict[str, Any] | None:
        health = self._read_optional_json(
            Path("index/collector-health.json"), dict
        )
        if health is not None and health.get("schema_version") != 1:
            raise DataIntegrityError(
                "Unsupported collector health schema_version"
            )
        return health

    def _read_athlete(self) -> dict[str, Any] | None:
        athlete = self._read_optional_json(Path("derived/athlete.json"), dict)
        if athlete is None:
            return None
        return {
            key: value
            for key, value in athlete.items()
            if key != "user_profile_id"
        }

    def _read_wellness(
        self, start: date, end: date
    ) -> tuple[list[dict[str, Any]], list[str]]:
        records: list[dict[str, Any]] = []
        missing_dates: list[str] = []
        current = start
        while current <= end:
            iso_date = current.isoformat()
            record = self._read_optional_json(
                Path("derived/daily") / f"{iso_date}.json", dict
            )
            if record is None:
                missing_dates.append(iso_date)
            else:
                if record.get("date") != iso_date:
                    raise DataIntegrityError(
                        f"Record date does not match derived/daily/{iso_date}.json"
                    )
                records.append(record)
            current += timedelta(days=1)
        return records, missing_dates

    def _read_training_history(
        self, start: date, end: date
    ) -> list[dict[str, Any]] | None:
        activities = self._read_optional_json(Path("derived/activities.json"), list)
        if activities is None:
            return None

        sessions: list[dict[str, Any]] = []
        for index, activity in enumerate(activities):
            if not isinstance(activity, dict):
                raise DataIntegrityError(
                    f"Expected object at derived/activities.json item {index}"
                )
            local_date = self._activity_date(activity, index)
            if local_date is None or not start <= local_date <= end:
                continue
            sessions.append(self._project_session(activity, local_date))

        sessions.sort(
            key=lambda session: session.get("local_start") or session["local_date"],
            reverse=True,
        )
        return sessions

    @staticmethod
    def _activity_date(activity: dict[str, Any], index: int) -> date | None:
        local_start = activity.get("start_local")
        if local_start is None:
            return None
        if not isinstance(local_start, str):
            raise DataIntegrityError(
                f"Expected string start_local at derived/activities.json item {index}"
            )
        try:
            return date.fromisoformat(local_start[:10])
        except ValueError as exc:
            raise DataIntegrityError(
                f"Invalid start_local at derived/activities.json item {index}"
            ) from exc

    @staticmethod
    def _project_session(
        activity: dict[str, Any], local_date: date
    ) -> dict[str, Any]:
        source_id = activity.get("activity_id")
        if source_id is None:
            raise DataIntegrityError(
                "Missing activity_id in derived/activities.json"
            )

        duration = activity.get("duration_s")
        distance = activity.get("distance_m")
        return {
            "id": f"garmin:{source_id}",
            "origin": "garmin_derived",
            "local_date": local_date.isoformat(),
            "local_start": activity.get("start_local"),
            "timing_precision": "local_datetime",
            "sport": GARMIN_SPORTS.get(activity.get("type")),
            "session_type": None,
            "title": activity.get("name"),
            "duration": (
                {
                    "value": duration,
                    "unit": "seconds",
                    "basis": "source_reported",
                }
                if duration is not None
                else None
            ),
            "distance": (
                {"value": distance, "unit": "meters"}
                if distance is not None
                else None
            ),
            "session_rpe": None,
            "loads": None,
            "source_detail": {
                "provider": "garmin",
                "activity_type": activity.get("type"),
                "average_heart_rate": activity.get("avg_hr"),
                "maximum_heart_rate": activity.get("max_hr"),
                "elevation_gain_m": activity.get("elevation_gain_m"),
                "average_speed_mps": activity.get("avg_speed_mps"),
                "calories": activity.get("calories"),
                "training_effect_aerobic": activity.get(
                    "training_effect_aerobic"
                ),
                "training_effect_anaerobic": activity.get(
                    "training_effect_anaerobic"
                ),
            },
        }

    def _read_optional_json(
        self, relative_path: Path, expected_type: type
    ) -> Any | None:
        path = self._root / relative_path
        try:
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DataIntegrityError(f"Malformed JSON in {relative_path}") from exc
        except OSError as exc:
            raise StorageError(f"Could not read {relative_path}") from exc

        if not isinstance(value, expected_type):
            raise DataIntegrityError(
                f"Expected {expected_type.__name__} in {relative_path}"
            )
        return value

    def _read_app_json(
        self, kind: _RecordKind, relative_path: Path
    ) -> dict[str, Any] | None:
        """Read an app record, mapping storage errors to path-free messages."""
        try:
            return self._read_optional_json(relative_path, dict)
        except DataIntegrityError as exc:
            raise DataIntegrityError(
                f"Stored {kind.noun} record is malformed"
            ) from exc
        except StorageError as exc:
            raise StorageError(f"Could not read {kind.noun}") from exc

    # -- App-owned Training Sessions -------------------------------------
    # The MVP assumes a single local writer; the revision check plus atomic
    # temp-file replace prevent sequential last-write-wins. Cross-process
    # compare-and-swap locking is out of scope for the single-profile MVP.

    def create_session(self, content: dict[str, Any]) -> dict[str, Any]:
        """Create a new app-owned Training Session and return its projection."""
        normalized = _validate_session_content(content)
        record_id, record = _new_app_record(normalized)
        self._write_app_record(_SESSION_KIND, record_id, record, create=True)
        return self._project_app_session(record)

    def get_session(self, session_id: str) -> dict[str, Any]:
        """Return one Training Session by opaque id (app-owned or Garmin-derived)."""
        if isinstance(session_id, str) and session_id.startswith(GARMIN_ID_PREFIX):
            return self._get_garmin_session(session_id)
        record = self._load_app_record(_SESSION_KIND, session_id)
        return self._project_app_session(record)

    def list_sessions(
        self, window: ContextWindow, sport: str | None = None
    ) -> list[dict[str, Any]]:
        """List Garmin-derived and app-owned sessions within the window."""
        start, end = window.bounds()
        sessions = list(self._read_training_history(start, end) or [])
        sessions.extend(self._read_app_sessions(start, end))
        if sport is not None:
            sessions = [s for s in sessions if s.get("sport") == sport]
        sessions.sort(
            key=lambda s: s.get("local_start") or s["local_date"], reverse=True
        )
        return sessions

    def replace_session(
        self, session_id: str, expected_revision: int, content: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace an app-owned session's editable content, checking revision."""
        record = self._replace_app_record(
            _SESSION_KIND,
            session_id,
            expected_revision,
            _validate_session_content(content),
        )
        return self._project_app_session(record)

    def delete_session(
        self, session_id: str, expected_revision: int
    ) -> dict[str, Any]:
        """Delete an app-owned session, checking revision.

        Read-only Garmin-derived sessions are rejected before the reference
        scan; a manual session matched by any retained Plan Revision cannot be
        hard-deleted until the match is corrected.
        """
        self._guard_mutable(session_id)
        self._reject_if_matched(session_id)
        return self._delete_app_record(_SESSION_KIND, session_id, expected_revision)

    # -- App-owned Goal Events ------------------------------------------
    # Goal Events share the App Record file-store convention with sessions
    # but live under their own directory. Per ADR 0002 a referenced event
    # cannot be deleted until plans detach from it; Training Plans do not
    # exist yet, so no plan-reference guard is wired in this slice.

    def create_goal_event(self, content: dict[str, Any]) -> dict[str, Any]:
        """Create a new app-owned Goal Event and return its projection."""
        normalized = _validate_goal_event_content(content)
        record_id, record = _new_app_record(normalized)
        self._write_app_record(_GOAL_EVENT_KIND, record_id, record, create=True)
        return self._project_goal_event(record)

    def get_goal_event(self, goal_event_id: str) -> dict[str, Any]:
        """Return one Goal Event by its opaque app id."""
        record = self._load_app_record(_GOAL_EVENT_KIND, goal_event_id)
        return self._project_goal_event(record)

    def list_goal_events(
        self, sport: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List app-owned Goal Events, soonest first, optionally filtered."""
        events = [
            self._project_goal_event(record)
            for record in self._iter_app_records(_GOAL_EVENT_KIND)
        ]
        if sport is not None:
            events = [e for e in events if e.get("sport") == sport]
        if status is not None:
            events = [e for e in events if e.get("status") == status]
        events.sort(key=_goal_event_sort_key)
        return events

    def replace_goal_event(
        self, goal_event_id: str, expected_revision: int, content: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace a Goal Event's editable content, checking revision."""
        record = self._replace_app_record(
            _GOAL_EVENT_KIND,
            goal_event_id,
            expected_revision,
            _validate_goal_event_content(content),
        )
        return self._project_goal_event(record)

    def delete_goal_event(
        self, goal_event_id: str, expected_revision: int
    ) -> dict[str, Any]:
        """Delete a Goal Event, checking revision.

        A Goal Event referenced by any retained Plan Revision cannot be
        hard-deleted; the plan must detach from it first.
        """
        self._guard_mutable(goal_event_id)
        self._reject_if_referenced(goal_event_id)
        return self._delete_app_record(
            _GOAL_EVENT_KIND, goal_event_id, expected_revision
        )

    # -- App-owned Training Plans ---------------------------------------
    # A Training Plan is a revisioned App Record whose content is an ordered
    # list of immutable Plan Revisions; the last entry is the current head.
    # Every accepted mutation appends one revision and advances the envelope
    # revision atomically via the shared temp-file replace. Activation's
    # single-active-plan uniqueness check assumes the MVP's single local
    # writer, like the other App Record mutations above.

    def create_plan(
        self,
        name: str,
        starts_on: str,
        ends_on: str,
        reason: str,
        *,
        goal_events: list[dict[str, Any]] | None = None,
        constraints: list[str] | None = None,
        planned_sessions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a draft Training Plan and return its head projection."""
        name = _validate_name(name)
        starts = _validate_plan_date(starts_on, "starts_on")
        ends = _validate_plan_date(ends_on, "ends_on")
        if starts > ends:
            raise InvalidRecord("starts_on must be on or before ends_on")
        reason = _validate_reason(reason)
        refs = _validate_goal_refs(goal_events)
        self._ensure_goal_refs_current(refs)
        constraint_list = _validate_constraints(constraints)
        planned: list[dict[str, Any]] = []
        if planned_sessions is not None:
            if not isinstance(planned_sessions, list):
                raise InvalidRecord("planned_sessions must be a list")
            for index, item in enumerate(planned_sessions):
                prescription = _validate_planned_session_prescription(
                    item, starts, ends, label=f"planned_sessions[{index}]"
                )
                planned.append(
                    _planned_session(uuid.uuid4().hex, prescription, "scheduled", [], None)
                )
        snapshot = _plan_snapshot(
            name, starts, ends, "draft", refs, constraint_list, planned
        )
        entry = _plan_revision_entry(1, "create", reason, None, snapshot)
        record_id, record = _new_app_record({"revisions": [entry]})
        self._write_app_record(_PLAN_KIND, record_id, record, create=True)
        return self._project_plan(record)

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        """Return one Training Plan head by its opaque id."""
        return self._project_plan(self._load_app_record(_PLAN_KIND, plan_id))

    def list_plans(self, status: str | None = None) -> list[dict[str, Any]]:
        """List Training Plan heads, earliest start first, optionally filtered."""
        plans = [
            self._project_plan(record)
            for record in self._iter_app_records(_PLAN_KIND)
        ]
        if status is not None:
            plans = [p for p in plans if p["status"] == status]
        plans.sort(key=lambda p: (p["starts_on"], p["id"]))
        return plans

    def get_plan_history(self, plan_id: str) -> dict[str, Any]:
        """Return the immutable Plan Revision history, oldest first."""
        record = self._load_app_record(_PLAN_KIND, plan_id)
        return {
            "id": record["id"],
            "revisions": [
                self._project_plan_revision(entry)
                for entry in record["content"]["revisions"]
            ],
        }

    def activate_plan(
        self, plan_id: str, expected_revision: int, reason: str
    ) -> dict[str, Any]:
        """Activate a draft plan, enforcing single-active uniqueness."""
        self._guard_mutable(plan_id)
        record = self._load_app_record(_PLAN_KIND, plan_id)
        self._check_revision(_PLAN_KIND, record, expected_revision)
        head = self._plan_head(record)
        if head["status"] != "draft":
            raise InvalidRecord("only a draft plan can be activated")
        reason = _validate_reason(reason)
        self._ensure_goal_refs_current(head["goal_events"])
        today = date.today().isoformat()
        for planned in head["planned_sessions"]:
            if (
                planned["disposition"] == "scheduled"
                and planned["scheduled_date"] < today
            ):
                raise InvalidRecord(
                    "activation requires resolving scheduled prescriptions "
                    "dated before the activation date"
                )
        for other in self._iter_app_records(_PLAN_KIND):
            if other["id"] == plan_id:
                continue
            if self._plan_head(other)["status"] == "active":
                raise InvalidRecord(f"plan {other['id']} is already active")
        return self._commit_lifecycle(record, "active", reason)

    def archive_plan(
        self, plan_id: str, expected_revision: int, reason: str
    ) -> dict[str, Any]:
        """Archive a draft or active plan; archiving is terminal."""
        self._guard_mutable(plan_id)
        record = self._load_app_record(_PLAN_KIND, plan_id)
        self._check_revision(_PLAN_KIND, record, expected_revision)
        head = self._plan_head(record)
        if head["status"] == "archived":
            raise InvalidRecord("plan is already archived")
        reason = _validate_reason(reason)
        return self._commit_lifecycle(record, "archived", reason)

    def delete_plan(
        self, plan_id: str, expected_revision: int
    ) -> dict[str, Any]:
        """Delete a never-active draft plan without fulfilment history."""
        self._guard_mutable(plan_id)
        record = self._load_app_record(_PLAN_KIND, plan_id)
        self._check_revision(_PLAN_KIND, record, expected_revision)
        revisions = record["content"]["revisions"]
        ever_active = any(
            entry["plan"]["status"] == "active" for entry in revisions
        )
        has_fulfilment = any(
            entry["kind"] == "fulfilment_correction" for entry in revisions
        ) or any(
            planned["disposition"] in ("fulfilled", "skipped")
            or planned["matches"]
            or planned["fulfilment_note"]
            for entry in revisions
            for planned in entry["plan"]["planned_sessions"]
        )
        if self._plan_head(record)["status"] != "draft" or ever_active or has_fulfilment:
            raise InvalidRecord(
                "only a never-active draft without fulfilment history can be "
                "deleted; archive the plan instead"
            )
        try:
            self._record_path(_PLAN_KIND, plan_id).unlink()
        except FileNotFoundError as exc:
            raise NotFound(f"Training plan {plan_id} does not exist") from exc
        except OSError as exc:
            raise StorageError("Could not delete training plan") from exc
        return {"id": plan_id, "deleted": True, "revision": record["revision"]}

    def adjust_plan(
        self,
        plan_id: str,
        expected_revision: int,
        reason: str,
        effective_from: str,
        operations: list[dict[str, Any]],
        *,
        name: str | None = None,
        starts_on: str | None = None,
        ends_on: str | None = None,
        goal_events: list[dict[str, Any]] | None = None,
        constraints: list[str] | None = None,
    ) -> dict[str, Any]:
        """Append a reasoned adjustment that changes only future prescriptions."""
        self._guard_mutable(plan_id)
        record = self._load_app_record(_PLAN_KIND, plan_id)
        self._check_revision(_PLAN_KIND, record, expected_revision)
        head = self._plan_head(record)
        if head["status"] == "archived":
            raise InvalidRecord(
                "archived plans accept only fulfilment corrections"
            )
        reason = _validate_reason(reason)

        new_name = head["name"] if name is None else _validate_name(name)
        new_starts = (
            head["starts_on"]
            if starts_on is None
            else _validate_plan_date(starts_on, "starts_on")
        )
        new_ends = (
            head["ends_on"]
            if ends_on is None
            else _validate_plan_date(ends_on, "ends_on")
        )
        if new_starts > new_ends:
            raise InvalidRecord("starts_on must be on or before ends_on")

        effective = _validate_plan_date(effective_from, "effective_from")
        today = date.today().isoformat()
        if effective < today:
            raise InvalidRecord("effective_from must not be earlier than today")
        prior = self._frozen_boundary(record)
        if prior is not None and effective < prior:
            raise InvalidRecord(
                "effective_from must not move earlier than a prior adjustment"
            )
        if not new_starts <= effective <= new_ends:
            raise InvalidRecord("effective_from must fall within the plan range")

        sessions = {ps["id"]: dict(ps) for ps in head["planned_sessions"]}
        order = [ps["id"] for ps in head["planned_sessions"]]
        if not isinstance(operations, list):
            raise InvalidRecord("operations must be a list")
        for index, op in enumerate(operations):
            self._apply_adjustment_op(
                op, index, sessions, order, effective, new_starts, new_ends, plan_id
            )
        planned_sessions = [sessions[i] for i in order]

        for ps in planned_sessions:
            if not new_starts <= ps["scheduled_date"] <= new_ends:
                raise InvalidRecord(
                    "a retained planned session would fall outside the plan range"
                )
        old_before = [
            ps for ps in head["planned_sessions"] if ps["scheduled_date"] < effective
        ]
        new_before = [
            ps for ps in planned_sessions if ps["scheduled_date"] < effective
        ]
        if old_before != new_before:
            raise InvalidRecord("prescriptions before effective_from are frozen")

        new_refs = (
            [dict(r) for r in head["goal_events"]]
            if goal_events is None
            else _validate_goal_refs(goal_events)
        )
        self._ensure_goal_refs_current(new_refs)
        new_constraints = (
            list(head["constraints"])
            if constraints is None
            else _validate_constraints(constraints)
        )

        snapshot = _plan_snapshot(
            new_name,
            new_starts,
            new_ends,
            head["status"],
            new_refs,
            new_constraints,
            planned_sessions,
        )
        entry = _plan_revision_entry(
            record["revision"] + 1,
            "adjustment",
            reason,
            effective,
            snapshot,
            recorded_date=today,
        )
        updated = _append_plan_revision(record, entry)
        self._write_app_record(_PLAN_KIND, plan_id, updated, create=False)
        return self._project_plan(updated)

    def set_planned_session_fulfilment(
        self,
        plan_id: str,
        expected_revision: int,
        planned_session_id: str,
        disposition: str,
        reason: str,
        matches: list[str] | None = None,
        fulfilment_note: str | None = None,
    ) -> dict[str, Any]:
        """Record an audited fulfilment/match correction without touching a
        prescription; allowed on past and archived history."""
        self._guard_mutable(plan_id)
        record = self._load_app_record(_PLAN_KIND, plan_id)
        self._check_revision(_PLAN_KIND, record, expected_revision)
        reason = _validate_reason(reason)
        if disposition not in _FULFILMENT_DISPOSITIONS:
            raise InvalidRecord(
                "disposition must be scheduled, fulfilled, or skipped"
            )
        head = self._plan_head(record)
        target = next(
            (ps for ps in head["planned_sessions"] if ps["id"] == planned_session_id),
            None,
        )
        if target is None:
            raise NotFound(
                f"Planned session {planned_session_id} does not exist in "
                f"plan {plan_id}"
            )
        if target["disposition"] == "cancelled":
            raise InvalidRecord(
                "a cancelled planned session is withdrawn prescribed work; "
                "reinstate it with a plan adjustment before recording fulfilment"
            )
        normalized_matches = _normalize_matches(matches, disposition)
        for training_session_id in normalized_matches:
            try:
                self.get_session(training_session_id)
            except NotFound as exc:
                raise InvalidRecord(
                    f"match references unknown training session {training_session_id}"
                ) from exc
            if self._match_used_elsewhere(
                training_session_id, plan_id, planned_session_id
            ):
                raise InvalidRecord(
                    f"training session {training_session_id} already fulfils "
                    "another planned session"
                )
        note = _optional_text(fulfilment_note, "fulfilment_note")
        updated_sessions: list[dict[str, Any]] = []
        for ps in head["planned_sessions"]:
            new_ps = dict(ps)
            if ps["id"] == planned_session_id:
                new_ps["disposition"] = disposition
                new_ps["matches"] = normalized_matches
                new_ps["fulfilment_note"] = note
            updated_sessions.append(new_ps)
        snapshot = _plan_snapshot(
            head["name"],
            head["starts_on"],
            head["ends_on"],
            head["status"],
            head["goal_events"],
            head["constraints"],
            updated_sessions,
        )
        entry = _plan_revision_entry(
            record["revision"] + 1,
            "fulfilment_correction",
            reason,
            None,
            snapshot,
        )
        updated = _append_plan_revision(record, entry)
        self._write_app_record(_PLAN_KIND, plan_id, updated, create=False)
        return self._project_plan(updated)

    # -- Training Plan internals ----------------------------------------

    @staticmethod
    def _plan_head(record: dict[str, Any]) -> dict[str, Any]:
        return record["content"]["revisions"][-1]["plan"]

    @staticmethod
    def _frozen_boundary(record: dict[str, Any]) -> str | None:
        boundaries = [
            entry["effective_from"]
            for entry in record["content"]["revisions"]
            if entry.get("effective_from")
        ]
        return max(boundaries) if boundaries else None

    def _apply_adjustment_op(
        self,
        op: Any,
        index: int,
        sessions: dict[str, dict[str, Any]],
        order: list[str],
        effective: str,
        starts_on: str,
        ends_on: str,
        plan_id: str,
    ) -> None:
        if not isinstance(op, dict):
            raise InvalidRecord(f"operations[{index}] must be an object")
        kind = op.get("op")
        if not isinstance(kind, str) or kind not in _ADJUST_OPS:
            raise InvalidRecord(
                f"operations[{index}].op must be add, update, or cancel"
            )
        if kind == "add":
            body = {k: v for k, v in op.items() if k != "op"}
            prescription = _validate_planned_session_prescription(
                body, starts_on, ends_on, label=f"operations[{index}]"
            )
            if prescription["scheduled_date"] < effective:
                raise InvalidRecord(
                    f"operations[{index}] adds a session before effective_from"
                )
            new_session = _planned_session(
                uuid.uuid4().hex, prescription, "scheduled", [], None
            )
            sessions[new_session["id"]] = new_session
            order.append(new_session["id"])
            return
        target_id = op.get("planned_session_id")
        if not isinstance(target_id, str) or target_id not in sessions:
            raise NotFound(
                f"Planned session {target_id} does not exist in plan {plan_id}"
            )
        current = sessions[target_id]
        if current["scheduled_date"] < effective:
            raise InvalidRecord(
                f"operations[{index}] cannot change a session dated before "
                "effective_from"
            )
        if kind == "update":
            body = {
                k: v
                for k, v in op.items()
                if k not in ("op", "planned_session_id")
            }
            prescription = _validate_planned_session_prescription(
                body, starts_on, ends_on, label=f"operations[{index}]"
            )
            if prescription["scheduled_date"] < effective:
                raise InvalidRecord(
                    f"operations[{index}] reschedules a session before "
                    "effective_from"
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
            raise InvalidRecord(
                f"operations[{index}] cancel has unknown fields: "
                + ", ".join(sorted(unknown))
            )
        if current["disposition"] != "scheduled":
            raise InvalidRecord(
                f"operations[{index}] can only cancel a scheduled session"
            )
        cancelled = dict(current)
        cancelled["disposition"] = "cancelled"
        sessions[target_id] = cancelled

    def _commit_lifecycle(
        self, record: dict[str, Any], status: str, reason: str
    ) -> dict[str, Any]:
        head = self._plan_head(record)
        snapshot = _plan_snapshot(
            head["name"],
            head["starts_on"],
            head["ends_on"],
            status,
            head["goal_events"],
            head["constraints"],
            head["planned_sessions"],
        )
        entry = _plan_revision_entry(
            record["revision"] + 1, "lifecycle", reason, None, snapshot
        )
        updated = _append_plan_revision(record, entry)
        self._write_app_record(_PLAN_KIND, record["id"], updated, create=False)
        return self._project_plan(updated)

    def _ensure_goal_refs_current(self, refs: list[dict[str, Any]]) -> None:
        for ref in refs:
            try:
                current = self._load_app_record(
                    _GOAL_EVENT_KIND, ref["goal_event_id"]
                )
            except NotFound as exc:
                raise InvalidRecord(
                    f"goal event {ref['goal_event_id']} does not exist"
                ) from exc
            if current["revision"] != ref["goal_event_revision"]:
                raise InvalidRecord(
                    f"goal event {ref['goal_event_id']} is at revision "
                    f"{current['revision']}, not the referenced "
                    f"{ref['goal_event_revision']}; review the plan reference"
                )

    def _match_used_elsewhere(
        self, session_id: str, plan_id: str, planned_session_id: str
    ) -> bool:
        for record in self._iter_app_records(_PLAN_KIND):
            for planned in self._plan_head(record)["planned_sessions"]:
                if session_id in planned["matches"] and not (
                    record["id"] == plan_id
                    and planned["id"] == planned_session_id
                ):
                    return True
        return False

    def _reject_if_referenced(self, goal_event_id: str) -> None:
        for record in self._iter_app_records(_PLAN_KIND):
            for entry in record["content"]["revisions"]:
                for ref in entry["plan"]["goal_events"]:
                    if ref["goal_event_id"] == goal_event_id:
                        raise ReferencedRecord(
                            f"Goal event {goal_event_id} is referenced by plan "
                            f"{record['id']} and cannot be deleted"
                        )

    def _reject_if_matched(self, session_id: str) -> None:
        for record in self._iter_app_records(_PLAN_KIND):
            for entry in record["content"]["revisions"]:
                for planned in entry["plan"]["planned_sessions"]:
                    if session_id in planned["matches"]:
                        raise ReferencedRecord(
                            f"Training session {session_id} is matched by plan "
                            f"{record['id']} and cannot be deleted"
                        )

    def _project_plan(self, record: dict[str, Any]) -> dict[str, Any]:
        plan = self._plan_head(record)
        goal_events = [self._project_goal_ref(ref) for ref in plan["goal_events"]]
        planned = sorted(
            (dict(ps) for ps in plan["planned_sessions"]),
            key=lambda ps: (ps["scheduled_date"], ps["id"]),
        )
        return {
            "id": record["id"],
            "origin": record["origin"],
            "name": plan["name"],
            "starts_on": plan["starts_on"],
            "ends_on": plan["ends_on"],
            "status": plan["status"],
            "goal_events": goal_events,
            "constraints": list(plan["constraints"]),
            "planned_sessions": planned,
            "requires_review": any(g["stale"] for g in goal_events),
            "provenance": record["provenance"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "revision": record["revision"],
        }

    def _project_goal_ref(self, ref: dict[str, Any]) -> dict[str, Any]:
        stale = False
        stale_reason: str | None = None
        try:
            current = self._load_app_record(_GOAL_EVENT_KIND, ref["goal_event_id"])
        except NotFound:
            stale = True
            stale_reason = "missing"
        else:
            if current["revision"] != ref["goal_event_revision"]:
                stale = True
                stale_reason = "revision_advanced"
        return {
            "goal_event_id": ref["goal_event_id"],
            "goal_event_revision": ref["goal_event_revision"],
            "stale": stale,
            "stale_reason": stale_reason,
        }

    @staticmethod
    def _project_plan_revision(entry: dict[str, Any]) -> dict[str, Any]:
        plan = entry["plan"]
        return {
            "revision": entry["revision"],
            "kind": entry["kind"],
            "recorded_at": entry["recorded_at"],
            "reason": entry["reason"],
            "effective_from": entry["effective_from"],
            "plan": {
                "name": plan["name"],
                "starts_on": plan["starts_on"],
                "ends_on": plan["ends_on"],
                "status": plan["status"],
                "goal_events": [dict(r) for r in plan["goal_events"]],
                "constraints": list(plan["constraints"]),
                "planned_sessions": [dict(ps) for ps in plan["planned_sessions"]],
            },
        }

    # -- App-record internals -------------------------------------------

    @staticmethod
    def _guard_mutable(record_id: str) -> None:
        if not isinstance(record_id, str) or not record_id:
            raise InvalidRecord("record id must be a non-empty string")
        if record_id.startswith(GARMIN_ID_PREFIX):
            raise ReadOnlyRecord(
                f"Garmin-derived record {record_id} is read-only"
            )

    @staticmethod
    def _check_revision(
        kind: _RecordKind, record: dict[str, Any], expected_revision: int
    ) -> None:
        if isinstance(expected_revision, bool) or not isinstance(
            expected_revision, int
        ):
            raise InvalidRecord("expected_revision must be an integer")
        current = record["revision"]
        if current != expected_revision:
            raise RevisionConflict(
                f"{kind.noun.capitalize()} {record['id']} is at revision "
                f"{current}, not the expected {expected_revision}"
            )

    def _record_relpath(self, kind: _RecordKind, record_id: str) -> Path:
        if not isinstance(record_id, str) or not _APP_ID_RE.match(record_id):
            raise NotFound(
                f"{kind.noun.capitalize()} {record_id} does not exist"
            )
        return Path("app") / kind.subdir / f"{record_id[len(APP_ID_PREFIX):]}.json"

    def _record_path(self, kind: _RecordKind, record_id: str) -> Path:
        return self._root / self._record_relpath(kind, record_id)

    def _load_app_record(
        self, kind: _RecordKind, record_id: str
    ) -> dict[str, Any]:
        record = self._read_app_json(kind, self._record_relpath(kind, record_id))
        if record is None:
            raise NotFound(
                f"{kind.noun.capitalize()} {record_id} does not exist"
            )
        return kind.coerce(record, record_id)

    def _iter_app_records(self, kind: _RecordKind) -> list[dict[str, Any]]:
        directory = self._root / "app" / kind.subdir
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError as exc:
            raise StorageError(f"Could not list {kind.noun}s") from exc
        records: list[dict[str, Any]] = []
        for path in paths:
            record = self._read_app_json(kind, path.relative_to(self._root))
            if record is None:
                continue
            records.append(kind.coerce(record, APP_ID_PREFIX + path.stem))
        return records

    def _read_app_sessions(
        self, start: date, end: date
    ) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        for record in self._iter_app_records(_SESSION_KIND):
            local_date = date.fromisoformat(record["content"]["local_date"])
            if start <= local_date <= end:
                sessions.append(self._project_app_session(record))
        return sessions

    def _get_garmin_session(self, session_id: str) -> dict[str, Any]:
        activities = self._read_optional_json(
            Path("derived/activities.json"), list
        )
        if activities is not None:
            for index, activity in enumerate(activities):
                if not isinstance(activity, dict):
                    raise DataIntegrityError(
                        f"Expected object at derived/activities.json item {index}"
                    )
                if (
                    f"{GARMIN_ID_PREFIX}{activity.get('activity_id')}"
                    == session_id
                ):
                    local_date = self._activity_date(activity, index)
                    if local_date is None:
                        raise DataIntegrityError(
                            "Garmin session is missing a start date"
                        )
                    return self._project_session(activity, local_date)
        raise NotFound(f"Training session {session_id} does not exist")

    def _replace_app_record(
        self,
        kind: _RecordKind,
        record_id: str,
        expected_revision: int,
        content: dict[str, Any],
    ) -> dict[str, Any]:
        self._guard_mutable(record_id)
        record = self._load_app_record(kind, record_id)
        self._check_revision(kind, record, expected_revision)
        updated = _bump_app_record(record, content)
        self._write_app_record(kind, record_id, updated, create=False)
        return updated

    def _delete_app_record(
        self, kind: _RecordKind, record_id: str, expected_revision: int
    ) -> dict[str, Any]:
        self._guard_mutable(record_id)
        record = self._load_app_record(kind, record_id)
        self._check_revision(kind, record, expected_revision)
        try:
            self._record_path(kind, record_id).unlink()
        except FileNotFoundError as exc:
            raise NotFound(
                f"{kind.noun.capitalize()} {record_id} does not exist"
            ) from exc
        except OSError as exc:
            raise StorageError(f"Could not delete {kind.noun}") from exc
        return {"id": record_id, "deleted": True, "revision": record["revision"]}

    def _write_app_record(
        self,
        kind: _RecordKind,
        record_id: str,
        record: dict[str, Any],
        *,
        create: bool,
    ) -> None:
        path = self._record_path(kind, record_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if create and path.exists():
                raise AlreadyExists(
                    f"{kind.noun.capitalize()} {record_id} already exists"
                )
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(record, handle, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except AlreadyExists:
            raise
        except OSError as exc:
            raise StorageError(f"Could not write {kind.noun}") from exc

    @staticmethod
    def _project_app_session(record: dict[str, Any]) -> dict[str, Any]:
        content = record["content"]
        return {
            "id": record["id"],
            "origin": record["origin"],
            "local_date": content["local_date"],
            "local_start": content.get("local_start"),
            "timing_precision": content["timing_precision"],
            "time_zone": content.get("time_zone"),
            "utc_offset": content.get("utc_offset"),
            "sport": content["sport"],
            "session_type": content.get("session_type"),
            "title": content.get("title"),
            "notes": content.get("notes"),
            "duration": content.get("duration"),
            "distance": content.get("distance"),
            "session_rpe": content.get("session_rpe"),
            "loads": content.get("loads"),
            "provenance": record["provenance"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "revision": record["revision"],
            "source_detail": None,
        }

    @staticmethod
    def _project_goal_event(record: dict[str, Any]) -> dict[str, Any]:
        content = record["content"]
        return {
            "id": record["id"],
            "origin": record["origin"],
            "local_date": content["local_date"],
            "local_start": content.get("local_start"),
            "timing_precision": content["timing_precision"],
            "time_zone": content.get("time_zone"),
            "utc_offset": content.get("utc_offset"),
            "sport": content["sport"],
            "name": content["name"],
            "priority": content["priority"],
            "status": content["status"],
            "distance": content.get("distance"),
            "goal": content.get("goal"),
            "outcome": content.get("outcome"),
            "notes": content.get("notes"),
            "provenance": record["provenance"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "revision": record["revision"],
        }


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidRecord(f"{field} must be a string")
    text = value.strip()
    return text or None


def _positive_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )


def _validate_duration(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidRecord("duration must be an object")
    unknown = set(value) - {"value", "unit", "basis"}
    if unknown:
        raise InvalidRecord(
            "unknown duration fields: " + ", ".join(sorted(unknown))
        )
    if not _positive_number(value.get("value")):
        raise InvalidRecord("duration.value must be a positive number")
    unit = value.get("unit", "seconds")
    if unit not in _DURATION_UNITS:
        raise InvalidRecord("duration.unit must be seconds")
    basis = value.get("basis")
    if basis not in _DURATION_BASES:
        raise InvalidRecord("duration.basis must be elapsed or active")
    return {"value": value["value"], "unit": unit, "basis": basis}


def _validate_distance(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidRecord("distance must be an object")
    unknown = set(value) - {"value", "unit"}
    if unknown:
        raise InvalidRecord(
            "unknown distance fields: " + ", ".join(sorted(unknown))
        )
    if not _positive_number(value.get("value")):
        raise InvalidRecord("distance.value must be a positive number")
    unit = value.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        raise InvalidRecord("distance.unit is required")
    return {"value": value["value"], "unit": unit.strip()}


def _validate_rpe(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
        raise InvalidRecord("session_rpe must be an integer from 1 to 10")
    return value


def _validate_loads(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise InvalidRecord("loads must be a list")
    loads: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise InvalidRecord(f"loads[{index}] must be an object")
        unknown = set(item) - _LOAD_FIELDS
        if unknown:
            raise InvalidRecord(
                f"loads[{index}] has unknown fields: "
                + ", ".join(sorted(unknown))
            )
        if not _positive_number(item.get("value")):
            raise InvalidRecord(
                f"loads[{index}].value must be a positive number"
            )
        normalized = {"value": item["value"]}
        for name in ("unit", "method", "source"):
            field_value = item.get(name)
            if not isinstance(field_value, str) or not field_value.strip():
                raise InvalidRecord(f"loads[{index}].{name} is required")
            normalized[name] = field_value.strip()
        loads.append(normalized)
    return loads or None


def _validate_local_timing(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate shared event-local timing into date/start/precision/zone."""
    local_date = payload.get("local_date")
    if not isinstance(local_date, str):
        raise InvalidRecord("local_date is required")
    try:
        parsed_date = date.fromisoformat(local_date)
    except ValueError as exc:
        raise InvalidRecord("local_date must be an ISO calendar date") from exc

    timing: dict[str, Any] = {"local_date": parsed_date.isoformat()}
    local_start = payload.get("local_start")
    if local_start is None:
        timing["local_start"] = None
        timing["timing_precision"] = "date_only"
    else:
        if not isinstance(local_start, str) or not local_start.strip():
            raise InvalidRecord("local_start must include a local date and time")
        local_start = local_start.strip()
        if "T" not in local_start and " " not in local_start:
            raise InvalidRecord("local_start must include a local date and time")
        try:
            parsed_start = datetime.fromisoformat(local_start)
        except ValueError as exc:
            raise InvalidRecord(
                "local_start must be an ISO local date and time"
            ) from exc
        if parsed_start.date() != parsed_date:
            raise InvalidRecord("local_start date must match local_date")
        timing["local_start"] = local_start
        timing["timing_precision"] = "local_datetime"

    timing["time_zone"] = _optional_text(payload.get("time_zone"), "time_zone")
    timing["utc_offset"] = _optional_text(payload.get("utc_offset"), "utc_offset")
    return timing


def _validate_session_content(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidRecord("session content must be an object")
    unknown = set(payload) - _SESSION_CONTENT_FIELDS
    if unknown:
        raise InvalidRecord(
            "unknown session fields: " + ", ".join(sorted(unknown))
        )

    sport = payload.get("sport")
    if not isinstance(sport, str) or not sport.strip():
        raise InvalidRecord("sport is required")

    content: dict[str, Any] = {
        "sport": sport.strip(),
        "session_type": _optional_text(payload.get("session_type"), "session_type"),
    }
    content.update(_validate_local_timing(payload))
    content["title"] = _optional_text(payload.get("title"), "title")
    content["notes"] = _optional_text(payload.get("notes"), "notes")
    content["duration"] = _validate_duration(payload.get("duration"))
    content["distance"] = _validate_distance(payload.get("distance"))
    content["session_rpe"] = _validate_rpe(payload.get("session_rpe"))
    content["loads"] = _validate_loads(payload.get("loads"))
    return content


def _validate_target_duration(value: Any, field: str) -> dict[str, Any] | None:
    """Validate a target/actual finish time as ``{value, unit: "seconds"}``."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidRecord(f"{field} must be an object")
    unknown = set(value) - {"value", "unit"}
    if unknown:
        raise InvalidRecord(
            f"unknown {field} fields: " + ", ".join(sorted(unknown))
        )
    if not _positive_number(value.get("value")):
        raise InvalidRecord(f"{field}.value must be a positive number")
    unit = value.get("unit", "seconds")
    if not isinstance(unit, str) or unit not in _DURATION_UNITS:
        raise InvalidRecord(f"{field}.unit must be seconds")
    return {"value": value["value"], "unit": unit}


def _validate_goal(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidRecord("goal must be an object")
    unknown = set(value) - _GOAL_FIELDS
    if unknown:
        raise InvalidRecord("unknown goal fields: " + ", ".join(sorted(unknown)))
    target = _validate_target_duration(
        value.get("target_duration"), "goal.target_duration"
    )
    statement = _optional_text(value.get("statement"), "goal.statement")
    if target is None and statement is None:
        return None
    return {"target_duration": target, "statement": statement}


def _validate_outcome(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidRecord("outcome must be an object")
    unknown = set(value) - _OUTCOME_FIELDS
    if unknown:
        raise InvalidRecord(
            "unknown outcome fields: " + ", ".join(sorted(unknown))
        )
    actual = _validate_target_duration(
        value.get("actual_duration"), "outcome.actual_duration"
    )
    statement = _optional_text(value.get("statement"), "outcome.statement")
    if actual is None and statement is None:
        return None
    return {"actual_duration": actual, "statement": statement}


def _validate_goal_event_content(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidRecord("goal event content must be an object")
    unknown = set(payload) - _GOAL_EVENT_CONTENT_FIELDS
    if unknown:
        raise InvalidRecord(
            "unknown goal event fields: " + ", ".join(sorted(unknown))
        )

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InvalidRecord("name is required")
    sport = payload.get("sport")
    if not isinstance(sport, str) or not sport.strip():
        raise InvalidRecord("sport is required")
    priority = payload.get("priority")
    if not isinstance(priority, str) or priority not in _GOAL_EVENT_PRIORITIES:
        raise InvalidRecord("priority must be primary, secondary, or practice")
    status = payload.get("status", "scheduled")
    if not isinstance(status, str) or status not in _GOAL_EVENT_STATUSES:
        raise InvalidRecord("status must be scheduled, completed, or cancelled")

    content: dict[str, Any] = {
        "name": name.strip(),
        "sport": sport.strip(),
        "priority": priority,
        "status": status,
    }
    content.update(_validate_local_timing(payload))
    content["distance"] = _validate_distance(payload.get("distance"))
    content["goal"] = _validate_goal(payload.get("goal"))
    content["outcome"] = _validate_outcome(payload.get("outcome"))
    content["notes"] = _optional_text(payload.get("notes"), "notes")
    return content


def _goal_event_sort_key(event: dict[str, Any]) -> tuple[datetime, str]:
    """Order events by real local timing, not the raw separator character."""
    local_start = event.get("local_start")
    if local_start:
        moment = datetime.fromisoformat(local_start)
    else:
        moment = datetime.combine(
            date.fromisoformat(event["local_date"]), datetime.min.time()
        )
    return moment, event["id"]


def _validate_reason(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRecord("reason is required")
    return value.strip()


def _validate_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRecord("name is required")
    return value.strip()


def _validate_plan_date(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidRecord(f"{field} is required")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise InvalidRecord(f"{field} must be an ISO calendar date") from exc


def _validate_positive_measure(value: Any, field: str) -> Any:
    if value is None:
        return None
    if not _positive_number(value):
        raise InvalidRecord(f"{field} must be a positive number")
    return value


def _validate_constraints(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidRecord("constraints must be a list")
    constraints: list[str] = []
    for index, item in enumerate(value):
        text = _optional_text(item, f"constraints[{index}]")
        if text is None:
            raise InvalidRecord(
                f"constraints[{index}] must be a nonblank statement"
            )
        constraints.append(text)
    return constraints


def _validate_goal_refs(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidRecord("goal_events must be a list")
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise InvalidRecord(f"goal_events[{index}] must be an object")
        unknown = set(item) - _GOAL_REF_FIELDS
        if unknown:
            raise InvalidRecord(
                f"goal_events[{index}] has unknown fields: "
                + ", ".join(sorted(unknown))
            )
        goal_event_id = item.get("goal_event_id")
        if not isinstance(goal_event_id, str) or not _APP_ID_RE.match(goal_event_id):
            raise InvalidRecord(
                f"goal_events[{index}].goal_event_id must be an app record id"
            )
        revision = item.get("goal_event_revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise InvalidRecord(
                f"goal_events[{index}].goal_event_revision must be a positive integer"
            )
        if goal_event_id in seen:
            raise InvalidRecord(
                f"goal_events references {goal_event_id} more than once"
            )
        seen.add(goal_event_id)
        refs.append(
            {"goal_event_id": goal_event_id, "goal_event_revision": revision}
        )
    return refs


def _validate_planned_session_prescription(
    payload: Any, starts_on: str, ends_on: str, *, label: str
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidRecord(f"{label} must be an object")
    unknown = set(payload) - _PLANNED_SESSION_INPUT_FIELDS
    if unknown:
        raise InvalidRecord(
            f"{label} has unknown fields: " + ", ".join(sorted(unknown))
        )
    scheduled_date = _validate_plan_date(
        payload.get("scheduled_date"), f"{label}.scheduled_date"
    )
    if not starts_on <= scheduled_date <= ends_on:
        raise InvalidRecord(
            f"{label}.scheduled_date must fall within the plan range"
        )
    sport = payload.get("sport")
    if not isinstance(sport, str) or not sport.strip():
        raise InvalidRecord(f"{label}.sport is required")
    prescription = payload.get("prescription")
    if not isinstance(prescription, str) or not prescription.strip():
        raise InvalidRecord(f"{label}.prescription is required")
    return {
        "scheduled_date": scheduled_date,
        "sport": sport.strip(),
        "session_type": _optional_text(
            payload.get("session_type"), f"{label}.session_type"
        ),
        "prescription": prescription.strip(),
        "target_duration_seconds": _validate_positive_measure(
            payload.get("target_duration_seconds"),
            f"{label}.target_duration_seconds",
        ),
        "target_distance_meters": _validate_positive_measure(
            payload.get("target_distance_meters"),
            f"{label}.target_distance_meters",
        ),
        "effort_guidance": _optional_text(
            payload.get("effort_guidance"), f"{label}.effort_guidance"
        ),
    }


def _planned_session(
    session_id: str,
    prescription: dict[str, Any],
    disposition: str,
    matches: list[str],
    fulfilment_note: str | None,
) -> dict[str, Any]:
    return {
        "id": session_id,
        "scheduled_date": prescription["scheduled_date"],
        "sport": prescription["sport"],
        "session_type": prescription["session_type"],
        "prescription": prescription["prescription"],
        "target_duration_seconds": prescription["target_duration_seconds"],
        "target_distance_meters": prescription["target_distance_meters"],
        "effort_guidance": prescription["effort_guidance"],
        "disposition": disposition,
        "matches": list(matches),
        "fulfilment_note": fulfilment_note,
    }


def _plan_snapshot(
    name: str,
    starts_on: str,
    ends_on: str,
    status: str,
    goal_events: list[dict[str, Any]],
    constraints: list[str],
    planned_sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "name": name,
        "starts_on": starts_on,
        "ends_on": ends_on,
        "status": status,
        "goal_events": [dict(ref) for ref in goal_events],
        "constraints": list(constraints),
        "planned_sessions": [dict(ps) for ps in planned_sessions],
    }


def _plan_revision_entry(
    revision: int,
    kind: str,
    reason: str,
    effective_from: str | None,
    plan: dict[str, Any],
    recorded_date: str | None = None,
) -> dict[str, Any]:
    return {
        "revision": revision,
        "kind": kind,
        "recorded_at": _utc_now(),
        "recorded_date": recorded_date or date.today().isoformat(),
        "reason": reason,
        "effective_from": effective_from,
        "plan": plan,
    }


def _append_plan_revision(
    record: dict[str, Any], entry: dict[str, Any]
) -> dict[str, Any]:
    """Append an immutable Plan Revision and advance the current revision."""
    revisions = record["content"]["revisions"] + [entry]
    return _bump_app_record(record, {"revisions": revisions})


def _normalize_matches(value: Any, disposition: str) -> list[str]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise InvalidRecord("matches must be a list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InvalidRecord("each match must be a training session id")
        normalized.append(item.strip())
    if len(set(normalized)) != len(normalized):
        raise InvalidRecord("matches contains duplicate training session ids")
    if disposition == "fulfilled" and not normalized:
        raise InvalidRecord(
            "fulfilled requires at least one matched training session"
        )
    if disposition != "fulfilled" and normalized:
        raise InvalidRecord("only a fulfilled planned session may carry matches")
    return normalized


_PLAN_SNAPSHOT_KEYS = {
    "name",
    "starts_on",
    "ends_on",
    "status",
    "goal_events",
    "constraints",
    "planned_sessions",
}
_PLANNED_SESSION_STORED_KEYS = {
    "id",
    "scheduled_date",
    "sport",
    "session_type",
    "prescription",
    "target_duration_seconds",
    "target_distance_meters",
    "effort_guidance",
    "disposition",
    "matches",
    "fulfilment_note",
}
_PLAN_REVISION_ENTRY_KEYS = {
    "revision",
    "kind",
    "recorded_at",
    "recorded_date",
    "reason",
    "effective_from",
    "plan",
}


def _is_iso_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_optional_str(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _is_optional_number(value: Any) -> bool:
    return value is None or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    )


def _coerce_planned_session(planned: Any, seen: set[str]) -> None:
    disposition = planned.get("disposition") if isinstance(planned, dict) else None
    if (
        not isinstance(planned, dict)
        or set(planned) != _PLANNED_SESSION_STORED_KEYS
        or not isinstance(planned.get("id"), str)
        or planned["id"] in seen
        or not _is_iso_date(planned.get("scheduled_date"))
        or not isinstance(planned.get("sport"), str)
        or not isinstance(disposition, str)
        or disposition not in _PLANNED_SESSION_DISPOSITIONS
        or not isinstance(planned.get("prescription"), str)
        or not isinstance(planned.get("matches"), list)
        or not all(isinstance(match, str) for match in planned["matches"])
        or not _is_optional_str(planned.get("session_type"))
        or not _is_optional_str(planned.get("effort_guidance"))
        or not _is_optional_str(planned.get("fulfilment_note"))
        or not _is_optional_number(planned.get("target_duration_seconds"))
        or not _is_optional_number(planned.get("target_distance_meters"))
    ):
        raise DataIntegrityError("Stored training plan record is malformed")
    seen.add(planned["id"])


def _coerce_plan_snapshot(plan: dict[str, Any]) -> None:
    status = plan.get("status")
    if (
        set(plan) != _PLAN_SNAPSHOT_KEYS
        or not isinstance(plan.get("name"), str)
        or not _is_iso_date(plan.get("starts_on"))
        or not _is_iso_date(plan.get("ends_on"))
        or not isinstance(status, str)
        or status not in _PLAN_STATUSES
        or not isinstance(plan.get("goal_events"), list)
        or not isinstance(plan.get("constraints"), list)
        or not all(isinstance(text, str) for text in plan["constraints"])
        or not isinstance(plan.get("planned_sessions"), list)
    ):
        raise DataIntegrityError("Stored training plan record is malformed")
    for ref in plan["goal_events"]:
        revision = ref.get("goal_event_revision") if isinstance(ref, dict) else None
        if (
            not isinstance(ref, dict)
            or set(ref) != _GOAL_REF_FIELDS
            or not isinstance(ref.get("goal_event_id"), str)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
        ):
            raise DataIntegrityError("Stored training plan record is malformed")
    seen: set[str] = set()
    for planned in plan["planned_sessions"]:
        _coerce_planned_session(planned, seen)


def _coerce_plan_record(
    record: dict[str, Any], expected_id: str
) -> dict[str, Any]:
    revision = record.get("revision")
    content = record.get("content")
    if (
        record.get("id") != expected_id
        or record.get("origin") != "app_record"
        or not isinstance(record.get("provenance"), dict)
        or not isinstance(record.get("created_at"), str)
        or not isinstance(record.get("updated_at"), str)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(content, dict)
        or set(content) != {"revisions"}
        or not isinstance(content.get("revisions"), list)
        or len(content["revisions"]) != revision
    ):
        raise DataIntegrityError("Stored training plan record is malformed")
    for index, entry in enumerate(content["revisions"]):
        plan = entry.get("plan") if isinstance(entry, dict) else None
        kind = entry.get("kind") if isinstance(entry, dict) else None
        effective_from = (
            entry.get("effective_from") if isinstance(entry, dict) else None
        )
        if (
            not isinstance(entry, dict)
            or set(entry) != _PLAN_REVISION_ENTRY_KEYS
            or entry.get("revision") != index + 1
            or not isinstance(kind, str)
            or kind not in _PLAN_REVISION_KINDS
            or not isinstance(entry.get("reason"), str)
            or not isinstance(entry.get("recorded_at"), str)
            or not isinstance(entry.get("recorded_date"), str)
            or not _is_optional_str(effective_from)
            or not isinstance(plan, dict)
        ):
            raise DataIntegrityError("Stored training plan record is malformed")
        _coerce_plan_snapshot(plan)
    return record


def _new_app_record(content: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build a fresh App Record envelope with a generated opaque id."""
    now = _utc_now()
    record_id = APP_ID_PREFIX + uuid.uuid4().hex
    record = {
        "id": record_id,
        "origin": "app_record",
        "provenance": {"source": "manual"},
        "created_at": now,
        "updated_at": now,
        "revision": 1,
        "content": content,
    }
    return record_id, record


def _bump_app_record(
    record: dict[str, Any], content: dict[str, Any]
) -> dict[str, Any]:
    """Advance an App Record to the next revision with new content."""
    return {
        "id": record["id"],
        "origin": record["origin"],
        "provenance": record["provenance"],
        "created_at": record["created_at"],
        "updated_at": _utc_now(),
        "revision": record["revision"] + 1,
        "content": content,
    }


def _coerce_envelope(
    record: dict[str, Any], expected_id: str, noun: str
) -> dict[str, Any]:
    """Validate the shared App Record envelope and return its content."""
    revision = record.get("revision")
    content = record.get("content")
    if (
        record.get("id") != expected_id
        or record.get("origin") != "app_record"
        or not isinstance(record.get("provenance"), dict)
        or not isinstance(record.get("created_at"), str)
        or not isinstance(record.get("updated_at"), str)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(content, dict)
        or content.get("timing_precision") not in _TIMING_PRECISIONS
    ):
        raise DataIntegrityError(f"Stored {noun} record is malformed")
    return content


def _coerce_content(
    record: dict[str, Any],
    expected_id: str,
    noun: str,
    validator: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    """Confirm stored content is exactly what validation would produce.

    A well-formed App Record always persists the validator's normalized output,
    so any drift (a missing defaulted field, a stale ``timing_precision``, an
    un-normalized value) means the stored record no longer satisfies the stable
    input contract and is reported rather than silently projected.
    """
    content = _coerce_envelope(record, expected_id, noun)
    try:
        normalized = validator(
            {k: v for k, v in content.items() if k != "timing_precision"}
        )
    except InvalidRecord as exc:
        raise DataIntegrityError(f"Stored {noun} record is malformed") from exc
    if normalized != content:
        raise DataIntegrityError(f"Stored {noun} record is malformed")
    return record


def _coerce_session_record(
    record: dict[str, Any], expected_id: str
) -> dict[str, Any]:
    return _coerce_content(
        record, expected_id, "training session", _validate_session_content
    )


def _coerce_goal_event_record(
    record: dict[str, Any], expected_id: str
) -> dict[str, Any]:
    return _coerce_content(
        record, expected_id, "goal event", _validate_goal_event_content
    )


@dataclass(frozen=True)
class _RecordKind:
    """One App Record family sharing the file-store convention."""

    subdir: str
    noun: str
    coerce: Callable[[dict[str, Any], str], dict[str, Any]]


_SESSION_KIND = _RecordKind("sessions", "training session", _coerce_session_record)
_GOAL_EVENT_KIND = _RecordKind("goal_events", "goal event", _coerce_goal_event_record)
_PLAN_KIND = _RecordKind("plans", "training plan", _coerce_plan_record)
