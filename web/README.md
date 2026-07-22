# Garmin Coach web

Signed-in Next.js views over safe PostgreSQL projections. Next never reads a
data directory or database and never receives the encryption key. Its only data
seam is the typed, server-only `src/lib/coach-client.ts` client to the private
Python adapter.

## Configuration

Copy `web/.env.example` to `web/.env` for local development:

- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` — Clerk's browser-safe publishable key.
- `CLERK_SECRET_KEY` — Clerk server key.
- `GARMIN_COACH_HTTP_URL` — private adapter origin (server only).
- `GARMIN_COACH_HTTP_SERVICE_TOKEN` — private service credential (server only).

Only the Clerk publishable key is public. Never use a `NEXT_PUBLIC_` prefix for
the adapter URL/token. Do not provide a database URL, encryption key, Profile
id, data mount, or raw-table credential to Next.

## Run

Use the one root topology:

```bash
docker compose up --build --wait
```

Migrations and adapter health complete before Next starts. Open
<http://localhost:3000>, sign in with Clerk, select **Connect Garmin**, follow
credentials stored → authenticating → first sync, and use **Refresh Garmin**
for later durable incremental jobs. Degraded/stale and `needs_reconnect` remain
explicit. Stop with `docker compose down` (without `--volumes`).

For frontend-only development, first run PostgreSQL migrations and
`coach.http_adapter`, then:

```bash
cd web
pnpm dev
```

## Security and tenancy

`src/proxy.ts` protects all pages and API routes. Connect, Refresh, Status, and
server-rendered reads call Clerk `auth()` server-side. The browser sends neither
a Profile id nor a subject; the typed client attaches the server-derived Clerk
subject only on the authenticated private hop. The Python application resolves
that actor to its one Profile and enforces cross-profile isolation.

Dashboard, activities, and trends contain explicit projection fields only.
They cannot return source payloads, source keys, ciphertext, table names, SQL,
or opaque Profile/source capabilities. PostgreSQL or adapter outage returns a
stable unavailable error and never falls back to files.
