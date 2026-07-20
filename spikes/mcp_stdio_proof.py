#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp==1.28.1"]
# ///
"""Throwaway proof that the official MCP SDK works over local stdio."""

from mcp.server.fastmcp import FastMCP

server = FastMCP("garmin-coach-stdio-proof")


@server.tool()
def prove_stdio_round_trip(message: str) -> dict[str, str]:
    """Return a deterministic synthetic payload to prove one MCP tool call."""
    return {
        "fixture": "synthetic-only",
        "message": message,
        "transport": "stdio",
    }


if __name__ == "__main__":
    server.run(transport="stdio")
