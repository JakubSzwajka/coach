"""Production local MCP server for coaching context and app-owned records."""

from __future__ import annotations

import argparse
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import StrictInt

from .application_adapter import ContextWindow, current_application_adapter

server = FastMCP("garmin-coach")


def _coach():
    """Return the request-scoped PostgreSQL application adapter."""
    return current_application_adapter()


def _parse_end_date(end_date: str | None) -> date | None:
    if end_date is None:
        return None
    try:
        return date.fromisoformat(end_date)
    except ValueError as exc:
        raise ValueError("end_date must be an ISO calendar date") from exc


def _session_content(
    sport: str,
    local_date: str,
    local_start: str | None,
    session_type: str | None,
    title: str | None,
    notes: str | None,
    time_zone: str | None,
    utc_offset: str | None,
    duration: dict[str, Any] | None,
    distance: dict[str, Any] | None,
    session_rpe: int | None,
    loads: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    return {
        "sport": sport,
        "local_date": local_date,
        "local_start": local_start,
        "session_type": session_type,
        "title": title,
        "notes": notes,
        "time_zone": time_zone,
        "utc_offset": utc_offset,
        "duration": duration,
        "distance": distance,
        "session_rpe": session_rpe,
        "loads": loads,
    }


@server.tool()
def read_coaching_context(
    days: int = 14, end_date: str | None = None
) -> dict[str, Any]:
    """Read coherent recent context for a coaching conversation.

    Args:
        days: Inclusive window length from 1 through 90 days.
        end_date: Optional ISO calendar date; defaults to today.

    Missing records and source values remain null or appear in ``missing_dates``.
    Collection outcomes and last-success timestamps come from the collector's
    machine-readable health contract; this tool does not infer freshness.
    """
    return _coach().read_context(
        ContextWindow(days=days, end_date=_parse_end_date(end_date))
    )


@server.tool()
def create_training_session(
    sport: str,
    local_date: str,
    local_start: str | None = None,
    session_type: str | None = None,
    title: str | None = None,
    notes: str | None = None,
    time_zone: str | None = None,
    utc_offset: str | None = None,
    duration: dict[str, Any] | None = None,
    distance: dict[str, Any] | None = None,
    session_rpe: StrictInt | None = None,
    loads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create an app-owned manual (non-Garmin) Training Session.

    Args:
        sport: Required physical discipline, for example ``bouldering``.
        local_date: Required ISO calendar date the session was performed.
        local_start: Optional local date-and-time; its presence sets
            ``timing_precision`` to ``local_datetime`` instead of ``date_only``.
        session_type: Optional sport-specific format or coaching purpose.
        title: Optional short title.
        notes: Optional free-text notes.
        time_zone: Optional IANA time zone carried only when known.
        utc_offset: Optional UTC offset carried only when known.
        duration: Optional ``{value, unit: "seconds", basis: "elapsed"|"active"}``.
        distance: Optional ``{value, unit}`` present only when applicable.
        session_rpe: Optional whole-session RPE integer from 1 through 10.
        loads: Optional list of ``{value, unit, method, source}`` load reports.

    A fresh opaque id is always generated; this never merges or upserts an
    existing session. Garmin-derived sessions cannot be created through this
    tool.
    """
    return _coach().create_session(
        _session_content(
            sport,
            local_date,
            local_start,
            session_type,
            title,
            notes,
            time_zone,
            utc_offset,
            duration,
            distance,
            session_rpe,
            loads,
        )
    )


@server.tool()
def list_training_sessions(
    days: int = 14, end_date: str | None = None, sport: str | None = None
) -> dict[str, Any]:
    """List Garmin-derived and app-owned Training Sessions within a window.

    Args:
        days: Inclusive window length from 1 through 90 days.
        end_date: Optional ISO calendar date; defaults to today.
        sport: Optional exact sport filter.

    Sessions are returned newest first. Garmin-derived sessions are read-only;
    app-owned sessions carry a ``revision`` for the mutation lifecycle.
    """
    sessions = _coach().list_sessions(
        ContextWindow(days=days, end_date=_parse_end_date(end_date)), sport=sport
    )
    return {"sessions": sessions}


@server.tool()
def get_training_session(session_id: str) -> dict[str, Any]:
    """Return one Training Session by its opaque id.

    Accepts both app-owned ids and Garmin-derived ids. Garmin-derived sessions
    are read-only and expose no revision.
    """
    return _coach().get_session(session_id)


@server.tool()
def replace_training_session(
    session_id: str,
    expected_revision: StrictInt,
    sport: str,
    local_date: str,
    local_start: str | None = None,
    session_type: str | None = None,
    title: str | None = None,
    notes: str | None = None,
    time_zone: str | None = None,
    utc_offset: str | None = None,
    duration: dict[str, Any] | None = None,
    distance: dict[str, Any] | None = None,
    session_rpe: StrictInt | None = None,
    loads: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Replace an app-owned Training Session's editable content.

    Args:
        session_id: Opaque id of the app-owned session to replace.
        expected_revision: Revision the caller last read; a stale value is
            rejected with a revision conflict instead of overwriting.

    The remaining arguments fully replace the editable content, following the
    same validation as ``create_training_session``. Identity, provenance, and
    creation metadata are immutable; the revision advances on success.
    Garmin-derived sessions are rejected as read-only.
    """
    return _coach().replace_session(
        session_id,
        expected_revision,
        _session_content(
            sport,
            local_date,
            local_start,
            session_type,
            title,
            notes,
            time_zone,
            utc_offset,
            duration,
            distance,
            session_rpe,
            loads,
        ),
    )


@server.tool()
def delete_training_session(
    session_id: str, expected_revision: StrictInt
) -> dict[str, Any]:
    """Delete an app-owned Training Session.

    Args:
        session_id: Opaque id of the app-owned session to delete.
        expected_revision: Revision the caller last read; a stale value is
            rejected with a revision conflict.

    Garmin-derived sessions are rejected as read-only.
    """
    return _coach().delete_session(session_id, expected_revision)


def _goal_event_content(
    name: str,
    local_date: str,
    sport: str,
    priority: str,
    status: str,
    local_start: str | None,
    time_zone: str | None,
    utc_offset: str | None,
    distance: dict[str, Any] | None,
    goal: dict[str, Any] | None,
    outcome: dict[str, Any] | None,
    notes: str | None,
) -> dict[str, Any]:
    return {
        "name": name,
        "local_date": local_date,
        "sport": sport,
        "priority": priority,
        "status": status,
        "local_start": local_start,
        "time_zone": time_zone,
        "utc_offset": utc_offset,
        "distance": distance,
        "goal": goal,
        "outcome": outcome,
        "notes": notes,
    }


@server.tool()
def create_goal_event(
    name: str,
    local_date: str,
    sport: str,
    priority: str,
    status: str = "scheduled",
    local_start: str | None = None,
    time_zone: str | None = None,
    utc_offset: str | None = None,
    distance: dict[str, Any] | None = None,
    goal: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Create an app-owned Goal Event on the training calendar.

    Args:
        name: Required name of the competition or event.
        local_date: Required event-local ISO calendar date.
        sport: Required physical discipline, for example ``running``.
        priority: Required planning intent: ``primary``, ``secondary``, or
            ``practice``. It never triggers automatic periodization by itself.
        status: Lifecycle state ``scheduled`` (default), ``completed``, or
            ``cancelled``; time passing never changes it.
        local_start: Optional event-local start time; its presence sets
            ``timing_precision`` to ``local_datetime`` instead of ``date_only``.
        time_zone: Optional IANA time zone carried only when known.
        utc_offset: Optional UTC offset carried only when known.
        distance: Optional ``{value, unit}`` present only when applicable.
        goal: Optional recorded target as
            ``{target_duration: {value, unit: "seconds"}, statement}``. It is a
            goal, never a prediction, and is never inferred from training.
        outcome: Optional observed result as
            ``{actual_duration: {value, unit: "seconds"}, statement}``. Its
            absence means unknown; recording it never creates a Training
            Session and never changes the lifecycle status.
        notes: Optional free-text notes.

    A fresh opaque id is always generated; this never merges or upserts an
    existing event.
    """
    return _coach().create_goal_event(
        _goal_event_content(
            name,
            local_date,
            sport,
            priority,
            status,
            local_start,
            time_zone,
            utc_offset,
            distance,
            goal,
            outcome,
            notes,
        )
    )


@server.tool()
def list_goal_events(
    sport: str | None = None, status: str | None = None
) -> dict[str, Any]:
    """List app-owned Goal Events, soonest first.

    Args:
        sport: Optional exact sport filter.
        status: Optional lifecycle filter (``scheduled``, ``completed``, or
            ``cancelled``).
    """
    return {"goal_events": _coach().list_goal_events(sport=sport, status=status)}


@server.tool()
def get_goal_event(goal_event_id: str) -> dict[str, Any]:
    """Return one Goal Event by its opaque id.

    Raises a not-found error when no app-owned Goal Event has that id.
    """
    return _coach().get_goal_event(goal_event_id)


@server.tool()
def replace_goal_event(
    goal_event_id: str,
    expected_revision: StrictInt,
    name: str,
    local_date: str,
    sport: str,
    priority: str,
    status: str = "scheduled",
    local_start: str | None = None,
    time_zone: str | None = None,
    utc_offset: str | None = None,
    distance: dict[str, Any] | None = None,
    goal: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Replace a Goal Event's editable content, checking revision.

    Args:
        goal_event_id: Opaque id of the app-owned event to replace.
        expected_revision: Revision the caller last read; a stale value is
            rejected with a revision conflict.

    This is a full content replacement: send the complete desired event, since
    omitted optional fields (including ``status``, which defaults to
    ``scheduled``) reset rather than merge. The identity and provenance are
    preserved. Changing an event (including a cancellation via ``status``) is a
    revisioned edit that stales any plan that referenced the prior revision.
    All other arguments match ``create_goal_event``.
    """
    return _coach().replace_goal_event(
        goal_event_id,
        expected_revision,
        _goal_event_content(
            name,
            local_date,
            sport,
            priority,
            status,
            local_start,
            time_zone,
            utc_offset,
            distance,
            goal,
            outcome,
            notes,
        ),
    )


@server.tool()
def delete_goal_event(
    goal_event_id: str, expected_revision: StrictInt
) -> dict[str, Any]:
    """Delete an app-owned Goal Event.

    Args:
        goal_event_id: Opaque id of the app-owned event to delete.
        expected_revision: Revision the caller last read; a stale value is
            rejected with a revision conflict.
    """
    return _coach().delete_goal_event(goal_event_id, expected_revision)


@server.tool()
def create_training_plan(
    name: str,
    starts_on: str,
    ends_on: str,
    reason: str,
    goal_events: list[dict[str, Any]] | None = None,
    constraints: list[str] | None = None,
    planned_sessions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a draft Training Plan and return its current head.

    Args:
        name: Required nonblank plan name.
        starts_on: Inclusive ISO start date; must be on or before ``ends_on``.
        ends_on: Inclusive ISO end date.
        reason: Required nonblank rationale recorded on the first Plan Revision.
        goal_events: Optional list of ``{goal_event_id, goal_event_revision}``
            references; each must resolve to that Goal Event's current revision.
        constraints: Optional list of nonblank advisory Plan Constraint
            statements the coach must consider; never an executable rule.
        planned_sessions: Optional list of prescriptions, each
            ``{scheduled_date, sport, prescription}`` plus optional
            ``session_type``, ``target_duration_seconds``,
            ``target_distance_meters``, and ``effort_guidance``.

    The plan starts as ``draft``; identities for the plan and every Planned
    Session are adapter-generated. Effort guidance is coaching text, never a
    recorded RPE, load, prediction, or medical claim.
    """
    return _coach().create_plan(
        name,
        starts_on,
        ends_on,
        reason,
        goal_events=goal_events,
        constraints=constraints,
        planned_sessions=planned_sessions,
    )


@server.tool()
def get_training_plan(plan_id: str) -> dict[str, Any]:
    """Return one Training Plan head with its Planned Sessions.

    ``requires_review`` is true when any Goal Event reference is stale (the
    event advanced or was removed); stale references are surfaced, never
    silently replanned.
    """
    return _coach().get_plan(plan_id)


@server.tool()
def list_training_plans(status: str | None = None) -> dict[str, Any]:
    """List Training Plan heads, earliest start first.

    Args:
        status: Optional exact lifecycle filter (``draft``, ``active``,
            ``archived``).
    """
    return {"plans": _coach().list_plans(status=status)}


@server.tool()
def get_training_plan_history(plan_id: str) -> dict[str, Any]:
    """Return the immutable Plan Revision history, oldest first.

    Each revision carries its recorded timestamp, reason, effective date, and
    the full plan snapshot (prescriptions plus fulfilment state) as it stood.
    Earlier revisions are never rewritten.
    """
    return _coach().get_plan_history(plan_id)


@server.tool()
def activate_training_plan(
    plan_id: str, expected_revision: StrictInt, reason: str
) -> dict[str, Any]:
    """Activate a draft Training Plan.

    Args:
        plan_id: Opaque id of the draft plan.
        expected_revision: Revision the caller last read; a stale value is
            rejected with a revision conflict.
        reason: Required nonblank rationale recorded on the revision.

    At most one plan may be active. Activation is rejected when another plan is
    already active, when a Goal Event reference is stale, or when a still
    ``scheduled`` prescription is dated before the activation date.
    """
    return _coach().activate_plan(plan_id, expected_revision, reason)


@server.tool()
def archive_training_plan(
    plan_id: str, expected_revision: StrictInt, reason: str
) -> dict[str, Any]:
    """Archive a draft or active Training Plan; archiving is terminal.

    Args:
        plan_id: Opaque id of the plan.
        expected_revision: Revision the caller last read.
        reason: Required nonblank rationale recorded on the revision.
    """
    return _coach().archive_plan(plan_id, expected_revision, reason)


@server.tool()
def delete_training_plan(
    plan_id: str, expected_revision: StrictInt
) -> dict[str, Any]:
    """Delete a never-active draft plan that has no fulfilment history.

    Args:
        plan_id: Opaque id of the draft plan.
        expected_revision: Revision the caller last read.

    An ever-active plan or one with recorded fulfilment is archived, not
    deleted.
    """
    return _coach().delete_plan(plan_id, expected_revision)


@server.tool()
def adjust_training_plan(
    plan_id: str,
    expected_revision: StrictInt,
    reason: str,
    effective_from: str,
    operations: list[dict[str, Any]],
    name: str | None = None,
    starts_on: str | None = None,
    ends_on: str | None = None,
    goal_events: list[dict[str, Any]] | None = None,
    constraints: list[str] | None = None,
) -> dict[str, Any]:
    """Append a reasoned adjustment that changes only future prescriptions.

    Args:
        plan_id: Opaque id of the draft or active plan.
        expected_revision: Revision the caller last read.
        reason: Required nonblank rationale for the adjustment.
        effective_from: ISO date from which change is allowed; it must be no
            earlier than today and no earlier than a prior adjustment, and lie
            within the plan range. Prescriptions dated before it are frozen.
        operations: List of edits, each one of
            ``{"op": "add", ...prescription}``,
            ``{"op": "update", "planned_session_id", ...prescription}``, or
            ``{"op": "cancel", "planned_session_id"}``. Adds and updates must
            land on or after ``effective_from``; a fundamentally different
            session is a cancel plus an add. Cancelling withdraws future work
            without erasing its history.
        name, starts_on, ends_on, goal_events, constraints: Optional head
            changes. Shortening the range is rejected if it would orphan a
            retained Planned Session; a stale Goal Event reference must be
            refreshed here or the adjustment is rejected.

    Fulfilment state is never changed by this tool; use
    ``set_planned_session_fulfilment`` for matches and dispositions.
    """
    return _coach().adjust_plan(
        plan_id,
        expected_revision,
        reason,
        effective_from,
        operations,
        name=name,
        starts_on=starts_on,
        ends_on=ends_on,
        goal_events=goal_events,
        constraints=constraints,
    )


@server.tool()
def set_planned_session_fulfilment(
    plan_id: str,
    expected_revision: StrictInt,
    planned_session_id: str,
    disposition: str,
    reason: str,
    matches: list[str] | None = None,
    fulfilment_note: str | None = None,
) -> dict[str, Any]:
    """Record an audited fulfilment/match correction without touching a
    prescription. Allowed on past and archived history.

    Args:
        plan_id: Opaque id of the plan.
        expected_revision: Revision the caller last read.
        planned_session_id: Id of the Planned Session to correct.
        disposition: One of ``scheduled``, ``fulfilled``, or ``skipped``.
            ``fulfilled`` requires at least one match; the others require none.
            Future cancellation is a prescription change, handled by
            ``adjust_training_plan``.
        reason: Required nonblank rationale for the correction.
        matches: Training Session ids that fulfil this Planned Session; a
            device split may attach several. Each must reference an existing
            Training Session and may fulfil at most one Planned Session.
        fulfilment_note: Optional nonblank coach or athlete interpretation.

    Matching is explicit identity linkage; nothing is auto-matched from date,
    sport, or similarity. Completed Training Sessions stay read-only and Garmin
    is never written.
    """
    return _coach().set_planned_session_fulfilment(
        plan_id,
        expected_revision,
        planned_session_id,
        disposition,
        reason,
        matches=matches,
        fulfilment_note=fulfilment_note,
    )


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main() -> None:
    """Run over stdio by default or loopback-only Streamable HTTP."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=8765,
        help="loopback HTTP port; used only with --transport streamable-http",
    )
    arguments = parser.parse_args()

    if arguments.transport == "streamable-http":
        server.settings.host = "127.0.0.1"
        server.settings.port = arguments.port
        server.settings.streamable_http_path = "/mcp"

    server.run(transport=arguments.transport)


if __name__ == "__main__":
    main()
