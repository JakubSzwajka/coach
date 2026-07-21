"""Private transactional reads over authoritative PostgreSQL records.

These projections are rebuilt on demand from typed authority. Composite reads
use one repeatable-read, read-only transaction so adapters never observe a
partially advanced ingestion graph or Plan Revision.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterator, Mapping, Sequence
from uuid import UUID

import psycopg

from ._app_record_store import AppRecordStore
from ._capture_store import CaptureStoreError
from .config import DatabaseSettings

_MAX_WINDOW_DAYS = 90


class ReadProjectionStore:
    """Build stable, profile-scoped consumer projections from authority."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings

    def dashboard(
        self, issuer: str, subject: str, starts_on: date, ends_on: date
    ) -> dict[str, Any] | None:
        starts_on, ends_on = _window(starts_on, ends_on)
        with self._snapshot(issuer, subject) as snapshot:
            if snapshot is None:
                return None
            connection, profile = snapshot
            observations = self._observations(
                connection, profile["id"], starts_on, ends_on, None
            )
            latest: dict[str, dict[str, Any]] = {}
            for observation in observations:
                previous = latest.get(observation["definition"])
                if previous is None or _observation_sort_key(previous) < _observation_sort_key(
                    observation
                ):
                    latest[observation["definition"]] = observation
            sessions = self._sessions(
                connection, profile["id"], starts_on, ends_on, None
            )
            plans = self._plans(connection, profile["id"], None, None, None)
            goals = self._goals(
                connection, profile["id"], starts_on, ends_on, None, None
            )
            return {
                "starts_on": starts_on,
                "ends_on": ends_on,
                "display_name": profile["display_name"],
                "latest_observations": tuple(
                    latest[key] for key in sorted(latest)
                ),
                "recent_sessions": tuple(sessions[:5]),
                "active_plan": next(
                    (plan for plan in plans if plan["status"] == "active"), None
                ),
                "goal_events": tuple(goals),
                "collection_health": self._collection_health(
                    connection, profile["id"]
                ),
            }

    def trends(
        self,
        issuer: str,
        subject: str,
        starts_on: date,
        ends_on: date,
        definitions: Sequence[str] | None,
    ) -> dict[str, Any] | None:
        starts_on, ends_on = _window(starts_on, ends_on)
        definitions = _definitions(definitions)
        with self._snapshot(issuer, subject) as snapshot:
            if snapshot is None:
                return None
            connection, profile = snapshot
            observations = self._observations(
                connection, profile["id"], starts_on, ends_on, definitions
            )
            return {
                "starts_on": starts_on,
                "ends_on": ends_on,
                "series": _series(observations),
            }

    def coaching_context(
        self, issuer: str, subject: str, starts_on: date, ends_on: date
    ) -> dict[str, Any] | None:
        starts_on, ends_on = _window(starts_on, ends_on)
        with self._snapshot(issuer, subject) as snapshot:
            if snapshot is None:
                return None
            connection, profile = snapshot
            observations = self._observations(
                connection, profile["id"], starts_on, ends_on, None
            )
            sessions = self._sessions(
                connection, profile["id"], starts_on, ends_on, None
            )
            plans = self._plans(
                connection, profile["id"], None, starts_on, ends_on
            )
            goals = self._goals(
                connection, profile["id"], starts_on, ends_on, None, None
            )
            calendar = self._calendar(
                connection, profile["id"], starts_on, ends_on
            )
            return {
                "starts_on": starts_on,
                "ends_on": ends_on,
                "observations": observations,
                "sessions": tuple(sessions),
                "plans": tuple(plans),
                "goal_events": tuple(goals),
                "calendar": calendar,
                "collection_health": self._collection_health(
                    connection, profile["id"]
                ),
            }

    def collection_health(
        self, issuer: str, subject: str
    ) -> dict[str, Any] | None:
        with self._snapshot(issuer, subject) as snapshot:
            if snapshot is None:
                return None
            connection, profile = snapshot
            return self._collection_health(connection, profile["id"])

    def unified_calendar(
        self, issuer: str, subject: str, starts_on: date, ends_on: date
    ) -> dict[str, Any] | None:
        starts_on, ends_on = _window(starts_on, ends_on)
        with self._snapshot(issuer, subject) as snapshot:
            if snapshot is None:
                return None
            connection, profile = snapshot
            return {
                "starts_on": starts_on,
                "ends_on": ends_on,
                "items": self._calendar(
                    connection, profile["id"], starts_on, ends_on
                ),
            }

    @contextmanager
    def _snapshot(
        self, issuer: str, subject: str
    ) -> Iterator[tuple[psycopg.Connection, Mapping[str, Any]] | None]:
        issuer = _required_text(issuer, "issuer")
        subject = _required_text(subject, "subject")
        with psycopg.connect(self._settings.url) as connection:
            with connection.transaction():
                connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
                row = connection.execute(
                    """
                    SELECT id, display_name FROM profiles
                    WHERE clerk_issuer = %s AND clerk_subject = %s
                    """,
                    (issuer, subject),
                ).fetchone()
                if row is None:
                    yield None
                else:
                    yield connection, {"id": row[0], "display_name": row[1]}

    @staticmethod
    def _sessions(
        connection: psycopg.Connection,
        profile_id: UUID,
        starts_on: date,
        ends_on: date,
        sport: str | None,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT id, ownership, collected_record_id
            FROM training_sessions
            WHERE profile_id = %s AND local_date BETWEEN %s AND %s
              AND (%s::text IS NULL OR sport = %s)
            ORDER BY COALESCE(local_start, local_date::timestamp) DESC, id
            """,
            (profile_id, starts_on, ends_on, sport, sport),
        ).fetchall()
        sessions: list[dict[str, Any]] = []
        for identifier, ownership, record_id in rows:
            record = AppRecordStore._training_session(
                connection, profile_id, identifier, ownership
            )
            record["source_identity"] = (
                record["id"]
                if ownership == "app"
                else f"record:{record_id.hex}"
            )
            annotation = connection.execute(
                """
                SELECT id, revision, notes, reliability, duplicate_flag
                FROM session_annotations
                WHERE profile_id = %s AND training_session_id = %s
                """,
                (profile_id, identifier),
            ).fetchone()
            record["annotation"] = (
                {
                    "id": f"app:{annotation[0].hex}",
                    "revision": annotation[1],
                    "notes": annotation[2],
                    "reliability": annotation[3],
                    "duplicate": annotation[4],
                }
                if annotation is not None
                else None
            )
            sessions.append(record)
        return sessions

    @staticmethod
    def _observations(
        connection: psycopg.Connection,
        profile_id: UUID,
        starts_on: date,
        ends_on: date,
        definitions: tuple[str, ...] | None,
    ) -> tuple[dict[str, Any], ...]:
        rows = connection.execute(
            """
            SELECT o.definition_key, o.status, o.value_type, o.unit,
                   o.window_kind, o.method, o.local_date, o.observed_at,
                   o.window_start, o.window_end, o.decimal_value,
                   o.integer_value, o.text_value, o.boolean_value,
                   o.collected_record_id
            FROM controlled_observations o
            WHERE o.profile_id = %s
              AND (
                  (o.window_kind = 'calendar_day'
                   AND o.local_date BETWEEN %s AND %s)
                  OR (o.window_kind = 'instant'
                      AND (o.observed_at AT TIME ZONE 'UTC')::date BETWEEN %s AND %s)
                  OR (o.window_kind = 'interval'
                      AND (o.window_end AT TIME ZONE 'UTC')::date >= %s
                      AND (o.window_start AT TIME ZONE 'UTC')::date <= %s)
              )
              AND (%s::text[] IS NULL OR o.definition_key = ANY(%s))
            ORDER BY o.definition_key,
                     COALESCE(o.local_date::timestamp,
                              o.observed_at AT TIME ZONE 'UTC',
                              o.window_end AT TIME ZONE 'UTC'),
                     o.collected_record_id
            """,
            (
                profile_id,
                starts_on,
                ends_on,
                starts_on,
                ends_on,
                starts_on,
                ends_on,
                list(definitions) if definitions is not None else None,
                list(definitions) if definitions is not None else None,
            ),
        ).fetchall()
        result = []
        for row in rows:
            values = {
                "decimal": row[10],
                "integer": row[11],
                "text": row[12],
                "boolean": row[13],
            }
            result.append(
                {
                    "definition": row[0],
                    "status": row[1],
                    "value": None if row[1] == "missing" else _number(values[row[2]]),
                    "value_type": row[2],
                    "unit": row[3],
                    "window_kind": row[4],
                    "method": row[5],
                    "local_date": row[6],
                    "observed_at": row[7],
                    "window_start": row[8],
                    "window_end": row[9],
                    "source_identity": f"record:{row[14].hex}",
                }
            )
        return tuple(result)

    @staticmethod
    def _plans(
        connection: psycopg.Connection,
        profile_id: UUID,
        status: str | None,
        starts_on: date | None,
        ends_on: date | None,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT p.id, p.current_revision, p.created_at, p.updated_at
            FROM training_plans p
            JOIN plan_revisions r
              ON r.profile_id = p.profile_id AND r.plan_id = p.id
             AND r.revision = p.current_revision
            WHERE p.profile_id = %s
              AND (%s::text IS NULL OR p.status = %s)
              AND (%s::date IS NULL OR r.ends_on >= %s)
              AND (%s::date IS NULL OR r.starts_on <= %s)
            ORDER BY r.starts_on, p.id
            """,
            (
                profile_id,
                status,
                status,
                starts_on,
                starts_on,
                ends_on,
                ends_on,
            ),
        ).fetchall()
        plans = []
        for identifier, revision, created_at, updated_at in rows:
            snapshot = AppRecordStore._snapshot(
                connection, profile_id, identifier, revision
            )
            goal_events = AppRecordStore._stale_goal_refs(
                connection, profile_id, snapshot["goal_events"]
            )
            plans.append(
                {
                    "id": f"app:{identifier.hex}",
                    "origin": "app_record",
                    "name": snapshot["name"],
                    "starts_on": snapshot["starts_on"],
                    "ends_on": snapshot["ends_on"],
                    "status": snapshot["status"],
                    "goal_events": goal_events,
                    "constraints": snapshot["constraints"],
                    "planned_sessions": sorted(
                        snapshot["planned_sessions"],
                        key=lambda item: (item["scheduled_date"], item["id"]),
                    ),
                    "requires_review": any(item["stale"] for item in goal_events),
                    "provenance": {"source": "manual"},
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "revision": revision,
                }
            )
        return plans

    @staticmethod
    def _goals(
        connection: psycopg.Connection,
        profile_id: UUID,
        starts_on: date | None,
        ends_on: date | None,
        sport: str | None,
        status: str | None,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT id FROM goal_events
            WHERE profile_id = %s
              AND (%s::date IS NULL OR local_date >= %s)
              AND (%s::date IS NULL OR local_date <= %s)
              AND (%s::text IS NULL OR sport = %s)
              AND (%s::text IS NULL OR status = %s)
            ORDER BY COALESCE(local_start, local_date::timestamp), id
            """,
            (
                profile_id,
                starts_on,
                starts_on,
                ends_on,
                ends_on,
                sport,
                sport,
                status,
                status,
            ),
        ).fetchall()
        return [
            AppRecordStore._goal_event(connection, profile_id, row[0])
            for row in rows
        ]

    def _calendar(
        self,
        connection: psycopg.Connection,
        profile_id: UUID,
        starts_on: date,
        ends_on: date,
    ) -> tuple[dict[str, Any], ...]:
        sessions = self._sessions(connection, profile_id, starts_on, ends_on, None)
        plans = self._plans(connection, profile_id, None, None, None)
        goals = self._goals(
            connection, profile_id, starts_on, ends_on, None, None
        )
        items: list[dict[str, Any]] = []

        match_index: dict[str, list[dict[str, Any]]] = {}
        for plan in plans:
            for planned in plan["planned_sessions"]:
                for session_id in planned["matches"]:
                    match_index.setdefault(session_id, []).append(
                        {
                            "plan_id": plan["id"],
                            "plan_name": plan["name"],
                            "plan_status": plan["status"],
                            "plan_revision": plan["revision"],
                            "planned_session_id": planned["id"],
                        }
                    )
                scheduled = date.fromisoformat(planned["scheduled_date"])
                if starts_on <= scheduled <= ends_on:
                    match_summaries = []
                    for session_id in planned["matches"]:
                        identifier, ownership = _decode_session_id(session_id)
                        row = connection.execute(
                            """
                            SELECT local_date, sport FROM training_sessions
                            WHERE profile_id = %s AND id = %s AND ownership = %s
                            """,
                            (profile_id, identifier, ownership),
                        ).fetchone()
                        if row is None:
                            continue
                        match_summaries.append(
                            {
                                "training_session_id": session_id,
                                "ownership": ownership,
                                "local_date": row[0],
                                "sport": row[1],
                            }
                        )
                    items.append(
                        {
                            "kind": "planned_session",
                            "id": planned["id"],
                            "local_date": scheduled,
                            "plan_id": plan["id"],
                            "plan_name": plan["name"],
                            "plan_status": plan["status"],
                            "plan_revision": plan["revision"],
                            "plan_requires_review": plan["requires_review"],
                            "sport": planned["sport"],
                            "session_type": planned["session_type"],
                            "prescription": planned["prescription"],
                            "target_duration_seconds": planned[
                                "target_duration_seconds"
                            ],
                            "target_distance_meters": planned[
                                "target_distance_meters"
                            ],
                            "effort_guidance": planned["effort_guidance"],
                            "disposition": planned["disposition"],
                            "fulfilment_note": planned["fulfilment_note"],
                            "matches": tuple(match_summaries),
                        }
                    )

        for session in sessions:
            content = {
                key: value
                for key, value in session.items()
                if key
                not in {
                    "id",
                    "origin",
                    "ownership",
                    "provenance",
                    "created_at",
                    "updated_at",
                    "revision",
                    "source_identity",
                    "annotation",
                    "source_detail",
                }
            }
            items.append(
                {
                    "kind": "training_session",
                    "id": session["id"],
                    "local_date": date.fromisoformat(session["local_date"]),
                    "local_start": session["local_start"],
                    "timing_precision": session["timing_precision"],
                    "origin": session["origin"],
                    "ownership": session["ownership"],
                    "source_identity": session["source_identity"],
                    "content": content,
                    "annotation": session["annotation"],
                    "plan_matches": tuple(match_index.get(session["id"], ())),
                    "revision": session["revision"],
                }
            )

        goal_references: dict[str, list[dict[str, Any]]] = {}
        for plan in plans:
            for ref in plan["goal_events"]:
                goal_references.setdefault(ref["goal_event_id"], []).append(
                    {
                        "plan_id": plan["id"],
                        "plan_name": plan["name"],
                        "plan_status": plan["status"],
                        "plan_revision": plan["revision"],
                        "referenced_revision": ref["goal_event_revision"],
                        "stale": ref["stale"],
                        "stale_reason": ref["stale_reason"],
                    }
                )
        for goal in goals:
            references = tuple(goal_references.get(goal["id"], ()))
            items.append(
                {
                    "kind": "goal_event",
                    "id": goal["id"],
                    "local_date": date.fromisoformat(goal["local_date"]),
                    "local_start": goal["local_start"],
                    "timing_precision": goal["timing_precision"],
                    "sport": goal["sport"],
                    "name": goal["name"],
                    "priority": goal["priority"],
                    "status": goal["status"],
                    "distance": goal["distance"],
                    "goal": goal["goal"],
                    "outcome": goal["outcome"],
                    "notes": goal["notes"],
                    "revision": goal["revision"],
                    "requires_review": any(ref["stale"] for ref in references),
                    "plan_references": references,
                }
            )
        items.sort(key=_calendar_sort_key)
        return tuple(items)

    @staticmethod
    def _collection_health(
        connection: psycopg.Connection, profile_id: UUID
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT sc.id, sc.provider, sc.state, domains.domain,
                   h.outcome, h.safe_code, h.attempted_at, h.finished_at,
                   h.last_success_at, cp.revision
            FROM source_connections sc
            CROSS JOIN (VALUES ('initial_sync'), ('incremental')) AS domains(domain)
            LEFT JOIN collection_health h
              ON h.profile_id = sc.profile_id
             AND h.source_connection_id = sc.id
             AND h.domain = domains.domain
            LEFT JOIN collection_checkpoints cp
              ON cp.profile_id = sc.profile_id
             AND cp.source_connection_id = sc.id
             AND cp.domain = domains.domain
            WHERE sc.profile_id = %s
            ORDER BY sc.provider, sc.id, domains.domain
            """,
            (profile_id,),
        ).fetchall()
        grouped: dict[UUID, dict[str, Any]] = {}
        for row in rows:
            source = grouped.setdefault(
                row[0],
                {
                    "source_identity": f"source:{row[0].hex}",
                    "provider": row[1],
                    "state": row[2],
                    "domains": [],
                },
            )
            source["domains"].append(
                {
                    "domain": row[3],
                    "outcome": row[4] or "not_attempted",
                    "safe_code": row[5],
                    "attempted_at": row[6],
                    "finished_at": row[7],
                    "last_success_at": row[8],
                    "checkpoint_revision": row[9],
                }
            )
        summary = connection.execute(
            """
            SELECT
              (SELECT count(*) FROM collected_records WHERE profile_id = %s),
              (SELECT count(*) FROM collected_record_captures WHERE profile_id = %s),
              (SELECT max(collected_at) FROM collected_record_captures WHERE profile_id = %s),
              (SELECT max(local_date) FROM training_sessions WHERE profile_id = %s),
              (SELECT max(COALESCE(local_date,
                         (observed_at AT TIME ZONE 'UTC')::date,
                         (window_end AT TIME ZONE 'UTC')::date))
                 FROM controlled_observations WHERE profile_id = %s)
            """,
            (profile_id, profile_id, profile_id, profile_id, profile_id),
        ).fetchone()
        return {
            "records": summary[0],
            "captures": summary[1],
            "latest_collected_at": summary[2],
            "latest_training_session_date": summary[3],
            "latest_observation_date": summary[4],
            "sources": tuple(
                {
                    **source,
                    "domains": tuple(source["domains"]),
                }
                for source in grouped.values()
            ),
        }


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CaptureStoreError(f"{name} is required")
    return value.strip()


def _window(starts_on: date, ends_on: date) -> tuple[date, date]:
    if not isinstance(starts_on, date) or isinstance(starts_on, datetime):
        raise CaptureStoreError("starts_on must be a date")
    if not isinstance(ends_on, date) or isinstance(ends_on, datetime):
        raise CaptureStoreError("ends_on must be a date")
    if starts_on > ends_on or (ends_on - starts_on).days >= _MAX_WINDOW_DAYS:
        raise CaptureStoreError("projection window must contain 1 to 90 days")
    return starts_on, ends_on


def _definitions(value: Sequence[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CaptureStoreError("definitions must be a sequence or null")
    result = []
    for definition in value:
        if not isinstance(definition, str) or not definition.strip():
            raise CaptureStoreError("definitions must contain nonblank text")
        normalized = definition.strip()
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _number(value: Any) -> Any:
    if not isinstance(value, Decimal):
        return value
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _observation_sort_key(item: Mapping[str, Any]) -> tuple[datetime, str]:
    if item["local_date"] is not None:
        moment = datetime.combine(item["local_date"], datetime.min.time()).replace(
            tzinfo=timezone.utc
        )
    else:
        moment = item["observed_at"] or item["window_end"]
    return moment, item["source_identity"]


def _series(observations: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    grouped: dict[tuple[str, str, str, str, str], list[Mapping[str, Any]]] = {}
    for item in observations:
        key = (
            item["definition"],
            item["value_type"],
            item["unit"],
            item["window_kind"],
            item["method"],
        )
        grouped.setdefault(key, []).append(item)
    return tuple(
        {
            "definition": key[0],
            "value_type": key[1],
            "unit": key[2],
            "window_kind": key[3],
            "method": key[4],
            "points": tuple(points),
        }
        for key, points in sorted(grouped.items())
    )


def _decode_session_id(value: str) -> tuple[UUID, str]:
    prefix, separator, encoded = value.partition(":")
    if separator != ":" or prefix not in {"app", "session"}:
        raise CaptureStoreError("stored Training Session identity is invalid")
    try:
        identifier = UUID(hex=encoded)
    except ValueError:
        raise CaptureStoreError("stored Training Session identity is invalid") from None
    return identifier, "app" if prefix == "app" else "collected"


def _calendar_sort_key(item: Mapping[str, Any]) -> tuple[date, str, int, str]:
    local_start = item.get("local_start")
    time_key = local_start if isinstance(local_start, str) else ""
    kind_order = {"goal_event": 0, "planned_session": 1, "training_session": 2}
    return item["local_date"], time_key, kind_order[item["kind"]], item["id"]
