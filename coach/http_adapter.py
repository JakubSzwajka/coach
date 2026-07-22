"""Persistent private HTTP adapter for the server-only Next.js client.

Transport authentication and a fixed Clerk issuer turn the trusted server-side
subject header into a ClerkActor. Browser payloads cannot select a Profile or
actor. All reads and writes are explicit CoachApplication messages.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from dotenv import load_dotenv

from collector.postgres_adapter import GarminCollectionAdapter, credential_bundle

from .application import (
    AccessDenied,
    ActivateTrainingPlan,
    AdjustTrainingPlan,
    ApplicationUnavailable,
    ArchiveTrainingPlan,
    ClerkActor,
    CoachApplication,
    CoachApplicationError,
    CollectionFailed,
    ConfigureSourceCredentialsAndRequestCollection,
    Conflict,
    CreateGoalEvent,
    CreateTrainingPlan,
    CreateTrainingSession,
    DeleteGoalEvent,
    DeleteTrainingPlan,
    DeleteTrainingSession,
    DeletedAppRecordView,
    EnsureProfile,
    EnsureSourceConnection,
    GetDashboard,
    GetGoalEvent,
    GetProfile,
    GetSourceConnectionStatus,
    GetTrainingPlan,
    GetTrainingPlanHistory,
    GetTrainingSession,
    GetTrends,
    InvalidRequest,
    ListGoalEvents,
    ListTrainingPlans,
    ListTrainingSessions,
    NotFound,
    ReplaceGoalEvent,
    ReplaceTrainingSession,
    RequestCollection,
    SecretBundle,
    SetPlannedSessionFulfilment,
    StaleRevision,
    TrainingPlanHistoryView,
    TrainingPlansView,
    TrainingSessionRecordsView,
    TrendsView,
    UpdateProfileDisplayName,
    _COLLECTION_WORKER_ACTOR,
    _RunNextPendingCollection,
)
from .postgres import DatabaseSettings

_SERVICE_HEADER = "Authorization"
_ACTOR_HEADER = "X-Garmin-Coach-Clerk-Subject"
_MAX_BODY = 32_768


@dataclass(frozen=True, slots=True)
class HttpAdapterConfig:
    clerk_issuer: str
    service_token: str = field(repr=False)
    encryption_key: bytes = field(repr=False)
    host: str = "0.0.0.0"
    port: int = 8080

    @classmethod
    def from_env(cls) -> "HttpAdapterConfig":
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        return cls(
            clerk_issuer=required("GARMIN_COACH_CLERK_ISSUER"),
            service_token=required("GARMIN_COACH_HTTP_SERVICE_TOKEN"),
            encryption_key=required("GARMIN_COACH_ENCRYPTION_KEY").encode("ascii"),
            host=os.environ.get("GARMIN_COACH_HTTP_HOST", "0.0.0.0").strip(),
            port=_port(os.environ.get("GARMIN_COACH_HTTP_PORT", "8080")),
        )

    def __repr__(self) -> str:
        return (
            "HttpAdapterConfig("
            f"clerk_issuer={self.clerk_issuer!r}, service_token=<redacted>, "
            f"encryption_key=<redacted>, host={self.host!r}, port={self.port})"
        )


class PersistentCollectionWorker:
    """One bounded service worker for durable collection jobs."""

    def __init__(self, application: CoachApplication) -> None:
        self._application = application
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="garmin-collection-worker",
            daemon=True,
        )
        self._started = False
        self._start_lock = threading.Lock()

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._started = True
            self._thread.start()

    def notify(self) -> None:
        self._wake.set()

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._started and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self._application.execute(
                    _COLLECTION_WORKER_ACTOR,
                    _RunNextPendingCollection(),
                )
            except CoachApplicationError:
                # Durable job/source health carries safe terminal outcomes;
                # application/DB outages are retried without a hot loop.
                self._wait()
            except Exception:
                # An unexpected lost worker is recovered through the normal
                # running -> worker_lost application semantics on the next pass.
                self._wait()
            else:
                if not result.work_found:
                    self._wait()

    def _wait(self) -> None:
        self._wake.wait(0.5)
        self._wake.clear()


class CoachHttpService:
    def __init__(
        self,
        application: CoachApplication,
        config: HttpAdapterConfig,
        run_collection_worker: bool = False,
    ) -> None:
        self.application = application
        self.config = config
        self._worker = (
            PersistentCollectionWorker(application)
            if run_collection_worker
            else None
        )
        self._admission_guard = threading.Lock()
        self._admitting: set[tuple[str, str]] = set()

    def start(self) -> None:
        if self._worker is not None:
            self._worker.start()

    def close(self) -> None:
        if self._worker is not None:
            self._worker.close()

    @contextmanager
    def _admit(self, actor: ClerkActor):
        key = (actor.issuer, actor.subject)
        with self._admission_guard:
            if key in self._admitting:
                raise Conflict("a collection request is already being admitted")
            self._admitting.add(key)
        try:
            yield
        finally:
            with self._admission_guard:
                self._admitting.discard(key)

    def _notify_worker(self) -> None:
        if self._worker is not None:
            self._worker.notify()

    def actor(self, headers: Mapping[str, str]) -> ClerkActor:
        authorization = _single_header(
            headers, _SERVICE_HEADER, "service_auth_required"
        )
        expected = f"Bearer {self.config.service_token}"
        if not hmac.compare_digest(authorization, expected):
            raise _HttpFailure(401, "service_auth_required")
        subject = _single_header(headers, _ACTOR_HEADER, "actor_required").strip()
        if (
            not subject
            or len(subject) > 512
            or any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in subject
            )
        ):
            raise _HttpFailure(401, "actor_required")
        return ClerkActor(self.config.clerk_issuer, subject)

    def dispatch(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        headers: Mapping[str, str],
        body: Any,
    ) -> tuple[int, Any]:
        if path == "/healthz" and method == "GET":
            try:
                self.application.read(
                    ClerkActor(self.config.clerk_issuer, "health-probe"), GetProfile()
                )
            except ApplicationUnavailable:
                raise _HttpFailure(503, "application_unavailable") from None
            return 200, {"status": "ok"}

        actor = self.actor(headers)
        if method == "GET" and path == "/v1/profile":
            view = self.application.read(actor, GetProfile())
            return 200, _profile(view)
        if method == "GET" and path == "/v1/dashboard":
            starts_on, ends_on = _window(query)
            return 200, _serialize(self.application.read(actor, GetDashboard(starts_on, ends_on)))
        if method == "GET" and path == "/v1/trends":
            starts_on, ends_on = _window(query)
            definitions = query.get("definition")
            return 200, _serialize(
                self.application.read(actor, GetTrends(starts_on, ends_on, definitions))
            )
        if method == "GET" and path == "/v1/activities":
            starts_on, ends_on = _window(query)
            sport = _optional_query(query, "sport")
            return 200, _serialize(
                self.application.read(
                    actor, ListTrainingSessions(starts_on, ends_on, sport)
                )
            )
        if method == "GET" and path == "/v1/garmin/status":
            return 200, _garmin_status(
                self.application.read(actor, GetSourceConnectionStatus("garmin"))
            )
        if method == "POST" and path == "/v1/garmin/connect":
            payload = _object(body)
            _exact_keys(payload, {"email", "password", "days"})
            email = _required_text(payload, "email", maximum=320)
            password = _required_secret(payload, "password", maximum=512)
            days = payload.get("days", 365)
            if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 730:
                raise InvalidRequest("days must be between 1 and 730")
            with self._admit(actor):
                self.application.execute(actor, EnsureProfile())
                source = self.application.execute(
                    actor, EnsureSourceConnection("garmin", "primary")
                )
                # Credential replacement and active-job reservation commit as
                # one Profile-locked transaction. A leader in another adapter
                # cannot observe a requested job without its credentials.
                self.application.execute(
                    actor,
                    ConfigureSourceCredentialsAndRequestCollection(
                        source,
                        SecretBundle(
                            credential_bundle(email, password, initial_days=days)
                        ),
                        f"web-connect:{uuid4().hex}",
                        "initial_sync",
                    ),
                )
                self._notify_worker()
            return 202, {"state": "requested", "kind": "initial_sync"}
        if method == "POST" and path == "/v1/garmin/refresh":
            _exact_keys(_object(body, allow_none=True), set())
            with self._admit(actor):
                source_status = self.application.read(
                    actor, GetSourceConnectionStatus("garmin")
                )
                if source_status is None or not source_status.credentials_stored:
                    raise Conflict("source credentials are not configured")
                source = self.application.execute(
                    actor, EnsureSourceConnection("garmin", "primary")
                )
                self.application.execute(
                    actor,
                    RequestCollection(
                        source, f"web-refresh:{uuid4().hex}", "incremental"
                    ),
                )
                self._notify_worker()
            return 202, {"state": "requested", "kind": "incremental"}
        if method == "POST" and path == "/v1/app/execute":
            return 200, _serialize(self.application.execute(actor, _command(body)))
        if method == "GET" and path.startswith("/v1/app/training-sessions/"):
            identifier = path.removeprefix("/v1/app/training-sessions/")
            return 200, _serialize(
                _available(self.application.read(actor, GetTrainingSession(identifier)))
            )
        if method == "GET" and path.startswith("/v1/app/goal-events/"):
            identifier = path.removeprefix("/v1/app/goal-events/")
            return 200, _serialize(
                _available(self.application.read(actor, GetGoalEvent(identifier)))
            )
        if method == "GET" and path.startswith("/v1/app/training-plans/") and path.endswith("/history"):
            identifier = path.removeprefix("/v1/app/training-plans/").removesuffix("/history")
            return 200, _serialize(
                _available(
                    self.application.read(actor, GetTrainingPlanHistory(identifier))
                )
            )
        if method == "GET" and path.startswith("/v1/app/training-plans/"):
            identifier = path.removeprefix("/v1/app/training-plans/")
            return 200, _serialize(
                _available(self.application.read(actor, GetTrainingPlan(identifier)))
            )
        if method == "GET" and path == "/v1/app/goal-events":
            return 200, _serialize(
                self.application.read(
                    actor,
                    ListGoalEvents(
                        _optional_query(query, "sport"),
                        _optional_query(query, "status"),
                    ),
                )
            )
        if method == "GET" and path == "/v1/app/training-plans":
            return 200, _serialize(
                self.application.read(
                    actor, ListTrainingPlans(_optional_query(query, "status"))
                )
            )
        raise _HttpFailure(404, "not_found")


class _Handler(BaseHTTPRequestHandler):
    service: CoachHttpService

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        parsed = urlsplit(self.path)
        try:
            body = self._read_body() if method in {"POST", "PUT", "DELETE"} else None
            status, response = self.service.dispatch(
                method,
                parsed.path,
                parse_qs(parsed.query, keep_blank_values=True),
                self.headers,
                body,
            )
        except _HttpFailure as exc:
            status, response = exc.status, _error(exc.code)
        except InvalidRequest:
            status, response = 400, _error("invalid_request")
        except (NotFound, AccessDenied):
            status, response = 404, _error("not_found")
        except StaleRevision:
            status, response = 409, _error("stale_revision")
        except Conflict:
            status, response = 409, _error("conflict")
        except CollectionFailed:
            status, response = 502, _error("collection_failed", retryable=True)
        except ApplicationUnavailable:
            status, response = 503, _error("application_unavailable", retryable=True)
        except CoachApplicationError:
            status, response = 500, _error("application_error")
        except Exception:
            status, response = 500, _error("internal_error")
        encoded = json.dumps(_json_value(response), separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_body(self) -> Any:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise _HttpFailure(400, "invalid_request") from None
        if length < 0 or length > _MAX_BODY:
            raise _HttpFailure(413, "request_too_large")
        if length == 0:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _HttpFailure(400, "invalid_request") from None

    def log_message(self, format: str, *args: object) -> None:
        # Request paths can carry record ids; the adapter emits no access log.
        return


@dataclass(frozen=True, slots=True)
class _HttpFailure(RuntimeError):
    status: int
    code: str


def create_server(service: CoachHttpService) -> ThreadingHTTPServer:
    class Handler(_Handler):
        pass

    class ServiceServer(ThreadingHTTPServer):
        daemon_threads = True

        def server_close(self) -> None:
            service.close()
            super().server_close()

    Handler.service = service
    server = ServiceServer((service.config.host, service.config.port), Handler)
    service.start()
    return server


def _single_header(
    headers: Mapping[str, str], name: str, failure_code: str
) -> str:
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all(name, [])
        if len(values) != 1:
            raise _HttpFailure(401, failure_code)
        return values[0]
    return headers.get(name, "")


def _available(value: Any) -> Any:
    if value is None:
        raise NotFound("requested record is not available")
    return value


def _window(query: Mapping[str, list[str]]) -> tuple[date, date]:
    allowed = {"days", "end_date", "definition", "sport"}
    if not set(query).issubset(allowed):
        raise InvalidRequest("unsupported query parameter")
    raw_days = _optional_query(query, "days") or "30"
    try:
        days = int(raw_days)
    except ValueError:
        raise InvalidRequest("days must be an integer") from None
    if not 1 <= days <= 90:
        raise InvalidRequest("days must be between 1 and 90")
    raw_end = _optional_query(query, "end_date")
    try:
        end = date.fromisoformat(raw_end) if raw_end else date.today()
    except ValueError:
        raise InvalidRequest("end_date must be an ISO date") from None
    return end - timedelta(days=days - 1), end


def _optional_query(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if values is None:
        return None
    if len(values) != 1 or not values[0].strip():
        raise InvalidRequest(f"{name} must occur once and be nonblank")
    return values[0].strip()


def _object(value: Any, *, allow_none: bool = False) -> dict[str, Any]:
    if value is None and allow_none:
        return {}
    if not isinstance(value, dict):
        raise InvalidRequest("request body must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], allowed: set[str]) -> None:
    if not set(value).issubset(allowed):
        raise InvalidRequest("request contains unsupported fields")


def _required_text(payload: Mapping[str, Any], name: str, *, maximum: int = 5000) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InvalidRequest(f"{name} is invalid")
    return value.strip()


def _required_secret(payload: Mapping[str, Any], name: str, *, maximum: int) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise InvalidRequest(f"{name} is invalid")
    return value


def _command(body: Any):
    envelope = _object(body)
    _exact_keys(envelope, {"type", "params"})
    command_type = _required_text(envelope, "type", maximum=80)
    params = _object(envelope.get("params"))
    constructors = {
        "update_profile_display_name": lambda: UpdateProfileDisplayName(
            params["expected_revision"], params.get("display_name")
        ),
        "create_training_session": lambda: CreateTrainingSession(params["content"]),
        "replace_training_session": lambda: ReplaceTrainingSession(
            params["id"], params["expected_revision"], params["content"]
        ),
        "delete_training_session": lambda: DeleteTrainingSession(
            params["id"], params["expected_revision"]
        ),
        "create_goal_event": lambda: CreateGoalEvent(params["content"]),
        "replace_goal_event": lambda: ReplaceGoalEvent(
            params["id"], params["expected_revision"], params["content"]
        ),
        "delete_goal_event": lambda: DeleteGoalEvent(
            params["id"], params["expected_revision"]
        ),
        "create_training_plan": lambda: CreateTrainingPlan(
            params["name"],
            params["starts_on"],
            params["ends_on"],
            params["reason"],
            params.get("goal_events"),
            params.get("constraints"),
            params.get("planned_sessions"),
        ),
        "activate_training_plan": lambda: ActivateTrainingPlan(
            params["id"], params["expected_revision"], params["reason"]
        ),
        "archive_training_plan": lambda: ArchiveTrainingPlan(
            params["id"], params["expected_revision"], params["reason"]
        ),
        "delete_training_plan": lambda: DeleteTrainingPlan(
            params["id"], params["expected_revision"]
        ),
        "adjust_training_plan": lambda: AdjustTrainingPlan(
            params["id"],
            params["expected_revision"],
            params["reason"],
            params["effective_from"],
            params["operations"],
            params.get("name"),
            params.get("starts_on"),
            params.get("ends_on"),
            params.get("goal_events"),
            params.get("constraints"),
        ),
        "set_planned_session_fulfilment": lambda: SetPlannedSessionFulfilment(
            params["id"],
            params["expected_revision"],
            params["planned_session_id"],
            params["disposition"],
            params["reason"],
            params.get("matches"),
            params.get("fulfilment_note"),
        ),
    }
    constructor = constructors.get(command_type)
    if constructor is None:
        raise InvalidRequest("unsupported command type")
    allowed = {
        "update_profile_display_name": {"expected_revision", "display_name"},
        "create_training_session": {"content"},
        "replace_training_session": {"id", "expected_revision", "content"},
        "delete_training_session": {"id", "expected_revision"},
        "create_goal_event": {"content"},
        "replace_goal_event": {"id", "expected_revision", "content"},
        "delete_goal_event": {"id", "expected_revision"},
        "create_training_plan": {"name", "starts_on", "ends_on", "reason", "goal_events", "constraints", "planned_sessions"},
        "activate_training_plan": {"id", "expected_revision", "reason"},
        "archive_training_plan": {"id", "expected_revision", "reason"},
        "delete_training_plan": {"id", "expected_revision"},
        "adjust_training_plan": {"id", "expected_revision", "reason", "effective_from", "operations", "name", "starts_on", "ends_on", "goal_events", "constraints"},
        "set_planned_session_fulfilment": {"id", "expected_revision", "planned_session_id", "disposition", "reason", "matches", "fulfilment_note"},
    }[command_type]
    _exact_keys(params, allowed)
    try:
        return constructor()
    except (KeyError, TypeError):
        raise InvalidRequest("command parameters are invalid") from None


def _profile(view: Any) -> Any:
    if view is None:
        return None
    return {"display_name": view.display_name, "revision": view.revision}


def _serialize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, TrainingSessionRecordsView):
        return {"sessions": [_serialize(item) for item in value.sessions]}
    if isinstance(value, TrainingPlansView):
        return {"plans": [_serialize(item) for item in value.plans]}
    if isinstance(value, TrainingPlanHistoryView):
        return {
            "id": value.id,
            "revisions": [
                {
                    "revision": item.revision,
                    "kind": item.kind,
                    "recorded_at": item.recorded_at,
                    "reason": item.reason,
                    "effective_from": item.effective_from,
                    "plan": item.plan,
                }
                for item in value.revisions
            ],
        }
    if isinstance(value, TrendsView):
        return {
            "starts_on": value.starts_on,
            "ends_on": value.ends_on,
            "series": [
                {
                    "definition": series.definition,
                    "value_type": series.value_type,
                    "unit": series.unit,
                    "window_kind": series.window_kind,
                    "method": series.method,
                    "points": [
                        {
                            "status": point.status,
                            "value": point.value,
                            "local_date": point.local_date,
                            "observed_at": point.observed_at,
                            "window_start": point.window_start,
                            "window_end": point.window_end,
                        }
                        for point in series.points
                    ],
                }
                for series in value.series
            ],
        }
    if isinstance(value, DeletedAppRecordView):
        return {"id": value.id, "revision": value.revision, "deleted": value.deleted}
    if hasattr(value, "profile") and hasattr(value, "display_name"):
        return {"display_name": value.display_name, "revision": value.revision}
    if hasattr(value, "content") and hasattr(value, "id"):
        serialized = {
            "id": value.id,
            "origin": value.origin,
            **dict(value.content),
            "created_at": value.created_at,
            "updated_at": value.updated_at,
            "revision": value.revision,
        }
        if hasattr(value, "ownership"):
            serialized["ownership"] = value.ownership
        return serialized
    if hasattr(value, "events"):
        return {"goal_events": [_serialize(item) for item in value.events]}
    if hasattr(value, "latest_observations") and hasattr(value, "recent_sessions"):
        return {
            "starts_on": value.starts_on,
            "ends_on": value.ends_on,
            "display_name": value.display_name,
            "latest_observations": [
                {
                    "definition": item.definition,
                    "status": item.status,
                    "value": item.value,
                    "unit": item.unit,
                    "local_date": item.local_date,
                }
                for item in value.latest_observations
            ],
            "recent_sessions": [_serialize(item) for item in value.recent_sessions],
            "collection_health": _safe_health(value.collection_health),
        }
    return value


def _safe_health(health: Any) -> dict[str, Any]:
    return {
        "records": health.records,
        "captures": health.captures,
        "latest_collected_at": health.latest_collected_at,
        "latest_training_session_date": health.latest_training_session_date,
        "latest_observation_date": health.latest_observation_date,
        "sources": [
            {
                "provider": source.provider,
                "state": source.state,
                "domains": [
                    {
                        "domain": domain.domain,
                        "outcome": domain.outcome,
                        "safe_code": domain.safe_code,
                        "attempted_at": domain.attempted_at,
                        "finished_at": domain.finished_at,
                        "last_success_at": domain.last_success_at,
                    }
                    for domain in source.domains
                ],
            }
            for source in health.sources
        ],
    }


def _garmin_status(status: Any) -> dict[str, Any]:
    if status is None:
        return {"connected": False, "phase": "not_connected", "job": None}
    initial = status.initial_sync
    incremental = status.incremental
    latest = status.latest_job
    prior_success = initial.last_success_at or incremental.last_success_at
    if status.state == "needs_reconnect":
        phase = "needs_reconnect"
    elif latest is not None and latest.state in {"requested", "running"}:
        phase = "authenticating" if status.last_authenticated_at is None else "syncing"
    elif latest is not None and latest.state == "failed" and prior_success is not None:
        phase = "degraded_stale"
    elif latest is not None and latest.state == "failed":
        phase = "degraded"
    elif initial.last_success_at is not None and incremental.last_success_at is None:
        phase = "first_sync_complete"
    elif prior_success is not None:
        phase = "connected"
    elif status.credentials_stored:
        phase = "credentials_stored"
    else:
        phase = "not_connected"
    return {
        "connected": prior_success is not None and status.state != "needs_reconnect",
        "phase": phase,
        "safe_code": status.reconnect_safe_code or (latest.safe_code if latest else None),
        "last_success_at": max(
            [value for value in (initial.last_success_at, incremental.last_success_at) if value],
            default=None,
        ),
        "job": (
            {
                "state": latest.state,
                "kind": latest.kind,
                "safe_code": latest.safe_code,
                "created_at": latest.created_at,
                "started_at": latest.started_at,
                "finished_at": latest.finished_at,
            }
            if latest is not None
            else None
        ),
    }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise TypeError("response contains an unsupported value")


def _error(code: str, *, retryable: bool = False) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": code.replace("_", " "),
            "retryable": retryable,
        }
    }


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise ValueError("GARMIN_COACH_HTTP_PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise ValueError("GARMIN_COACH_HTTP_PORT must be between 1 and 65535")
    return port


def main() -> None:
    if os.environ.get("GARMIN_COACH_DISABLE_DOTENV") != "1":
        load_dotenv(override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int)
    arguments = parser.parse_args()
    config = HttpAdapterConfig.from_env()
    if arguments.port is not None:
        config = HttpAdapterConfig(
            config.clerk_issuer,
            config.service_token,
            config.encryption_key,
            config.host,
            _port(str(arguments.port)),
        )
    settings = DatabaseSettings.from_env()
    application = CoachApplication(
        settings,
        collection_adapter=GarminCollectionAdapter(),
        encryption_key=config.encryption_key,
    )
    server = create_server(
        CoachHttpService(application, config, run_collection_worker=True)
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
