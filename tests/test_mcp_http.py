from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


_REPO_ROOT = Path(__file__).resolve().parents[1]


class McpHttpTest(unittest.IsolatedAsyncioTestCase):
    async def test_client_discovers_and_invokes_context_over_local_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_json(
                root / "derived" / "athlete.json",
                {"snapshot_date": "2026-07-18", "full_name": "Synthetic Athlete"},
            )
            self._write_json(
                root / "derived" / "daily" / "2026-07-18.json",
                {"date": "2026-07-18", "training_readiness": None},
            )
            self._write_json(root / "derived" / "activities.json", [])
            self._write_json(
                root / "index" / "collector-health.json",
                {
                    "schema_version": 1,
                    "run": {
                        "outcome": "successful",
                        "attempted_at": "2026-07-18T08:00:00Z",
                        "finished_at": "2026-07-18T08:01:00Z",
                        "last_success_at": "2026-07-18T08:01:00Z",
                    },
                    "domains": {
                        "daily": {
                            "outcome": "successful",
                            "last_success_at": "2026-07-18T08:01:00Z",
                        },
                        "activities": {
                            "outcome": "successful",
                            "last_success_at": "2026-07-18T08:01:00Z",
                        },
                        "plans": {"outcome": "not_attempted"},
                        "profile": {"outcome": "not_attempted"},
                    },
                },
            )

            port = await self._unused_local_port()
            environment = os.environ.copy()
            environment["GARMIN_COACH_DATA_DIR"] = str(root)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "coach.mcp_server",
                "--transport",
                "streamable-http",
                "--port",
                str(port),
                cwd=_REPO_ROOT,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                await self._wait_until_listening(process, port)
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp"
                ) as (reader, writer, _):
                    async with ClientSession(reader, writer) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        result = await session.call_tool(
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

            self.assertEqual(len(tools.tools), 20)
            self.assertFalse(result.isError)
            self.assertEqual(result.structuredContent["window"]["days"], 1)
            self.assertIsNone(
                result.structuredContent["wellness"]["records"][0][
                    "training_readiness"
                ]
            )

    async def _unused_local_port(self) -> int:
        server = await asyncio.start_server(lambda _reader, _writer: None, "127.0.0.1", 0)
        try:
            return server.sockets[0].getsockname()[1]
        finally:
            server.close()
            await server.wait_closed()

    async def _wait_until_listening(
        self, process: asyncio.subprocess.Process, port: int
    ) -> None:
        for _ in range(100):
            if process.returncode is not None:
                _, stderr = await process.communicate()
                self.fail(
                    "HTTP MCP server exited before listening: "
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
        self.fail("HTTP MCP server did not listen within five seconds")

    def _write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
