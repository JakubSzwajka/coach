# Anthropic Claude MCP deployment assessment

> Historical deployment assessment reviewed on 2026-07-19. Statements labelled
> “current” describe that checkpoint, not the repository's present runtime or
> approved storage target. Use `README.md` for current operation and
> [`ADR-0004`](../../adr/0004-postgresql-durable-runtime-authority.md) for the
> accepted PostgreSQL architecture.

## Executive conclusion

Garmin Coach works with Claude locally today. Claude Code and Claude Desktop can launch the existing Python MCP server over stdio; no HTTP listener or hosted deployment is required. Claude Code 2.1.187 is installed on this machine, and the repository-local server has been added at Claude's local project scope and verified as connected.

The loopback Streamable HTTP endpoint is also usable by Claude Code on the same machine, but it is not a Claude.ai deployment. Claude.ai and Claude Desktop custom connectors call remote MCP servers from Anthropic's cloud, so they cannot reach `127.0.0.1` on a user's laptop. A hosted connector requires a public HTTPS endpoint, authentication, user/profile isolation, durable storage, and explicit control over mutable tools.

## Support matrix

| Claude surface | Official attachment | Current status | Missing work |
|---|---|---|---|
| Claude Code, local | stdio: `claude mcp add --transport stdio ...` | **Works now; connected and verified.** | Nothing at the MCP protocol boundary. |
| Claude Code, same-machine HTTP | `claude mcp add --transport http <name> <url>` | Works while the loopback server is running. | Process supervision only; keep it local and unauthenticated. |
| Claude Desktop, local | `claude_desktop_config.json` with `command`, `args`, and `env` | Existing stdio server is compatible. | Manual JSON setup or optional `.mcpb` packaging. |
| Claude.ai custom connector | Public remote Streamable HTTP/SSE connector | **Not deployable yet.** | Public HTTPS, authentication, profile isolation, operations, privacy controls. |
| Claude Desktop remote connector | Same cloud connector infrastructure as Claude.ai | **Not deployable yet.** | Same hosted requirements as Claude.ai. |
| Anthropic Messages API MCP connector | Public URL in `mcp_servers` plus an `mcp_toolset` | **Not deployable yet.** | Public endpoint, caller-managed OAuth token, current beta header, retention decision. |

Sources: [Claude Code MCP](https://code.claude.com/docs/en/mcp), [local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers), [remote custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp), and [Messages API MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector).

## Local Claude Code: works now

Claude Code documents local stdio servers as:

```bash
claude mcp add \
  --scope local \
  --transport stdio \
  --env GARMIN_COACH_DATA_DIR=/ABS/REPO/data \
  garmin-coach -- \
  /ABS/REPO/.venv/bin/python -m coach.mcp_server
```

`local` scope keeps machine-specific paths private to the current project in the user's Claude configuration; `project` scope would create a shareable `.mcp.json`, which is inappropriate for personal absolute paths. See [Claude Code: Connect to tools via MCP](https://code.claude.com/docs/en/mcp).

This machine is already configured. The observed health check is:

```text
garmin-coach:
  Scope: Local config (private to you in this project)
  Status: Connected
  Type: stdio
```

Use `claude mcp get garmin-coach` or `claude mcp list` to recheck it. To remove the local entry:

```bash
claude mcp remove garmin-coach --scope local
```

Stdio is the recommended local path because Claude starts and stops the process automatically; no port, TLS, or local process manager is needed.

## Local Claude Desktop

The official local-server guide documents these configuration files:

- macOS: `$HOME/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Use an absolute interpreter and data path:

```json
{
  "mcpServers": {
    "garmin-coach": {
      "command": "/ABS/REPO/.venv/bin/python",
      "args": ["-m", "coach.mcp_server"],
      "env": {
        "GARMIN_COACH_DATA_DIR": "/ABS/REPO/data",
        "PYTHONPATH": "/ABS/REPO"
      }
    }
  }
}
```

Completely restart Claude Desktop after editing. The local process runs with the user's permissions, so its data directory should remain narrowly scoped. See [MCP: Connect to local servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

Anthropic also documents `.mcpb` Desktop Extensions as an optional packaging format. Packaging would remove manual Python/configuration setup, but it does not create a hosted Claude.ai connector. See [Anthropic: Desktop Extensions](https://www.anthropic.com/engineering/desktop-extensions).

## Local Streamable HTTP

Garmin Coach can run at `http://127.0.0.1:8765/mcp` and Claude Code documents HTTP registration:

```bash
GARMIN_COACH_DATA_DIR=/ABS/REPO/data \
  /ABS/REPO/.venv/bin/python -m coach.mcp_server \
  --transport streamable-http --port 8765

claude mcp add \
  --scope local \
  --transport http \
  garmin-coach-http http://127.0.0.1:8765/mcp
```

This is a same-machine convenience only. The implementation is intentionally fixed to loopback and has no application authentication. The MCP transport specification recommends loopback binding for local servers and requires defensive Origin handling for HTTP. See [MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

Do not enter the loopback URL as a Claude.ai or Claude Desktop custom connector: hosted connector traffic originates from Anthropic's cloud, not the local device. See [Anthropic custom-connector network requirements](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

## What is missing for Claude.ai or a remote Claude deployment

### 1. Public HTTPS Streamable HTTP endpoint

A remote connector needs a stable, publicly reachable HTTPS URL such as `https://mcp.example.com/mcp`. The deployment must support Streamable HTTP POST/GET behavior, MCP protocol-version negotiation, session handling when enabled, Origin validation, and DNS-rebinding protection. Legacy HTTP+SSE remains supported by Claude but is deprecated in favor of Streamable HTTP.

Sources: [Building custom connectors](https://claude.com/docs/connectors/building) and [MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

### 2. Authentication and authorization

The current `FastMCP("garmin-coach")` server has no token verifier or OAuth provider. Publishing it as-is would expose personal health context and mutable App Record tools to any caller that can reach it.

For hosted Claude connectors, Anthropic documents OAuth Dynamic Client Registration, Client ID Metadata Documents, reviewed custom authentication, beta static bearer/API-key headers, and deliberate authless connectors. A health-data service should not use the authless option. Tokens must not be placed in URL query parameters, and the server must validate every request rather than treating an MCP session id as authentication.

Sources: [Claude connector authentication](https://claude.com/docs/connectors/building/authentication), [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization), and [MCP security best practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices).

### 3. Authenticated profile isolation

The current store is deliberately single-profile and file-backed. A remote service needs authenticated subject-to-profile mapping so one athlete can never read or mutate another athlete's records. Collection, app-record mutations, backups, deletion, logs, and audit history must all be scoped to the authenticated profile.

Authentication alone is insufficient: the storage adapter must stop selecting one process-wide `GARMIN_COACH_DATA_DIR` for every request.

### 4. Safe remote tool exposure

The server is Garmin-read-only, but it is not globally read-only: its 20 tools include create, replace, delete, plan activation, adjustment, and fulfilment matching. A remote rollout should initially expose only `read_coaching_context` and targeted read/list/get tools, then deliberately enable writes with per-tool approvals, revision checks, and auditable authorization decisions.

Claude custom connectors and the Messages API `mcp_toolset` support tool selection. Sources: [Custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) and [Messages API MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector).

### 5. Durable operations

A hosted deployment still needs:

- persistent per-profile storage rather than one laptop directory;
- collector scheduling and secure Garmin credential/token storage;
- interprocess mutation safety and concurrency handling;
- backups, deletion, and retention policy;
- process supervision, health checks, structured logs, monitoring, and recovery;
- TLS termination and restricted ingress;
- an explicit stateful-session strategy or intentional stateless HTTP design.

The current FastMCP default is stateful HTTP (`stateless_http=False`), so multiple replicas would require affinity/shared session state or a deliberate stateless cutover. This is an implementation inference from the pinned [FastMCP v1.28.1 source](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/v1.28.1/src/mcp/server/fastmcp/server.py) and the [MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

### 6. Privacy and Anthropic API retention decision

Anthropic's Messages API MCP connector is currently not eligible for Zero Data Retention or HIPAA use; exchanged tool definitions and results follow the standard retention policy. Health-data use therefore needs an explicit privacy, retention, and contractual decision before selecting the API connector.

Sources: [Messages API MCP connector data retention](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector#data-retention) and [Anthropic API and data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention).

## Messages API specifics

Anthropic's current beta uses header `mcp-client-2025-11-20`; the earlier `mcp-client-2025-04-04` version is deprecated. The request supplies a public URL server in `mcp_servers` and an `mcp_toolset` in `tools`. Only MCP tool calls are supported by this connector; local stdio cannot be attached directly, and the API caller is responsible for OAuth token acquisition and refresh.

See [Anthropic Messages API MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector).

## Recommended sequence

1. **Use Claude Code locally over stdio now.** This path is configured and connected.
2. **Add Claude Desktop local stdio only if needed.** Optionally package an `.mcpb` later.
3. **Keep loopback HTTP experimental and local.** It adds no value for Claude.ai by itself.
4. **Design remote reads before remote writes.** Establish authenticated profile isolation, read-tool allowlisting, retention, and audit boundaries.
5. **Deploy a public HTTPS Streamable HTTP service with OAuth.** Verify it with MCP Inspector and Claude Code before enabling hosted connectors.
6. **Attach Claude.ai/Desktop through Custom Connectors.** Enable write tools only after authorization and audit behavior are proven.
7. **Use the Messages API connector only for an explicit API workload** after accepting its authentication and retention model.

## Primary sources

1. [Claude Code: Connect to tools via MCP](https://code.claude.com/docs/en/mcp)
2. [MCP: Connect to local servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
3. [MCP: Connect to remote servers](https://modelcontextprotocol.io/docs/develop/connect-remote-servers)
4. [Anthropic Support: custom connectors using remote MCP](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
5. [Claude developer docs: Building custom connectors](https://claude.com/docs/connectors/building)
6. [Claude developer docs: Connector authentication](https://claude.com/docs/connectors/building/authentication)
7. [Anthropic Platform: Messages API MCP connector](https://platform.claude.com/docs/en/agents-and-tools/mcp-connector)
8. [MCP specification: transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
9. [MCP specification: authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
10. [MCP security best practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)
11. [Anthropic: Desktop Extensions](https://www.anthropic.com/engineering/desktop-extensions)
12. [Anthropic API and data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention)
13. [Official MCP Python SDK v1.28.1 FastMCP source](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/v1.28.1/src/mcp/server/fastmcp/server.py)
