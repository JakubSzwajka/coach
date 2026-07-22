# Private HTTP adapter contract

The persistent private Python HTTP adapter for Next.js is an adapter at the
`CoachApplication` seam. It does not define a second domain interface.

## Request contract

1. Authenticate the private transport and verify the Clerk token before calling
   the application module.
2. Construct `ClerkActor` only from the verified issuer and subject. Never accept
   a Profile id or `ProfileRef` from an HTTP body, query string, or header.
3. Decode only an explicit allowlist of public query and command message types.
   Call `CoachApplication.read` or `CoachApplication.execute` in the persistent
   Python process.
4. Do not expose `CoachApplication.ingest` over HTTP. Ingest is an internal
   collector interface and receives opaque capabilities issued inside Python.

The initial public messages are Profile reads/provisioning/display-name updates,
dashboard/trend/Training Session projections, safe collection status, explicit
App Record commands, and Connect/Refresh collection requests. Source payloads,
source keys, encrypted credential/token envelopes, database identifiers, table
names, and SQL are not HTTP representations.

The deployed adapter accepts `Authorization: Bearer <service token>` plus one
`X-Garmin-Coach-Clerk-Subject` header from the server-only Next client. The
adapter pins the Clerk issuer from its own configuration. The service token and
actor header are never browser representations: protected Next routes call
Clerk `auth()` and construct the private request. Bodies and query strings use
strict allowlists and reject actor, subject, and Profile selection fields.

Connect encrypts credentials and requests a durable `initial_sync` job; Refresh
requests `incremental`; Status reports only safe phases (`credentials_stored`,
`authenticating`, `syncing`, `first_sync_complete`, `connected`,
`degraded`, `degraded_stale`, and `needs_reconnect`). Collection runs inside the
persistent Python service through one database-leased worker (never one thread
per request). The adapter sends a fixed, non-Profile-selecting service actor and
a private run-next command to `CoachApplication.execute`; the application owns
the worker lease, pending-job selection, stored Clerk actor reconstruction, job
capability reconstruction, recovery, and persistence. The private command is
not accepted by the HTTP command decoder or registered as an MCP tool, and its
result reports only whether work was found—never Profile data or capabilities.
PostgreSQL admits at most one requested/running job per Profile/source;
duplicate Connect/Refresh requests return `409 conflict`. On startup the worker
drains durable requested jobs and terminalizes stranded running evidence as
`worker_lost` while the process-independent worker lease remains held. Parsed
Garmin responses reach PostgreSQL only through `CoachApplication.ingest`, in the
same transaction as checkpoint and successful job completion.

## Response and failure contract

Serialize explicit view fields rather than dataclass internals. In particular,
opaque Profile/source capabilities are not serialized. Map stable application
errors without forwarding exception details:

| Application error | HTTP meaning |
| --- | --- |
| `InvalidRequest` | invalid allowlisted request |
| `NotFound` | actor-owned record is unavailable |
| `AccessDenied` | record is unavailable (same external shape as not found) |
| `StaleRevision` | optimistic revision conflict |
| `Conflict` | current-state conflict |
| `ApplicationUnavailable` | temporary PostgreSQL/application unavailability |

Responses and logs must not contain Clerk subjects, collected payloads, source
keys, ciphertext, database URLs, or exception details. The adapter starts only
with valid `GARMIN_COACH_DATABASE_URL` configuration and has no filesystem
fallback or dual-write path.

MCP remains an in-process adapter over the same three application operations.
The collector, MCP registries, and web routes now use those adapters together;
legacy file modules are not runtime entry points and no consumer falls back.
