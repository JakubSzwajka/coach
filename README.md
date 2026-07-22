# coach

A personal running/fitness data platform over collected and app-authored
training data: collect once, use everywhere. Garmin access is **read-only** and
all output is fitness guidance, not medical advice. See [`VISION.md`](VISION.md).

## Runtime architecture

PostgreSQL is the sole runtime authority:

```text
Garmin Connect -> collector -> CoachApplication.ingest/execute -> PostgreSQL
local MCP ------------------> CoachApplication.read/execute ----^
remote MCP (read-only) -----> CoachApplication.read ------------^
Next.js -> private Python HTTP adapter -> CoachApplication -----^
```

`CoachApplication` owns actor-to-Profile resolution, isolation, validation,
optimistic revisions, source read-only rules, transactions, collection jobs,
and safe projections. Runtime producers and consumers have no data-directory
read, write, dual-write, or fallback path. A PostgreSQL outage therefore fails
closed.

The private HTTP adapter is persistent and service-authenticated. Next.js uses
one server-only typed client; it receives no data mount, PostgreSQL URL,
encryption key, table access, or raw payload access. Browser requests never
contain a Profile id or Clerk actor. Protected Next routes derive the Clerk
subject with `auth()` and pass it over the authenticated private hop.

Legacy files are permitted only as frozen migration input, export, backup, or
rollback material under the separately owner-gated private-data cutover. This
change does not inspect or import them. See
[`ADR-0004`](docs/adr/0004-postgresql-durable-runtime-authority.md).

## One local stack

### 1. Install and configure

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd web && corepack enable && pnpm install --frozen-lockfile && cd ..

cp .env.example .env
cp web/.env.example web/.env
```

Replace placeholders locally. Generate distinct database passwords, a private
service token, and a Fernet key; never commit the resulting files:

```bash
openssl rand -hex 32
.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

For Compose, the two database URLs in `.env` use host `postgres`. Put Clerk's
publishable and secret keys only in `web/.env`. The root service token must
match the server-only value injected into Next; it must not use a
`NEXT_PUBLIC_` name.

### 2. Start

```bash
docker compose up --build --wait
```

This is the supported clean-start topology. PostgreSQL must become healthy,
the one-shot `migrate` service applies every migration, the private adapter
must pass its PostgreSQL health check, and only then does Next start. Open
<http://localhost:3000>.

Do not run a separate legacy web command and do not mount `data/` into Next.

### 3. Sign in and connect

1. Sign in through the configured Clerk application.
2. Select **Connect Garmin**.
3. Enter Garmin credentials and the initial history length.
4. Watch the header status. It distinguishes:
   - credentials stored;
   - authenticating;
   - initial sync running;
   - first successful sync;
   - degraded/stale prior data after a failed refresh;
   - `needs_reconnect` after repairable authentication failure.
5. Reload after completion. Dashboard, activities, and trends contain only safe
   typed projections; source payloads and raw tables are never exposed.

Connect stores encrypted credentials and creates a durable `initial_sync` job.
The adapter runs it through `CoachApplication.execute`; parsed source responses
flow in memory to `CoachApplication.ingest`. Garmin MFA is intentionally
noninteractive and transitions the source to `needs_reconnect`.

### 4. Refresh

Select **Refresh Garmin** while signed in. The protected route derives the same
Clerk actor server-side and creates a durable `incremental` job. Only one job
per Profile runs at a time. A failed refresh keeps prior authoritative records
and reports them as degraded/stale; it never fabricates freshness.

### 5. Stop

```bash
docker compose down
```

This preserves the PostgreSQL volume. Never use `down --volumes` on a populated
runtime. Backup/restore and private file cutover are owned by separate work.

## Troubleshooting

```bash
docker compose ps
docker compose logs migrate
docker compose logs adapter
docker compose logs web
```

- **`migrate` failed:** verify the admin migration URL uses host `postgres` and
  matches the new volume's initialized admin password.
- **Adapter unhealthy:** verify the application URL, service token, Clerk
  issuer, and Fernet key are present. Adapter errors are stable/redacted and do
  not include database URLs, subjects, credentials, payloads, or exception
  details.
- **Web reports application unavailable:** confirm `adapter` is healthy and the
  server-only service token matches. PostgreSQL outage intentionally has no
  file fallback.
- **Needs reconnect:** submit current Garmin credentials through **Reconnect
  Garmin**. Do not copy token-cache files into the repository.
- **No data after connect:** inspect the safe signed-in status route and adapter
  job state. Missing remains unknown; the UI does not infer a successful sync
  from old records.
- **Password changed on an existing PostgreSQL volume:** environment changes do
  not rotate roles. Use an authenticated PostgreSQL `ALTER ROLE`; never destroy
  a populated volume as a rotation shortcut.

## Collector CLI

The web path is the normal local operator flow. A deployment-local scheduled
collector may run the PostgreSQL entry point directly with only the application
URL, encryption key, explicitly configured local actor, and (for initial
connect/reconnect only) Garmin credentials:

```bash
GARMIN_COACH_DATABASE_URL='postgresql://coach:<app-password>@127.0.0.1:5432/coach' \
GARMIN_COACH_LOCAL_ACTOR_ISSUER='https://local.example.test' \
GARMIN_COACH_LOCAL_ACTOR_SUBJECT='local-owner-placeholder' \
GARMIN_COACH_ENCRYPTION_KEY='<fernet-key>' \
GARMIN_EMAIL='you@example.com' \
GARMIN_PASSWORD='<garmin-password>' \
.venv/bin/python -m collector.collect --days 30 --reconnect
```

Later runs omit only Garmin credentials and use cached encrypted tokens in
PostgreSQL:

```bash
GARMIN_COACH_DATABASE_URL='postgresql://coach:<app-password>@127.0.0.1:5432/coach' \
GARMIN_COACH_LOCAL_ACTOR_ISSUER='https://local.example.test' \
GARMIN_COACH_LOCAL_ACTOR_SUBJECT='local-owner-placeholder' \
GARMIN_COACH_ENCRYPTION_KEY='<fernet-key>' \
.venv/bin/python -m collector.collect
```

The command never reads or writes the legacy data directory.

## MCP

### Local: 20-tool read/App Record surface

Local stdio and loopback HTTP retain the established 20 tools and their revision
checks. Configure an explicit owner actor and the non-superuser application
URL:

```toml
[mcp_servers.garmin_coach]
command = ".venv/bin/python"
args = ["-m", "coach.mcp_server"]
env = {
  GARMIN_COACH_DATABASE_URL = "postgresql://coach:<app-password>@127.0.0.1:5432/coach",
  GARMIN_COACH_LOCAL_ACTOR_ISSUER = "https://local.example.test",
  GARMIN_COACH_LOCAL_ACTOR_SUBJECT = "local-owner-placeholder"
}
```

For loopback Streamable HTTP:

```bash
.venv/bin/python -m coach.mcp_server --transport streamable-http --port 8765
```

The local HTTP listener is unauthenticated and loopback-only. Never proxy it.
Collected Training Sessions remain read-only; App Record replace/delete
commands require the last-read revision.

### Remote: authenticated read-only surface

The Clerk-protected remote server exposes exactly eight reads:
`read_coaching_context`, Training Session list/get, Goal Event list/get, and
Training Plan get/list/history. App Record mutations are absent from its tool
registry. Every verified Clerk subject resolves through `CoachApplication`; no
tool argument selects a Profile.

Required configuration is the application database URL plus:

```dotenv
GARMIN_COACH_MCP_PUBLIC_URL=https://mcp.example.com/mcp
GARMIN_COACH_CLERK_ISSUER=https://example.clerk.accounts.dev
GARMIN_COACH_CLERK_SECRET_KEY=sk_test_example
```

Start behind an HTTPS reverse proxy:

```bash
.venv/bin/python -m coach.remote_mcp_server --port 8765
```

## Verification

```bash
# Database-free legacy/domain contracts and adapter unit tests, scrubbed env
TEST_HOME="$(mktemp -d)"
env -i PATH="$PATH" HOME="$TEST_HOME" TMPDIR="${TMPDIR:-/tmp}" \
  PYTHONPATH="$PWD" PYTHONDONTWRITEBYTECODE=1 \
  GARMIN_COACH_DISABLE_DOTENV=1 \
  .venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
rm -rf "$TEST_HOME"

# Non-skipping disposable real-PostgreSQL authority/cutover/MCP/HTTP gate
scripts/test-postgres

cd web
pnpm lint
pnpm typecheck
pnpm build
```

The PostgreSQL gate proves collector ingest, local and remote MCP policy and
reads, representative App Record mutations, stale revisions, actor isolation,
service-auth failure, server-side actor derivation, raw non-exposure, and
outage fail-closed behavior with synthetic fixtures only.

## Repository layout

```text
coach/application.py          authoritative deep application module
coach/http_adapter.py         private service-authenticated Next adapter
coach/application_adapter.py  in-process MCP translation
coach/postgres/               migrations and private persistence helpers
collector/postgres_adapter.py in-memory read-only Garmin adapter
collector/collect.py          durable PostgreSQL collection entry point
web/src/lib/coach-client.ts   only server-side web data client
```

The repository is public. Never add credentials, tokens, collected health data,
real identifiers, device serials, or absolute personal paths.
