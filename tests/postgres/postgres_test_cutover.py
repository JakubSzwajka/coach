from __future__ import annotations

import ast
import builtins
import http.client
import json
import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
import psycopg
from cryptography.fernet import Fernet
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken

from coach.application import (
    AccessDenied,
    ApplicationUnavailable,
    ClerkActor,
    CoachApplication,
    CollectionFailed,
    ConfigureSourceCredentials,
    Conflict,
    CreateGoalEvent,
    CreateTrainingPlan,
    EnsureProfile,
    EnsureSourceConnection,
    GetCollectionJob,
    GetDashboard,
    GetSourceStatus,
    GetTrainingSession,
    InvalidRequest,
    RequestCollection,
    RunCollectionJob,
    SecretBundle,
    _AuthenticatedTokenSession,
    _COLLECTION_WORKER_ACTOR,
    _RunNextPendingCollection,
)
from coach.application_adapter import (
    ApplicationCoachAdapter,
    ContextWindow,
    current_application_adapter,
)
from coach.http_adapter import (
    CoachHttpService,
    HttpAdapterConfig,
    _command,
    create_server,
)
from coach.postgres import DatabaseSettings
from coach.postgres.migrate import migrate
from coach.mcp_server import read_coaching_context
from coach.remote_mcp_server import (
    RemoteMcpConfig,
    _REMOTE_TOOLS,
    _audited_tool,
    create_remote_server,
)
from collector import endpoints
from collector.collect import main as collect_main
from collector.postgres_adapter import GarminCollectionAdapter, _GarminSession
from tests.postgres.support import test_database


class _SyntheticGarmin:
    def get_stats(self, iso: str):
        return {
            "totalSteps": 4321,
            "restingHeartRate": 49,
            "privateRawMarker": "must-not-project",
        }

    def get_activities(self, start: int, limit: int):
        return [{"activityId": "synthetic-source-id"}]

    def get_activity(self, activity_id: str):
        return {
            "activityName": "Synthetic run",
            "activityTypeDTO": {"typeKey": "running"},
            "startTimeLocal": f"{date.today().isoformat()} 07:30:00",
            "duration": 1800,
            "distance": 5000,
            "privateRawMarker": "must-not-project",
        }


class _SyntheticGarminAdapter(GarminCollectionAdapter):
    def __init__(self, client: _SyntheticGarmin) -> None:
        self.client = client

    def authenticate(self, token_directory, credentials, cached_tokens):
        return _AuthenticatedTokenSession(
            _GarminSession(self.client, initial_days=1, activity_limit=10),
            b"synthetic-rotated-token-bundle",
        )


class _IngestTracingApplication(CoachApplication):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ingest_calls = 0

    def ingest(self, profile, batch):
        self.ingest_calls += 1
        return super().ingest(profile, batch)


class CutoverPostgreSQLTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database = test_database()
        cls.application_url = database.application_url
        cls.migration_url = database.migration_url
        cls.settings = DatabaseSettings.from_url(cls.application_url)
        migrate(DatabaseSettings.from_url(cls.migration_url))

    def setUp(self) -> None:
        with psycopg.connect(self.migration_url) as connection:
            connection.execute("TRUNCATE TABLE profiles CASCADE")
        self.key = Fernet.generate_key()
        self.application = CoachApplication(self.settings, encryption_key=self.key)
        self.actor_a = ClerkActor("https://identity.example.test", "cutover-a")
        self.actor_b = ClerkActor("https://identity.example.test", "cutover-b")
        self.profile_a = self.application.execute(
            self.actor_a, EnsureProfile("Synthetic A")
        ).profile
        self.application.execute(self.actor_b, EnsureProfile("Synthetic B"))

    def _collector_ingest(self) -> str:
        adapter = _SyntheticGarminAdapter(_SyntheticGarmin())
        application = _IngestTracingApplication(
            self.settings,
            collection_adapter=adapter,
            encryption_key=self.key,
        )
        source = application.execute(
            self.actor_a, EnsureSourceConnection("garmin", "primary")
        )
        application.execute(
            self.actor_a,
            ConfigureSourceCredentials(source, SecretBundle(b"synthetic-credentials")),
        )
        requested = application.execute(
            self.actor_a,
            RequestCollection(source, "synthetic-real-collector", "initial_sync"),
        )
        with (
            patch.object(endpoints, "DAILY", {"stats": lambda client, iso: client.get_stats(iso)}),
            patch.object(endpoints, "PLANS", {}),
            patch.object(endpoints, "SNAPSHOT", {}),
            patch.object(
                endpoints,
                "ACTIVITY_DETAIL",
                {"summary": lambda client, identifier: client.get_activity(identifier)},
            ),
        ):
            completed = application.execute(
                self.actor_a, RunCollectionJob(requested.job)
            )
        self.assertEqual(completed.state, "succeeded")
        self.assertEqual(application.ingest_calls, 1)
        dashboard = self.application.read(
            self.actor_a, GetDashboard(date.today(), date.today())
        )
        self.assertEqual(len(dashboard.recent_sessions), 1)
        self.assertEqual(len(dashboard.latest_observations), 10)
        return dashboard.recent_sessions[0].id

    def test_collector_and_local_mcp_adapter_use_only_application_contract(self) -> None:
        collected_id = self._collector_ingest()
        adapter = ApplicationCoachAdapter(self.application, self.actor_a)
        context = adapter.read_context(ContextWindow(1, date.today()))
        self.assertEqual(context["wellness"]["records"][0]["steps"], 4321)
        self.assertEqual(context["training_history"][0]["id"], collected_id)

        created = adapter.create_session(
            {"sport": "strength", "local_date": date.today().isoformat()}
        )
        self.assertEqual(created["revision"], 1)
        with self.assertRaises(Conflict):
            adapter.replace_session(
                collected_id,
                1,
                {"sport": "running", "local_date": date.today().isoformat()},
            )
        self.assertEqual(len(adapter.list_sessions(ContextWindow(1, date.today()))), 2)

    def test_persistent_worker_resumes_requested_and_recovers_running_after_restart(self) -> None:
        adapter = _SyntheticGarminAdapter(_SyntheticGarmin())
        application = CoachApplication(
            self.settings,
            collection_adapter=adapter,
            encryption_key=self.key,
        )
        source = application.execute(
            self.actor_a, EnsureSourceConnection("garmin", "primary")
        )
        application.execute(
            self.actor_a,
            ConfigureSourceCredentials(source, SecretBundle(b"synthetic-credentials")),
        )
        requested = application.execute(
            self.actor_a,
            RequestCollection(source, "resume-requested", "initial_sync"),
        )
        config = HttpAdapterConfig(
            "https://identity.example.test",
            "synthetic-service-token",
            self.key,
            "127.0.0.1",
            0,
        )
        with (
            patch.object(endpoints, "DAILY", {"stats": lambda client, iso: client.get_stats(iso)}),
            patch.object(endpoints, "PLANS", {}),
            patch.object(endpoints, "SNAPSHOT", {}),
            patch.object(endpoints, "ACTIVITY_DETAIL", {}),
        ):
            first_server = create_server(
                CoachHttpService(
                    application, config, run_collection_worker=True
                )
            )
            try:
                completed = self._wait_job(application, requested.job, "succeeded")
            finally:
                first_server.server_close()
        self.assertEqual(completed.safe_code, None)

        class LostWorkerAdapter(_SyntheticGarminAdapter):
            def authenticate(self, token_directory, credentials, cached_tokens):
                raise psycopg.OperationalError("synthetic adapter process loss")

        stranded_application = CoachApplication(
            self.settings,
            collection_adapter=LostWorkerAdapter(_SyntheticGarmin()),
            encryption_key=self.key,
        )
        stranded = stranded_application.execute(
            self.actor_a,
            RequestCollection(source, "resume-running", "incremental"),
        )
        with self.assertRaises(ApplicationUnavailable):
            stranded_application.execute(
                self.actor_a, RunCollectionJob(stranded.job)
            )
        self.assertEqual(
            stranded_application.read(
                self.actor_a, GetCollectionJob(stranded.job)
            ).state,
            "running",
        )

        recovery_application = CoachApplication(
            self.settings,
            collection_adapter=adapter,
            encryption_key=self.key,
        )
        second_server = create_server(
            CoachHttpService(
                recovery_application, config, run_collection_worker=True
            )
        )
        try:
            recovered = self._wait_job(
                recovery_application, stranded.job, "failed"
            )
        finally:
            second_server.server_close()
        self.assertEqual(recovered.safe_code, "worker_lost")
        self.assertFalse(
            any(
                thread.name == "garmin-collection-worker" and thread.is_alive()
                for thread in threading.enumerate()
            )
        )

    def test_http_adapter_worker_has_no_persistence_or_profile_enumeration(self) -> None:
        adapter_module = __import__(
            "coach.http_adapter", fromlist=["__file__"]
        )
        source = Path(adapter_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("CollectionStore", imported_names)
        self.assertNotIn("coach.postgres._collection_store", imported_names)
        self.assertNotIn("_collection_store", source)
        self.assertNotIn("next_pending_work", source)
        self.assertNotIn("worker_connection", source)
        self.assertNotIn("clerk_subject", source)
        self.assertNotIn("profile_id", source)

        command = _RunNextPendingCollection()
        with self.assertRaises(AccessDenied):
            self.application.execute(self.actor_a, command)
        with self.assertRaises(InvalidRequest):
            self.application.execute(_COLLECTION_WORKER_ACTOR, EnsureProfile())
        idle = self.application.execute(_COLLECTION_WORKER_ACTOR, command)
        self.assertFalse(idle.work_found)
        self.assertEqual(set(idle.__dataclass_fields__), {"work_found"})
        rendered = repr(idle)
        self.assertNotIn("cutover-a", rendered)
        self.assertNotIn("capability", rendered.lower())
        with self.assertRaisesRegex(InvalidRequest, "unsupported command type"):
            _command({"type": "run_next_pending_collection", "params": {}})

    def test_two_service_workers_do_not_recover_work_held_by_live_worker(self) -> None:
        with (
            patch.object(
                endpoints,
                "DAILY",
                {"stats": lambda client, iso: client.get_stats(iso)},
            ),
            patch.object(endpoints, "PLANS", {}),
            patch.object(endpoints, "SNAPSHOT", {}),
            patch.object(endpoints, "ACTIVITY_DETAIL", {}),
        ):
            external = _SyntheticGarminAdapter(_SyntheticGarmin()).collect(
                _GarminSession(_SyntheticGarmin(), 1, 10), "initial_sync"
            )
        started = threading.Event()
        release = threading.Event()

        class BlockingAdapter(_SyntheticGarminAdapter):
            def collect(self, session, kind):
                started.set()
                if not release.wait(timeout=10):
                    raise AssertionError("synthetic collection was not released")
                return external

        source = self.application.execute(
            self.actor_a, EnsureSourceConnection("garmin", "primary")
        )
        self.application.execute(
            self.actor_a,
            ConfigureSourceCredentials(
                source, SecretBundle(b"synthetic-credentials")
            ),
        )
        requested = self.application.execute(
            self.actor_a,
            RequestCollection(source, "two-live-service-workers", "initial_sync"),
        )
        first_application = CoachApplication(
            self.settings,
            collection_adapter=BlockingAdapter(_SyntheticGarmin()),
            encryption_key=self.key,
        )
        second_application = CoachApplication(
            self.settings,
            collection_adapter=_SyntheticGarminAdapter(_SyntheticGarmin()),
            encryption_key=self.key,
        )
        config = HttpAdapterConfig(
            "https://identity.example.test",
            "synthetic-service-token",
            self.key,
            "127.0.0.1",
            0,
        )
        first = create_server(
            CoachHttpService(
                first_application, config, run_collection_worker=True
            )
        )
        self.assertTrue(started.wait(timeout=5))
        second = create_server(
            CoachHttpService(
                second_application, config, run_collection_worker=True
            )
        )
        try:
            time.sleep(0.75)
            live = self.application.read(
                self.actor_a, GetCollectionJob(requested.job)
            )
            self.assertEqual((live.state, live.safe_code), ("running", None))
            with psycopg.connect(self.application_url) as connection:
                evidence = connection.execute(
                    "SELECT j.state, j.safe_code, r.state, a.state "
                    "FROM collection_jobs AS j "
                    "JOIN collection_runs AS r ON r.profile_id = j.profile_id "
                    "AND r.job_id = j.id "
                    "JOIN collection_attempts AS a ON a.profile_id = r.profile_id "
                    "AND a.run_id = r.id"
                ).fetchone()
            self.assertEqual(evidence, ("running", None, "running", "running"))
        finally:
            release.set()
            first.server_close()
            second.server_close()
        completed = self._wait_job(
            self.application, requested.job, "succeeded"
        )
        self.assertIsNone(completed.safe_code)

    def test_concurrent_connect_and_refresh_have_bounded_conflict_admission(self) -> None:
        with (
            patch.object(endpoints, "DAILY", {"stats": lambda client, iso: client.get_stats(iso)}),
            patch.object(endpoints, "PLANS", {}),
            patch.object(endpoints, "SNAPSHOT", {}),
            patch.object(endpoints, "ACTIVITY_DETAIL", {}),
        ):
            external = _SyntheticGarminAdapter(_SyntheticGarmin()).collect(
                _GarminSession(_SyntheticGarmin(), 1, 10), "initial_sync"
            )

        started = threading.Event()
        release = threading.Event()

        class BlockingAdapter(_SyntheticGarminAdapter):
            def collect(self, session, kind):
                started.set()
                if not release.wait(timeout=10):
                    raise AssertionError("synthetic collection was not released")
                return external

        application = CoachApplication(
            self.settings,
            collection_adapter=BlockingAdapter(_SyntheticGarmin()),
            encryption_key=self.key,
        )
        server, thread = self._http_server(application, self.settings)
        try:
            port = server.server_address[1]
            connect_body = {
                "email": "synthetic@example.test",
                "password": "synthetic-password",
                "days": 1,
            }
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(
                    executor.map(
                        lambda _: self._request(
                            port,
                            "POST",
                            "/v1/garmin/connect",
                            subject="cutover-a",
                            body=connect_body,
                        ),
                        range(2),
                    )
                )
            self.assertEqual(sorted(response[0] for response in responses), [202, 409])
            self.assertTrue(started.wait(timeout=5))

            with ThreadPoolExecutor(max_workers=8) as executor:
                refreshes = list(
                    executor.map(
                        lambda _: self._request(
                            port,
                            "POST",
                            "/v1/garmin/refresh",
                            subject="cutover-a",
                        ),
                        range(16),
                    )
                )
            self.assertEqual({response[0] for response in refreshes}, {409})
            self.assertEqual(
                {response[1]["error"]["code"] for response in refreshes},
                {"conflict"},
            )
            status_code, status = self._request(
                port, "GET", "/v1/garmin/status", subject="cutover-a"
            )
            self.assertEqual(status_code, 200)
            self.assertEqual(status["job"]["state"], "running")
            self.assertEqual(status["job"]["kind"], "initial_sync")
            with psycopg.connect(self.application_url) as connection:
                counts = connection.execute(
                    "SELECT count(*), count(*) FILTER (WHERE state IN ('requested', 'running')) "
                    "FROM collection_jobs"
                ).fetchone()
            self.assertEqual(counts, (1, 1))
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_partial_garmin_endpoint_failure_cannot_advance_freshness(self) -> None:
        private_detail = "private optional endpoint failure"
        application = _IngestTracingApplication(
            self.settings,
            collection_adapter=_SyntheticGarminAdapter(_SyntheticGarmin()),
            encryption_key=self.key,
        )
        source = application.execute(
            self.actor_a, EnsureSourceConnection("garmin", "primary")
        )
        application.execute(
            self.actor_a,
            ConfigureSourceCredentials(source, SecretBundle(b"synthetic-credentials")),
        )
        requested = application.execute(
            self.actor_a,
            RequestCollection(source, "synthetic-partial-endpoint", "initial_sync"),
        )

        def fail_sleep(client, iso):
            raise RuntimeError(private_detail)

        with (
            patch.object(
                endpoints,
                "DAILY",
                {
                    "stats": lambda client, iso: client.get_stats(iso),
                    "sleep": fail_sleep,
                },
            ),
            patch.object(endpoints, "PLANS", {}),
            patch.object(endpoints, "SNAPSHOT", {}),
            patch.object(endpoints, "ACTIVITY_DETAIL", {}),
            self.assertRaises(CollectionFailed),
        ):
            application.execute(self.actor_a, RunCollectionJob(requested.job))

        status = application.read(self.actor_a, GetSourceStatus(source))
        self.assertEqual(application.ingest_calls, 0)
        self.assertEqual((status.outcome, status.safe_code), ("failed", "provider_unavailable"))
        self.assertIsNone(status.last_success_at)
        self.assertIsNone(status.checkpoint_revision)
        with psycopg.connect(self.application_url) as connection:
            evidence = json.dumps(
                connection.execute(
                    "SELECT safe_code, diagnostics FROM collection_jobs"
                ).fetchall(),
                default=str,
            )
            captures = connection.execute(
                "SELECT count(*) FROM collected_record_captures"
            ).fetchone()[0]
        self.assertEqual(captures, 0)
        self.assertNotIn(private_detail, evidence)

    def test_remote_registry_is_read_only_and_keeps_all_eight_read_surfaces(self) -> None:
        self.assertEqual(
            {tool.__name__ for tool in _REMOTE_TOOLS},
            {
                "read_coaching_context",
                "list_training_sessions",
                "get_training_session",
                "list_goal_events",
                "get_goal_event",
                "get_training_plan",
                "list_training_plans",
                "get_training_plan_history",
            },
        )

    def test_private_http_auth_actor_isolation_mutation_staleness_and_safe_projection(self) -> None:
        self._collector_ingest()
        server, thread = self._http_server(self.application)
        try:
            port = server.server_address[1]
            unauthorized = self._request(port, "GET", "/v1/dashboard?days=1", token="wrong")
            self.assertEqual(unauthorized[0], 401)
            self.assertEqual(unauthorized[1]["error"]["code"], "service_auth_required")
            status_code, source_status = self._request(
                port, "GET", "/v1/garmin/status", subject="cutover-a"
            )
            self.assertEqual(status_code, 200)
            self.assertEqual(source_status["phase"], "first_sync_complete")

            actor_in_body = self._request(
                port,
                "POST",
                "/v1/app/execute",
                subject="cutover-a",
                body={
                    "type": "create_training_session",
                    "params": {
                        "content": {
                            "sport": "strength",
                            "local_date": date.today().isoformat(),
                        }
                    },
                    "subject": "cutover-b",
                },
            )
            self.assertEqual(actor_in_body[0], 400)

            created_status, created = self._request(
                port,
                "POST",
                "/v1/app/execute",
                subject="cutover-a",
                body={
                    "type": "create_training_session",
                    "params": {
                        "content": {
                            "sport": "strength",
                            "local_date": date.today().isoformat(),
                            "title": "Synthetic manual session",
                        }
                    },
                },
            )
            self.assertEqual(created_status, 200)
            self.assertEqual(created["revision"], 1)

            replaced_status, replaced = self._request(
                port,
                "POST",
                "/v1/app/execute",
                subject="cutover-a",
                body={
                    "type": "replace_training_session",
                    "params": {
                        "id": created["id"],
                        "expected_revision": 1,
                        "content": {
                            "sport": "strength",
                            "local_date": date.today().isoformat(),
                            "title": "Synthetic revised session",
                        },
                    },
                },
            )
            self.assertEqual(replaced_status, 200)
            self.assertEqual(replaced["revision"], 2)
            stale_status, stale = self._request(
                port,
                "POST",
                "/v1/app/execute",
                subject="cutover-a",
                body={
                    "type": "replace_training_session",
                    "params": {
                        "id": created["id"],
                        "expected_revision": 1,
                        "content": {
                            "sport": "strength",
                            "local_date": date.today().isoformat(),
                        },
                    },
                },
            )
            self.assertEqual(stale_status, 409)
            self.assertEqual(stale["error"]["code"], "stale_revision")

            goal = self.application.execute(
                self.actor_a,
                CreateGoalEvent(
                    {
                        "name": "Synthetic event",
                        "local_date": date.today().isoformat(),
                        "sport": "running",
                        "priority": "practice",
                        "status": "scheduled",
                    }
                ),
            )
            plan = self.application.execute(
                self.actor_a,
                CreateTrainingPlan(
                    "Synthetic plan",
                    date.today().isoformat(),
                    date.today().isoformat(),
                    "Synthetic acceptance",
                ),
            )
            unavailable_paths = (
                f"/v1/app/training-sessions/{created['id']}",
                f"/v1/app/goal-events/{goal.id}",
                f"/v1/app/training-plans/{plan.id}",
                f"/v1/app/training-plans/{plan.id}/history",
            )
            for unavailable_path in unavailable_paths:
                with self.subTest(path=unavailable_path):
                    foreign_status, foreign = self._request(
                        port,
                        "GET",
                        unavailable_path,
                        subject="cutover-b",
                    )
                    self.assertEqual(foreign_status, 404)
                    self.assertEqual(foreign["error"]["code"], "not_found")

            dashboard_status, dashboard = self._request(
                port, "GET", "/v1/dashboard?days=1", subject="cutover-a"
            )
            self.assertEqual(dashboard_status, 200)
            rendered = json.dumps(dashboard)
            self.assertNotIn("privateRawMarker", rendered)
            self.assertNotIn("must-not-project", rendered)
            self.assertNotIn("synthetic-source-id", rendered)
            self.assertNotIn("payload", rendered)
            self.assertNotIn("source_key", rendered)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_postgresql_outage_fails_closed_across_runtime_without_file_touches(self) -> None:
        unavailable_url = "postgresql://coach:synthetic@127.0.0.1:1/unavailable"
        unavailable = CoachApplication(
            DatabaseSettings.from_url(unavailable_url), encryption_key=self.key
        )
        with TemporaryDirectory() as temporary:
            legacy = Path(temporary) / "legacy-sentinel"
            legacy.mkdir()
            (legacy / "derived").mkdir()
            touched: list[str] = []

            def is_legacy(value) -> bool:
                try:
                    candidate = Path(os.path.abspath(os.fspath(value)))
                except TypeError:
                    return False
                return candidate == legacy or legacy in candidate.parents

            original_open = builtins.open
            original_os_open = os.open
            original_listdir = os.listdir
            original_scandir = os.scandir
            original_path_open = Path.open
            original_path_mkdir = Path.mkdir
            original_path_read_text = Path.read_text
            original_path_write_text = Path.write_text
            original_path_read_bytes = Path.read_bytes
            original_path_write_bytes = Path.write_bytes

            def guarded(name, original):
                def invoke(value, *args, **kwargs):
                    if is_legacy(value):
                        touched.append(f"{name}:{value}")
                        raise AssertionError("legacy filesystem fallback attempted")
                    return original(value, *args, **kwargs)

                return invoke

            environment = {
                "GARMIN_COACH_DATABASE_URL": unavailable_url,
                "GARMIN_COACH_LOCAL_ACTOR_ISSUER": "https://identity.example.test",
                "GARMIN_COACH_LOCAL_ACTOR_SUBJECT": "cutover-a",
                "GARMIN_COACH_ENCRYPTION_KEY": self.key.decode("ascii"),
                "GARMIN_COACH_DATA_DIR": str(legacy),
                "GARMIN_COACH_DISABLE_DOTENV": "1",
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch("builtins.open", guarded("open", original_open)),
                patch("os.open", guarded("os.open", original_os_open)),
                patch("os.listdir", guarded("listdir", original_listdir)),
                patch("os.scandir", guarded("scandir", original_scandir)),
                patch.object(Path, "open", guarded("Path.open", original_path_open)),
                patch.object(Path, "mkdir", guarded("mkdir", original_path_mkdir)),
                patch.object(
                    Path, "read_text", guarded("read_text", original_path_read_text)
                ),
                patch.object(
                    Path, "write_text", guarded("write_text", original_path_write_text)
                ),
                patch.object(
                    Path, "read_bytes", guarded("read_bytes", original_path_read_bytes)
                ),
                patch.object(
                    Path, "write_bytes", guarded("write_bytes", original_path_write_bytes)
                ),
            ):
                with self.assertRaises(ApplicationUnavailable):
                    current_application_adapter()
                with self.assertRaises(ApplicationUnavailable):
                    read_coaching_context(days=1)

                authenticated_remote = _audited_tool(
                    read_coaching_context, "https://identity.example.test"
                )
                access_token = AccessToken(
                    token="synthetic",
                    client_id="synthetic",
                    scopes=["profile"],
                    expires_at=int(time.time()) + 60,
                    resource="https://mcp.example.test/mcp",
                    subject="cutover-a",
                )
                with (
                    patch(
                        "coach.remote_mcp_server.get_access_token",
                        return_value=access_token,
                    ),
                    self.assertRaises(ApplicationUnavailable),
                ):
                    authenticated_remote(days=1)

                with self.assertRaises(ApplicationUnavailable):
                    collect_main([])

                server, thread = self._http_server(unavailable)
                try:
                    status, body = self._request(
                        server.server_address[1],
                        "GET",
                        "/v1/dashboard?days=1",
                        subject="cutover-a",
                    )
                    self.assertEqual(status, 503)
                    self.assertEqual(
                        body["error"]["code"], "application_unavailable"
                    )
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)

            self.assertEqual(touched, [])

    def _wait_job(self, application, job, state, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = application.read(self.actor_a, GetCollectionJob(job))
            if view is not None and view.state == state:
                return view
            time.sleep(0.02)
        self.fail(f"collection job did not reach {state}")

    def _http_server(
        self,
        application: CoachApplication,
        run_collection_worker: bool = False,
    ):
        config = HttpAdapterConfig(
            "https://identity.example.test",
            "synthetic-service-token",
            self.key,
            "127.0.0.1",
            0,
        )
        server = create_server(
            CoachHttpService(
                application,
                config,
                run_collection_worker=run_collection_worker,
            )
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def _request(
        self,
        port: int,
        method: str,
        path: str,
        *,
        token: str = "synthetic-service-token",
        subject: str | None = None,
        body: object | None = None,
    ):
        headers = {"Authorization": f"Bearer {token}"}
        if subject is not None:
            headers["X-Garmin-Coach-Clerk-Subject"] = subject
        encoded = None
        if body is not None:
            encoded = json.dumps(body)
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()


class _AcceptingVerifier:
    async def verify_token(self, token: str) -> AccessToken:
        return AccessToken(
            token=token,
            client_id="synthetic-client",
            scopes=["profile"],
            expires_at=int(time.time()) + 300,
            resource="https://mcp.example.test/mcp",
            subject="mcp-process-actor",
        )


class McpProcessPostgreSQLTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database = test_database()
        cls.application_url = database.application_url
        cls.migration_url = database.migration_url
        migrate(DatabaseSettings.from_url(cls.migration_url))

    def setUp(self) -> None:
        with psycopg.connect(self.migration_url) as connection:
            connection.execute("TRUNCATE TABLE profiles CASCADE")
        application = CoachApplication(DatabaseSettings.from_url(self.application_url))
        application.execute(
            ClerkActor("https://identity.example.test", "mcp-process-actor"),
            EnsureProfile("Synthetic MCP Actor"),
        )

    async def test_local_stdio_preserves_twenty_tools_and_representative_mutation(self) -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "coach.mcp_server"],
            cwd=Path(__file__).resolve().parents[2],
            env={
                "GARMIN_COACH_DATABASE_URL": self.application_url,
                "GARMIN_COACH_LOCAL_ACTOR_ISSUER": "https://identity.example.test",
                "GARMIN_COACH_LOCAL_ACTOR_SUBJECT": "mcp-process-actor",
                "GARMIN_COACH_DISABLE_DOTENV": "1",
            },
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = await session.list_tools()
                created = await session.call_tool(
                    "create_training_session",
                    {
                        "sport": "strength",
                        "local_date": date.today().isoformat(),
                    },
                )
                context = await session.call_tool(
                    "read_coaching_context",
                    {"days": 1, "end_date": date.today().isoformat()},
                )
        self.assertEqual(len(tools.tools), 20)
        self.assertFalse(created.isError)
        self.assertEqual(created.structuredContent["revision"], 1)
        self.assertFalse(context.isError)
        self.assertEqual(len(context.structuredContent["training_history"]), 1)

    async def test_authenticated_remote_mcp_reads_same_actor_and_exposes_no_writes(self) -> None:
        config = RemoteMcpConfig(
            public_url="https://mcp.example.test/mcp",
            issuer_url="https://identity.example.test",
            clerk_secret_key="synthetic-clerk-secret",
        )
        with patch.dict(
            os.environ,
            {"GARMIN_COACH_DATABASE_URL": self.application_url},
            clear=False,
        ):
            server = create_remote_server(config, token_verifier=_AcceptingVerifier())
            transport = httpx.ASGITransport(app=server.streamable_http_app())
            async with server.session_manager.run():
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="https://mcp.example.test",
                    headers={"Authorization": "Bearer synthetic-token"},
                ) as client:
                    async with streamable_http_client(
                        "https://mcp.example.test/mcp", http_client=client
                    ) as (reader, writer, _):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            context = await session.call_tool(
                                "read_coaching_context",
                                {"days": 1, "end_date": date.today().isoformat()},
                            )
        self.assertEqual({tool.name for tool in tools.tools}, {tool.__name__ for tool in _REMOTE_TOOLS})
        self.assertFalse(context.isError)
        self.assertEqual(context.structuredContent["athlete"]["full_name"], "Synthetic MCP Actor")


if __name__ == "__main__":
    unittest.main()
