# coach

A personal running/fitness **data platform** over your own collected and
app-authored training data: collect once, use everywhere. A collector imports
Garmin Connect read-only; a Next.js web app and MCP adapters consume the record;
and (in stages) an AI coach reasons over it.

See [`VISION.md`](VISION.md) for the north star, staged ambition, and guardrails.

> **Guardrails:** the project is **read-only** to Garmin (it never writes back
> to your account), and any coaching output is training guidance, **not medical
> advice**.

## Architecture direction

M1 makes PostgreSQL the sole durable runtime authority behind one deep Python
`CoachApplication`:

```
Garmin Connect ──▶ collector ──▶ CoachApplication.ingest ──▶ PostgreSQL
MCP ───────────────────────────▶ CoachApplication.read/execute ──┘
Next.js ──▶ private authenticated HTTP adapter ──────────────────┘
```

PostgreSQL owns Profiles, operational collection state, immutable parsed source
captures, canonical and App Records, and projections. MCP is an in-process
adapter; Next.js does not query raw tables or duplicate authoritative Python
validation. Files are migration, export, backup, frozen rollback, or temporary
secret-hydration material—not a second runtime authority. See
[`ADR-0004`](docs/adr/0004-postgresql-durable-runtime-authority.md).

The commands below describe the **current pre-cutover file-backed baseline**:

```
Garmin Connect ──▶ collector ──▶ data/raw/      (parsed endpoint snapshots)
                                 data/derived/  (compact, coach-friendly)
                                       │
                                       ├──▶ web/   (Next.js dashboard, trends, activities)
                                       └──▶ coach  (MCP context and App Records)
```

M1 switches all producers and consumers together; it does not dual-write. The
private-data cutover remains separately owner-gated.

## Requirements

- Python 3.10+
- A Garmin Connect account (the collector logs in as you)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then fill in your Garmin credentials
```

Credentials are loaded from `.env` automatically and are only used on the rare
re-login—auth tokens are cached in `~/.garminconnect` afterwards. Your `.env`
and all collected `data/` are gitignored and never committed.

## Collect data

```bash
python -m collector.collect                 # yesterday + today + new activities
python -m collector.collect --days 30       # backfill last 30 days
python -m collector.collect --date 2026-07-10
python -m collector.collect --weekly        # also refresh the profile snapshot
```

Collection is idempotent (no gaps, no duplicates) and safe to run on a cron.
See [`collector/README.md`](collector/README.md) for the data layout and cron
setup.

## Connect an MCP client

The production MCP server exposes one read-only context tool,
`read_coaching_context`, an app-owned Training Session lifecycle
(`create_training_session`, `list_training_sessions`, `get_training_session`,
`replace_training_session`, `delete_training_session`), and an app-owned Goal
Event lifecycle (`create_goal_event`, `list_goal_events`, `get_goal_event`,
`replace_goal_event`, `delete_goal_event`), and an app-owned Training Plan
lifecycle (`create_training_plan`, `get_training_plan`, `list_training_plans`,
`get_training_plan_history`, `activate_training_plan`,
`archive_training_plan`, `delete_training_plan`, `adjust_training_plan`,
`set_planned_session_fulfilment`). Reads return a bounded
athlete snapshot, recent wellness, machine-readable collector
health/freshness, and recent Training Sessions. Missing files and source
values remain explicit as `null` or `missing_dates`; the server never writes
to Garmin or `data/derived/`.

Activate the virtual environment, start Codex from the repository root, and add
this Codex configuration:

```toml
[mcp_servers.garmin_coach]
command = "python"
args = ["-m", "coach.mcp_server"]
env = { GARMIN_COACH_DATA_DIR = "data" }
```

`python` resolves through the activated virtual environment on macOS, Linux,
and Windows. Keep the client rooted in this repository so `data` resolves.
Other MCP clients use the equivalent command, arguments, working-directory, and
environment fields. `GARMIN_COACH_DATA_DIR` is optional and defaults to `data`;
set it to a different local file-backed store for synthetic fixtures.

### Local HTTP transport

Stdio remains the default. To keep a local Streamable HTTP endpoint running,
start the server explicitly:

```bash
GARMIN_COACH_DATA_DIR=data .venv/bin/python -m coach.mcp_server \
  --transport streamable-http \
  --port 8765
```

Connect an HTTP-capable MCP client to `http://127.0.0.1:8765/mcp`. For OMP:

```json
{
  "mcpServers": {
    "garmin_coach": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "timeout": 120000
    }
  }
}
```

The HTTP listener is intentionally fixed to loopback. It has no authentication,
so do not proxy it, bind it to a LAN interface, or expose it publicly.

### Clerk-protected remote HTTP transport

The public-facing server is a separate process protected by Clerk OAuth. Every
verified Clerk subject is resolved to one isolated Profile and receives the
same 20 read/App Record tools as the local MCP surface; no caller can select a
profile id. Restrict who may join or sign in through the Clerk application
configuration. The server is fixed to loopback, so a production deployment must
put an HTTPS reverse proxy in front.

Copy the example configuration once, then fill in the Clerk values. The server
loads `.env` automatically and explicit process environment variables take
precedence:

```bash
cp .env.example .env
```

Required remote-server entries:

```dotenv
GARMIN_COACH_DATA_DIR=data
GARMIN_COACH_MCP_PUBLIC_URL=https://mcp.example.com/mcp
GARMIN_COACH_CLERK_ISSUER=https://example.clerk.accounts.dev
GARMIN_COACH_CLERK_SECRET_KEY=sk_test_example
```

Then start the protected server:

```bash
.venv/bin/python -m coach.remote_mcp_server --port 8765
```

Keep `.env` local; it is gitignored. OAuth clients register through Clerk DCR,
so users do not configure a client ID manually. The server publishes RFC 9728
protected-resource metadata, validates token status through Clerk's Backend
API, and records only redacted tool audit events. Local development may use
loopback HTTP; non-loopback resource and issuer URLs must use HTTPS.

The tool accepts `days` from 1 through 90 (default 14) and an optional ISO
`end_date`. Collection health is reported from
`data/index/collector-health.json`; file presence, log text, and
`state.last_run` are never treated as successful collection.

Manual (non-Garmin) sessions are app-owned records stored under `data/app/`.
`create_training_session` requires `sport` and an ISO `local_date`; timing
precision is explicit (`date_only`, or `local_datetime` when a `local_start`
is given), `session_rpe` is an optional integer 1–10, and each load carries
`value`, `unit`, `method`, and `source`. Every create makes a new opaque id;
there is no upsert or automatic duplicate matching. `replace_training_session`
and `delete_training_session` require the last-read `expected_revision` and
reject stale revisions. Garmin-derived sessions (ids prefixed `garmin:`) are
read-only and cannot be mutated through these tools.

Goal Events are app-owned calendar records stored under
`data/app/goal_events/`. `create_goal_event` requires a `name`, an ISO
`local_date`, a `sport`, and a canonical `priority` (`primary`, `secondary`,
or `practice`); `status` is `scheduled` (default), `completed`, or
`cancelled`. Timing precision is explicit (`date_only`, or `local_datetime`
when a `local_start` is given), and `distance`, a `goal`
(`{target_duration: {value, unit: "seconds"}, statement}`), and an `outcome`
(`{actual_duration, statement}`) stay unknown until supplied. A goal is a
recorded target, never a prediction; recording an outcome never creates a
Training Session and never changes the lifecycle. `list_goal_events` returns
every event soonest-first with optional `sport`/`status` filters.
`replace_goal_event` and `delete_goal_event` require the last-read
`expected_revision`; a cancellation is just a revisioned edit, and priority
never triggers automatic periodization.

Training Plans are revisioned app-owned records stored under
`data/app/plans/`. `create_training_plan` requires a `name`, inclusive
`starts_on`/`ends_on` dates, and a `reason`; it starts as `draft` and may
carry advisory `constraints`, `goal_events`
(`{goal_event_id, goal_event_revision}` at the event's current revision), and
dated `planned_sessions`. Plan and Planned Session ids are adapter-generated.
`activate_training_plan` allows at most one active plan and rejects a stale
goal reference or a still-`scheduled` prescription dated before activation;
`archive_training_plan` is terminal; `delete_training_plan` only removes a
never-active draft without fulfilment history. `adjust_training_plan` appends
an immutable, reasoned Plan Revision from an `effective_from` date and may
`add`, `update`, or `cancel` only Planned Sessions on or after it — earlier
prescriptions stay frozen forever. `set_planned_session_fulfilment` records an
audited `scheduled`/`fulfilled`/`skipped` disposition and explicit Training
Session `matches` (a device split may attach several; each Training Session
fulfils at most one Planned Session) without altering a prescription, even on
past or archived history. `get_training_plan_history` returns every revision
oldest-first. Matching is explicit; nothing is auto-matched, completed
Training Sessions stay read-only, and Garmin is never written.

## Web dashboard

A Next.js app under [`web/`](web/) presents the store — dashboard, trends, and
activities — reading `data/derived/` **server-side**, so the browser never sees
the raw files. Local development:

```bash
cd web
pnpm install
cp .env.example .env          # then add your Clerk keys
GARMIN_COACH_DATA_DIR=../data pnpm dev   # http://localhost:3000
```

Or run it containerised from the repo root (mounts `data/` read-write so the
collector can update the signed-in profile):

```bash
docker compose up --build          # http://localhost:3000
```

Access requires a signed-in Clerk user — mandatory auth via Next proxy, using
the same Clerk application as the MCP server. Keys load from `web/.env`
(`NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY`). The header can bind
the current Clerk user to Garmin credentials, run an initial background
backfill, and trigger later incremental refreshes; credentials are encrypted
before storage and never enter browser storage. Set `GARMIN_COACH_SECRET_KEY`
from secret injection to keep key material outside the data volume. Legacy flat
data is never assigned by first-login order. Garmin MFA must be disabled in this
pre-cutover implementation. Data is read at request time, so a reload reflects
the latest successful collector run. See
[`web/README.md`](web/README.md) for profile resolution and job details.

## Layout

```
collector/   Python package: idempotent Garmin pull → raw + derived store
coach/       CoachData adapter (read context + app-owned sessions & goal events) + stdio MCP
web/         Next.js dashboard (server-reads data/derived/) + Dockerfile
data/        collected + app data (gitignored: raw/, derived/, index/, logs/, app/)
docs/        knowledge/, agents/ (agent config), adr/ (as decisions land)
spikes/      throwaway experiments (auth smoke test)
VISION.md    project direction and guardrails
AGENTS.md    how coding agents work in this repo
```

## Working with agents

This repo is set up for AI coding agents — issue tracking, triage labels, and
domain docs are described in [`AGENTS.md`](AGENTS.md) and `docs/agents/`.

## License

[MIT](LICENSE) © 2026 Jakub Szwajka
