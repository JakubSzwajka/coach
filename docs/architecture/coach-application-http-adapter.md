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

The initial public messages are Profile reads/provisioning/display-name updates
and safe collection summaries. Source payloads, source keys, encrypted
credential/token envelopes, database identifiers, table names, and SQL are not
HTTP representations.

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
Consumer wiring and removal of the pre-cutover file adapters are separate DAG
steps; this contract does not switch a consumer early.
