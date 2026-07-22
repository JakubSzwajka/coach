"""Test-only adapter for portable legacy MCP DTO contract tests."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from coach.data import CoachData


@contextmanager
def legacy_mcp_adapter(root: str | Path):
    with patch(
        "coach.mcp_server.current_application_adapter",
        return_value=CoachData(Path(root)),
    ):
        yield
