from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from coach.profiles import ProfileRegistry

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REMOTE_TOOLS = {
    "read_coaching_context",
    "create_training_session",
    "list_training_sessions",
    "get_training_session",
    "replace_training_session",
    "delete_training_session",
    "create_goal_event",
    "list_goal_events",
    "get_goal_event",
    "replace_goal_event",
    "delete_goal_event",
    "create_training_plan",
    "get_training_plan",
    "list_training_plans",
    "get_training_plan_history",
    "activate_training_plan",
    "archive_training_plan",
    "delete_training_plan",
    "adjust_training_plan",
    "set_planned_session_fulfilment",
}


class RemoteMcpProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_user_discovers_profile_scoped_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = ProfileRegistry(root).bind("user_owner")
            data_root = ProfileRegistry(root).data_root(profile.id)
            self._write_json(
                data_root / "derived" / "athlete.json",
                {"snapshot_date": "2026-07-18", "full_name": "Synthetic Athlete"},
            )
            self._write_json(
                data_root / "derived" / "daily" / "2026-07-18.json",
                {"date": "2026-07-18", "training_readiness": None},
            )
            self._write_json(data_root / "derived" / "activities.json", [])

            port = await self._unused_local_port()
            public_url = "https://mcp.example.test/mcp"
            local_url = f"http://127.0.0.1:{port}/mcp"
            token = "oauth-access-token"
            clerk_api_server = self._serve_token_verifier(token)
            issuer = "https://clerk.example.test"
            clerk_api_url = (
                f"http://127.0.0.1:{clerk_api_server.server_port}"
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "GARMIN_COACH_DATA_DIR": str(root),
                    "GARMIN_COACH_MCP_PUBLIC_URL": public_url,
                    "GARMIN_COACH_CLERK_ISSUER": issuer,
                    "GARMIN_COACH_CLERK_SECRET_KEY": "sk_test_secret",
                    "GARMIN_COACH_CLERK_API_URL": clerk_api_url,
                }
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "coach.remote_mcp_server",
                "--port",
                str(port),
                cwd=_REPO_ROOT,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                await self._wait_until_listening(process, port)
                async with httpx.AsyncClient(
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Host": "mcp.example.test",
                        "Origin": "https://untrusted.example.test",
                    }
                ) as invalid_origin_client:
                    rejected_origin = await invalid_origin_client.post(
                        local_url,
                        json={},
                    )
                async with httpx.AsyncClient(
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Host": "mcp.example.test",
                    }
                ) as http_client:
                    async with streamable_http_client(
                        local_url, http_client=http_client
                    ) as (reader, writer, _):
                        async with ClientSession(reader, writer) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            context = await session.call_tool(
                                "read_coaching_context",
                                {"days": 1, "end_date": "2026-07-18"},
                            )
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                stdout, stderr = await process.communicate()
                clerk_api_server.shutdown()
                clerk_api_server.server_close()

            self.assertEqual(rejected_origin.status_code, 403)
            self.assertNotIn("www-authenticate", rejected_origin.headers)
            self.assertEqual({tool.name for tool in tools.tools}, _REMOTE_TOOLS)
            self.assertFalse(context.isError)
            self.assertEqual(context.structuredContent["window"]["days"], 1)
            logs = stdout + stderr
            self.assertNotIn(token.encode(), logs)
            self.assertNotIn(b"Synthetic Athlete", logs)
            self.assertIn(
                b"remote_tool tool=read_coaching_context decision=allowed subject=",
                logs,
            )
            self.assertIn(b"latency_ms=", logs)

    @staticmethod
    def _serve_token_verifier(expected_token: str) -> ThreadingHTTPServer:
        response_payload = json.dumps(
            {
                "object": "clerk_idp_oauth_access_token",
                "id": "oat_test",
                "client_id": "oauth_client",
                "subject": "user_owner",
                "scopes": ["profile", "offline_access"],
                "revoked": False,
                "revocation_reason": None,
                "expired": False,
                "expiration": 1_800_000_000,
                "created_at": 1_700_000_000,
                "updated_at": 1_700_000_000,
            }
        ).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                content_length = int(self.headers.get("Content-Length", "0"))
                request_payload = json.loads(self.rfile.read(content_length))
                if (
                    self.path
                    != "/oauth_applications/access_tokens/verify"
                    or self.headers.get("Authorization")
                    != "Bearer sk_test_secret"
                    or request_payload != {"access_token": expected_token}
                ):
                    self.send_error(401)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header(
                    "Content-Length",
                    str(len(response_payload)),
                )
                self.end_headers()
                self.wfile.write(response_payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    @staticmethod
    async def _unused_local_port() -> int:
        server = await asyncio.start_server(lambda _reader, _writer: None, "127.0.0.1", 0)
        try:
            return server.sockets[0].getsockname()[1]
        finally:
            server.close()
            await server.wait_closed()

    def _write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    async def _wait_until_listening(
        self, process: asyncio.subprocess.Process, port: int
    ) -> None:
        for _ in range(200):
            if process.returncode is not None:
                _, stderr = await process.communicate()
                self.fail(
                    "Remote MCP server exited before listening: "
                    + stderr.decode("utf-8", errors="replace")
                )
            try:
                _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.05)
                continue
            writer.close()
            await writer.wait_closed()
            return
        self.fail("Remote MCP server did not listen within ten seconds")


if __name__ == "__main__":
    unittest.main()
