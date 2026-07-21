# Garmin Coach — web

Next.js dashboard over the collector's file store. Reads `data/derived/`
**server-side** (App Router server components / `node:fs`), so raw files are
never exposed to the browser, and gates all access behind Clerk auth.

## Run

```bash
pnpm install
cp .env.example .env                      # add your Clerk keys
GARMIN_COACH_DATA_DIR=../data pnpm dev    # http://localhost:3000
```

Next.js loads `.env` / `.env.local` automatically. Or containerised from the
repo root: `docker compose up --build` (reads `web/.env`, mounts `data/`
read-write for collection and encrypted credentials).

### Environment

- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY` — Clerk API keys
  (same Clerk application as the MCP server). Required; access is signed-in only.
- `GARMIN_COACH_DATA_DIR` — store root (the directory that contains `derived/`).
  Defaults to `../data`; the container sets `/data`.
- `GARMIN_COACH_SECRET_KEY` — optional persistent Fernet key. Supply it through
  secret injection to keep key material outside the data volume. If omitted,
  the pre-cutover file implementation generates a `0600` key at
  `<data-root>/secret.key`, which does not protect against theft of that entire
  volume.

## Auth

`src/proxy.ts` runs `clerkMiddleware` + `auth.protect()`, so every route
requires a signed-in Clerk user; signed-out requests are redirected to Clerk's
hosted sign-in. There are no orgs — one Clerk user maps to one athlete profile.

## Connect and refresh Garmin

The header's **Connect Garmin** button sends credentials through a protected
Next route to a background Python worker over stdin. The browser never supplies
a Clerk subject or profile id; the route derives `auth().userId`, Python binds
that subject to an opaque profile, Fernet-encrypts credentials, and backfills
1–730 days into that profile root. A legacy flat store is never displayed or
claimed according to first-login order; migration remains an explicit,
owner-authorized maintenance action.

After a successful connection the button becomes **Refresh Garmin**, which runs
the normal idempotent incremental collector. The UI polls opaque per-user job
status and refreshes the server-rendered data after success. Only one job per
user runs at a time. Garmin MFA must be disabled because background collection
is intentionally noninteractive.


## Views

- **Dashboard** (`/`) — latest wellness snapshot + recent activities.
- **Trends** (`/trends`) — readiness, HRV, resting HR, sleep, stress, VO₂max
  over 30d / 90d / all.
- **Activities** (`/activities`) — sortable table of collected sessions.

## Seams

- `src/lib/coach-data.ts` — the only module that reads the file store; types
  mirror the Python `CoachData` projections under `derived/`.
- `src/lib/profile.ts` — resolves the signed-in Clerk subject to a profile root.
  Reads `<root>/profiles/registry.json` (matching the Python `ProfileRegistry`)
  and returns only the subject's `profiles/<id>` root. It returns `null`
  (→ "no profile linked") when no binding exists and never falls back to flat
  data.
- `src/lib/garmin-jobs.ts` + `coach/web_job.py` — the secret-safe Node→Python
  background-job boundary, job locking, encrypted credential write, collection,
  and subject-hashed status polling.

## Stack

Next.js (App Router) · Clerk · shadcn/ui · Tailwind v4 · Recharts · TanStack
Table · Biome. Data is read at request time (`dynamic = "force-dynamic"`), so a
reload reflects the latest collector run.
