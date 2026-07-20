# Python stdio MCP walking skeleton — throwaway proof

Question answered: a Python server built with the official MCP SDK can be discovered and invoked locally by a real MCP-capable AI client over stdio.

This spike is deliberately synthetic. It reads no Garmin data, accepts no credentials, and defines no permanent coach or storage contract.

## Artifact

- `mcp_stdio_proof.py` — one `FastMCP` server and one deterministic tool; `uv` installs the pinned `mcp==1.28.1` dependency from the inline script metadata.
- `mcp_stdio_proof.codex.toml` — exact Codex MCP server configuration, relative to the repository root.

To keep the configuration permanently, merge the TOML fragment into Codex's user configuration. To try it without changing user configuration, run from the repository root:

```bash
codex mcp \
  -c 'mcp_servers.garmin_coach_stdio_proof.command="uv"' \
  -c 'mcp_servers.garmin_coach_stdio_proof.args=["run","spikes/mcp_stdio_proof.py"]' \
  list
```

Observed discovery row:

```text
Name                      Command  Args
 garmin_coach_stdio_proof  uv       run spikes/mcp_stdio_proof.py
```

A normal interactive client invocation uses the same overrides:

```bash
codex \
  -c 'mcp_servers.garmin_coach_stdio_proof.command="uv"' \
  -c 'mcp_servers.garmin_coach_stdio_proof.args=["run","spikes/mcp_stdio_proof.py"]' \
  'Use the garmin_coach_stdio_proof MCP server. Call prove_stdio_round_trip exactly once with message walking-skeleton-ok. Return only the MCP tool result.'
```

## Observed call evidence

The local non-interactive Codex run used the same two configuration overrides, an ephemeral session, and the controlled synthetic server. The client emitted this completed MCP event (unrelated session metadata omitted):

```json
{
  "type": "mcp_tool_call",
  "server": "garmin_coach_stdio_proof",
  "tool": "prove_stdio_round_trip",
  "arguments": {
    "message": "walking-skeleton-ok"
  },
  "result": {
    "structured_content": {
      "fixture": "synthetic-only",
      "message": "walking-skeleton-ok",
      "transport": "stdio"
    }
  },
  "status": "completed"
}
```

This proves the integration seam only: process launch, MCP initialization/tool discovery, argument-schema exposure, invocation, and structured result return over stdio.

## Primary sources

- [Official Python MCP SDK v1.28.1](https://github.com/modelcontextprotocol/python-sdk/tree/v1.28.1)
- [Official Codex MCP configuration](https://developers.openai.com/codex/mcp)
